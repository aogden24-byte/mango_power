"""Sensors for Mango Power units: reported values, derived values and energy totals."""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfApparentPower,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfReactivePower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from . import MangoConfigEntry
from .const import (
    CONF_CAPACITY_WH, CONF_EXPORT_PRICE, CONF_IMPORT_PRICE, DEFAULT_CAPACITY_WH, DEFAULT_PRICE,
    DOMAIN, MAX_INTEGRATION_GAP_S, MODEL_GW, MODEL_S,
)
from .coordinator import MangoDeviceCoordinator
from .entity import MangoEntity

Data = dict[str, Any]


def _num(d: Data, *keys: str) -> float | None:
    """Sum of the given keys; None if all are missing."""
    vals = [d.get(k) for k in keys]
    vals = [float(v) for v in vals if isinstance(v, (int, float))]
    return sum(vals) if vals else None


def input_power(d: Data) -> float | None:
    return _num(d, "pvPower", "gridImportPower")


def output_power(d: Data) -> float | None:
    return _num(d, "loadPower", "loadDcPower")


def net_power(d: Data) -> float | None:
    """Estimated battery power: + charging, - discharging (ignores conversion losses)."""
    i, o = input_power(d), output_power(d)
    if i is None and o is None:
        return None
    return (i or 0) - (o or 0)


# ------------------------------------------------------------------ descriptions


@dataclass(frozen=True, kw_only=True)
class MangoSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Data, float], Any]


def _power(key, name, fn, icon=None, enabled=True) -> MangoSensorDescription:
    return MangoSensorDescription(
        key=key, name=name, icon=icon, value_fn=fn,
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=enabled,
    )


def _raw(k: str) -> Callable[[Data, float], Any]:
    return lambda d, cap: d.get(k)


def _time_to_full(d: Data, cap: float) -> float | None:
    net, soc = net_power(d), d.get("soc")
    if net is None or soc is None or net <= 5 or soc >= 100:
        return None
    return round((100 - soc) / 100 * cap / net * 60)


def _time_to_empty(d: Data, cap: float) -> float | None:
    net, soc = net_power(d), d.get("soc")
    if net is None or soc is None or net >= -5:
        return None
    return round(soc / 100 * cap / -net * 60)


SENSORS: tuple[MangoSensorDescription, ...] = (
    # --- reported by the cloud ---
    MangoSensorDescription(
        key="soc", name="State of charge", value_fn=_raw("soc"),
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    _power("pvPower", "Solar power", _raw("pvPower"), "mdi:solar-power"),
    _power("gridImportPower", "Grid input power", _raw("gridImportPower"), "mdi:transmission-tower-import"),
    _power("gridExportPower", "Grid export power", _raw("gridExportPower"), "mdi:transmission-tower-export", enabled=False),
    _power("loadPower", "AC output power", _raw("loadPower"), "mdi:power-socket-us"),
    _power("loadDcPower", "DC output power", _raw("loadDcPower"), "mdi:current-dc"),
    MangoSensorDescription(
        key="inverterTemp", name="Inverter temperature", value_fn=_raw("inverterTemp"),
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    # --- derived ---
    _power("input_power", "Total input power", lambda d, c: input_power(d), "mdi:import"),
    _power("output_power", "Total output power", lambda d, c: output_power(d), "mdi:export"),
    _power("net_power", "Net battery power", lambda d, c: net_power(d), "mdi:battery-sync"),
    MangoSensorDescription(
        key="stored_energy", name="Stored energy", icon="mdi:battery-high",
        value_fn=lambda d, cap: round(d["soc"] / 100 * cap / 1000, 2) if d.get("soc") is not None else None,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    MangoSensorDescription(
        key="energy_to_full", name="Energy to full", icon="mdi:battery-plus-outline",
        value_fn=lambda d, cap: round((100 - d["soc"]) / 100 * cap / 1000, 2) if d.get("soc") is not None else None,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    MangoSensorDescription(
        key="capacity", name="Rated capacity", icon="mdi:battery",
        value_fn=lambda d, cap: round(cap / 1000, 2),
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MangoSensorDescription(
        key="time_to_full", name="Time to full", icon="mdi:battery-clock",
        value_fn=_time_to_full,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
    ),
    MangoSensorDescription(
        key="time_to_empty", name="Time to empty", icon="mdi:battery-clock-outline",
        value_fn=_time_to_empty,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
    ),
    # --- diagnostics ---
    MangoSensorDescription(
        key="lastActiveAt", name="Last active",
        value_fn=lambda d, c: dt_util.parse_datetime(d["lastActiveAt"]) if d.get("lastActiveAt") else None,
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MangoSensorDescription(
        key="firmware", name="Firmware", icon="mdi:chip", value_fn=_raw("firmware"),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # --- settings the app doesn't show (read-only) ---
    MangoSensorDescription(
        key="acOutputVolt", name="AC output voltage setting", icon="mdi:sine-wave",
        value_fn=_raw("acOutputVolt"),
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MangoSensorDescription(
        key="acFrequency", name="AC frequency setting", icon="mdi:sine-wave",
        value_fn=_raw("acFrequency"),
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        device_class=SensorDeviceClass.FREQUENCY,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MangoSensorDescription(
        key="ecoReservedSoc", name="Eco reserve SOC", icon="mdi:battery-lock-open",
        value_fn=_raw("ecoReservedSoc"),
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


# ------------------------------------------------------------------ Mango Power S (microinverter)


def _s(key, name, unit, device_class, *, icon=None, enabled=True, category=None,
       state_class=SensorStateClass.MEASUREMENT, precision=None) -> MangoSensorDescription:
    """A value reported as-is by the S's realtime endpoint."""
    return MangoSensorDescription(
        key=key, name=name, icon=icon, value_fn=_raw(key),
        native_unit_of_measurement=unit,
        device_class=device_class,
        state_class=state_class,
        entity_registry_enabled_default=enabled,
        entity_category=category,
        suggested_display_precision=precision,
    )


S_SENSORS: tuple[MangoSensorDescription, ...] = (
    # DC side (solar panel)
    _s("dcPower", "PV input power", UnitOfPower.WATT, SensorDeviceClass.POWER, icon="mdi:solar-power"),
    _s("dcVoltage", "PV input voltage", UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE, precision=1),
    _s("dcCurrent", "PV input current", UnitOfElectricCurrent.AMPERE, SensorDeviceClass.CURRENT, precision=2),
    # AC side (to the house)
    _s("acPower", "AC output power", UnitOfPower.WATT, SensorDeviceClass.POWER, icon="mdi:power-socket-us"),
    _s("acVoltage", "AC voltage", UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE, precision=1),
    _s("acCurrent", "AC current", UnitOfElectricCurrent.AMPERE, SensorDeviceClass.CURRENT, precision=2),
    _s("acFrequency", "AC frequency", UnitOfFrequency.HERTZ, SensorDeviceClass.FREQUENCY, precision=2),
    _s("temperature", "Temperature", UnitOfTemperature.CELSIUS, SensorDeviceClass.TEMPERATURE, precision=1),
    # Energy, as reported by the unit (usable directly in the Energy dashboard)
    _s("totalEnergyProduced", "Energy produced", UnitOfEnergy.KILO_WATT_HOUR, SensorDeviceClass.ENERGY,
       icon="mdi:solar-power-variant", state_class=SensorStateClass.TOTAL_INCREASING, precision=3),
    # diagnostics
    _s("rssi", "Wi-Fi signal", SIGNAL_STRENGTH_DECIBELS_MILLIWATT, SensorDeviceClass.SIGNAL_STRENGTH,
       category=EntityCategory.DIAGNOSTIC, enabled=False),
    MangoSensorDescription(
        key="meshLevel", name="Mesh level", icon="mdi:lan", value_fn=_raw("meshLevel"),
        entity_category=EntityCategory.DIAGNOSTIC, entity_registry_enabled_default=False,
    ),
    *(d for d in SENSORS if d.key in ("lastActiveAt", "firmware")),
)

# Grid meter (Gateway Lite). The cloud reports it only through the root unit of a mesh group.
S_METER_SENSORS: tuple[MangoSensorDescription, ...] = (
    _s("meterPhaseAActivePower", "Meter active power", UnitOfPower.WATT, SensorDeviceClass.POWER,
       icon="mdi:meter-electric"),
    _s("meterPhaseAVoltage", "Meter voltage", UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE, precision=1),
    _s("meterPhaseACurrent", "Meter current", UnitOfElectricCurrent.AMPERE, SensorDeviceClass.CURRENT, precision=2),
    _s("meterPhaseAReactivePower", "Meter reactive power", UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
       SensorDeviceClass.REACTIVE_POWER, enabled=False),
    _s("meterPhaseAApparentPower", "Meter apparent power", UnitOfApparentPower.VOLT_AMPERE,
       SensorDeviceClass.APPARENT_POWER, enabled=False),
    _s("meterPhaseAPowerFactor", "Meter power factor", None, SensorDeviceClass.POWER_FACTOR,
       enabled=False, precision=2),
    _s("meterRssi", "Meter Wi-Fi signal", SIGNAL_STRENGTH_DECIBELS_MILLIWATT, SensorDeviceClass.SIGNAL_STRENGTH,
       category=EntityCategory.DIAGNOSTIC, enabled=False),
)


# ------------------------------------------------------------------ Gateway Lite (grid meter)


def _grid(d: Data) -> float | None:
    v = d.get("meterPhaseAActivePower")
    return float(v) if isinstance(v, (int, float)) else None


def _grid_import(d: Data) -> float | None:
    """Power drawn from the grid (positive meter reading is assumed to mean import)."""
    g = _grid(d)
    return None if g is None else max(g, 0.0)


def _grid_export(d: Data) -> float | None:
    g = _grid(d)
    return None if g is None else max(-g, 0.0)


GW_SENSORS: tuple[MangoSensorDescription, ...] = (
    _s("meterPhaseAActivePower", "Grid power", UnitOfPower.WATT, SensorDeviceClass.POWER,
       icon="mdi:transmission-tower"),
    _power("grid_import_power", "Grid import power", lambda d, c: _grid_import(d), "mdi:transmission-tower-import"),
    _power("grid_export_power", "Grid export power", lambda d, c: _grid_export(d), "mdi:transmission-tower-export"),
    _s("meterPhaseAVoltage", "Grid voltage", UnitOfElectricPotential.VOLT, SensorDeviceClass.VOLTAGE, precision=1),
    _s("meterPhaseACurrent", "Grid current", UnitOfElectricCurrent.AMPERE, SensorDeviceClass.CURRENT, precision=2),
    _s("meterPhaseAReactivePower", "Grid reactive power", UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
       SensorDeviceClass.REACTIVE_POWER, enabled=False),
    _s("meterPhaseAApparentPower", "Grid apparent power", UnitOfApparentPower.VOLT_AMPERE,
       SensorDeviceClass.APPARENT_POWER, enabled=False),
    _s("meterPhaseAPowerFactor", "Grid power factor", None, SensorDeviceClass.POWER_FACTOR,
       enabled=False, precision=2),
    _s("meterRssi", "Meter Wi-Fi signal", SIGNAL_STRENGTH_DECIBELS_MILLIWATT, SensorDeviceClass.SIGNAL_STRENGTH,
       category=EntityCategory.DIAGNOSTIC, enabled=False),
    *(d for d in SENSORS if d.key in ("lastActiveAt", "firmware")),
)

# kWh totals integrated from the grid power, for the Energy dashboard
GW_ENERGY: tuple[tuple[str, str, Callable[[Data], float | None], str], ...] = (
    ("grid_import_energy", "Grid import energy", _grid_import, "mdi:transmission-tower-import"),
    ("grid_export_energy", "Grid export energy", _grid_export, "mdi:transmission-tower-export"),
)


# ------------------------------------------------------------------ Site totals (cloud, both phases)


def _site(key: str) -> Callable[[Data], float | None]:
    def fn(d: Data) -> float | None:
        v = d.get(key)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    return fn


def _site_import(d: Data) -> float | None:
    g = _site("grid_power")(d)
    return None if g is None else max(g, 0.0)


def _site_export(d: Data) -> float | None:
    g = _site("grid_power")(d)
    return None if g is None else max(-g, 0.0)


SITE_SENSORS: tuple[MangoSensorDescription, ...] = (
    _power("grid_power", "Grid power", lambda d, c: _site("grid_power")(d), "mdi:transmission-tower"),
    _power("grid_import_power", "Grid import power", lambda d, c: _site_import(d), "mdi:transmission-tower-import"),
    _power("grid_export_power", "Grid export power", lambda d, c: _site_export(d), "mdi:transmission-tower-export"),
    _power("pv_power", "Solar power", lambda d, c: _site("pv_power")(d), "mdi:solar-power"),
    _power("load_power", "Home power", lambda d, c: _site("load_power")(d), "mdi:home-lightning-bolt"),
)

SITE_ENERGY: tuple[tuple[str, str, Callable[[Data], float | None], str], ...] = (
    ("grid_import_energy", "Grid import energy", _site_import, "mdi:transmission-tower-import"),
    ("grid_export_energy", "Grid export energy", _site_export, "mdi:transmission-tower-export"),
    ("load_energy", "Home energy", _site("load_power"), "mdi:home-lightning-bolt"),
)


def _charge(d: Data) -> float | None:
    n = net_power(d)
    return None if n is None else max(n, 0.0)


def _discharge(d: Data) -> float | None:
    n = net_power(d)
    return None if n is None else max(-n, 0.0)


# Energy totals for the Energy dashboard: (key, name, power function, icon)
ENERGY: tuple[tuple[str, str, Callable[[Data], float | None], str], ...] = (
    ("solar_energy", "Solar energy", lambda d: _num(d, "pvPower"), "mdi:solar-power"),
    ("grid_import_energy", "Grid input energy", lambda d: _num(d, "gridImportPower"), "mdi:transmission-tower-import"),
    ("ac_output_energy", "AC output energy", lambda d: _num(d, "loadPower"), "mdi:power-socket-us"),
    ("dc_output_energy", "DC output energy", lambda d: _num(d, "loadDcPower"), "mdi:current-dc"),
    ("battery_charge_energy", "Battery charge energy", _charge, "mdi:battery-arrow-up"),
    ("battery_discharge_energy", "Battery discharge energy", _discharge, "mdi:battery-arrow-down"),
)


class MangoDailyEnergySensor(MangoEntity, RestoreSensor):
    """Energy produced today by one S inverter (lifetime counter minus its value at midnight)."""

    def __init__(self, coordinator: MangoDeviceCoordinator, key: str, name: str) -> None:
        super().__init__(coordinator, key)
        self.entity_description = SensorEntityDescription(
            key=key, name=name, icon="mdi:solar-power-variant-outline",
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            device_class=SensorDeviceClass.ENERGY,
            state_class=SensorStateClass.TOTAL_INCREASING,
            suggested_display_precision=3,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        prev = await self.async_get_last_state()
        if prev:
            self.coordinator.daily.restore(
                prev.attributes.get("day"), prev.attributes.get("start_of_day_total")
            )

    @property
    def native_value(self) -> float | None:
        return self.coordinator.daily.value

    @property
    def extra_state_attributes(self):
        d = self.coordinator.daily
        return {"day": d.day, "start_of_day_total": d.baseline}

    @property
    def available(self) -> bool:
        return self.coordinator.daily.value is not None


def _live_sum(key: str) -> Callable[[list[MangoDeviceCoordinator]], float | None]:
    """Sum a reported value over the inverters that answered the last poll."""
    def fn(coords: list[MangoDeviceCoordinator]) -> float | None:
        vals = [
            c.data[key] for c in coords
            if c.last_update_success and c.data and isinstance(c.data.get(key), (int, float))
        ]
        return round(sum(vals), 2) if vals else None
    return fn


def _daily_sum(coords: list[MangoDeviceCoordinator]) -> float | None:
    vals = [c.daily.value for c in coords if c.daily.value is not None]
    return round(sum(vals), 3) if vals else None


def _lifetime_sum(coords: list[MangoDeviceCoordinator]) -> float | None:
    # last known counter per inverter, so one unit going offline does not make the total dip
    vals = [c.daily.last_total for c in coords if c.daily.last_total is not None]
    return round(sum(vals), 3) if vals else None


# (key, description, function over all S coordinators)
ARRAY_SENSORS = (
    ("ac_power", SensorEntityDescription(
        key="ac_power", name="Total AC output power", icon="mdi:solar-power",
        native_unit_of_measurement=UnitOfPower.WATT, device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT), _live_sum("acPower")),
    ("dc_power", SensorEntityDescription(
        key="dc_power", name="Total PV input power", icon="mdi:solar-panel-large",
        native_unit_of_measurement=UnitOfPower.WATT, device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT), _live_sum("dcPower")),
    ("energy_today", SensorEntityDescription(
        key="energy_today", name="Total energy produced today", icon="mdi:solar-power-variant-outline",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING, suggested_display_precision=3), _daily_sum),
    ("energy_total", SensorEntityDescription(
        key="energy_total", name="Total energy produced", icon="mdi:solar-power-variant",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR, device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING, suggested_display_precision=3), _lifetime_sum),
)


class MangoArraySensor(SensorEntity):
    """A total across all Mango Power S inverters, shown on one virtual "array" device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry_id: str, coords: list[MangoDeviceCoordinator], key: str,
                 description: SensorEntityDescription, fn) -> None:
        self._coords = coords
        self._fn = fn
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_s_array_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_s_array")},
            name="Mango Power S array",
            manufacturer="Mango Power",
            model="Mango Power S (all inverters)",
        )

    async def async_added_to_hass(self) -> None:
        for coord in self._coords:
            self.async_on_remove(coord.async_add_listener(self._refresh))

    @callback
    def _refresh(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        return self._fn(self._coords)


async def async_setup_entry(
    hass: HomeAssistant, entry: MangoConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    cap = float(entry.options.get(CONF_CAPACITY_WH, DEFAULT_CAPACITY_WH))
    ents: list[SensorEntity] = []
    for coord in entry.runtime_data.coordinators:
        if coord.model == MODEL_GW:
            ents += [MangoSensor(coord, desc, cap) for desc in GW_SENSORS]
            ents += [MangoEnergySensor(coord, *spec) for spec in GW_ENERGY]
            continue
        if coord.model == MODEL_S:
            # Meter readings live on the Gateway Lite device when there is one.
            with_meter = coord.is_group_root and not coord.meter_via_gateway
            descs = S_SENSORS + (S_METER_SENSORS if with_meter else ())
            ents += [MangoSensor(coord, desc, cap) for desc in descs]
            ents.append(MangoDailyEnergySensor(coord, "todayEnergy", "Energy produced today"))
            continue
        ents += [MangoSensor(coord, desc, cap) for desc in SENSORS]
        ents += [MangoEnergySensor(coord, *spec) for spec in ENERGY]
        # daily totals, reset at local midnight (solar enabled by default, others optional)
        ents += [
            MangoEnergySensor(coord, f"{key}_today", f"{name} today", fn, icon,
                              daily=True, enabled=(key == "solar_energy"))
            for key, name, fn, icon in ENERGY
        ]
    # Whole-site totals from the cloud (both grid phases).
    for site in entry.runtime_data.sites:
        ents += [MangoSensor(site, desc, cap) for desc in SITE_SENSORS]
        ents.append(MangoSiteDailySensor(site, "energy_today_generation", "Solar energy today"))
        ents += [MangoEnergySensor(site, *spec) for spec in SITE_ENERGY]
        today = {
            key: MangoEnergySensor(site, f"{key}_today", f"{name} today", fn, icon, daily=True)
            for key, name, fn, icon in SITE_ENERGY
        }
        ents += list(today.values())
        # Estimated money for today's import/export, when a price is set in the options.
        for key, label, opt in (("grid_export_energy", "Grid export value today", CONF_EXPORT_PRICE),
                                ("grid_import_energy", "Grid import cost today", CONF_IMPORT_PRICE)):
            price = float(entry.options.get(opt, DEFAULT_PRICE) or 0)
            if price > 0:
                ents.append(MangoValueSensor(site, f"{key}_value_today", label, today[key], price,
                                             hass.config.currency))
    # Totals across all S inverters, on their own "array" device (only useful with 2 or more).
    s_coords = [c for c in entry.runtime_data.coordinators if c.model == MODEL_S]
    if len(s_coords) >= 2:
        ents += [
            MangoArraySensor(entry.entry_id, s_coords, key, desc, fn)
            for key, desc, fn in ARRAY_SENSORS
        ]
    async_add_entities(ents)


class MangoSensor(MangoEntity, SensorEntity):
    entity_description: MangoSensorDescription

    def __init__(self, coordinator: MangoDeviceCoordinator, description: MangoSensorDescription, cap: float) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._cap = cap

    @property
    def native_value(self):
        return self.entity_description.value_fn(self.coordinator.data or {}, self._cap)

    @property
    def available(self) -> bool:
        # derived values (time to full etc.) may legitimately be None -> 'unknown', not unavailable
        return self.coordinator.last_update_success


class MangoEnergySensor(MangoEntity, RestoreSensor):
    """kWh total integrated from power (trapezoidal rule); survives restarts."""

    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 3

    def __init__(self, coordinator, key, name, power_fn, icon, daily=False, enabled=True) -> None:
        super().__init__(coordinator, key)
        self._attr_name = name
        self._attr_icon = icon
        self._attr_entity_registry_enabled_default = enabled
        self._power_fn = power_fn
        self._daily = daily
        self._day = dt_util.now().date().isoformat()
        self._kwh = 0.0
        self._last_p: float | None = None
        self._last_t: float | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last and isinstance(last.native_value, (int, float)):
            self._kwh = float(last.native_value)
        if self._daily:
            prev = await self.async_get_last_state()
            if not prev or prev.attributes.get("day") != self._day:
                self._kwh = 0.0  # restarted on a new day
            self.async_on_remove(
                async_track_time_change(self.hass, self._midnight, hour=0, minute=0, second=0)
            )
        self._sample()

    @callback
    def _midnight(self, now) -> None:
        self._day = dt_util.now().date().isoformat()
        self._kwh = 0.0
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self):
        return {"day": self._day} if self._daily else None

    def _sample(self) -> None:
        p = self._power_fn(self.coordinator.data or {}) if self.coordinator.last_update_success else None
        now = time.monotonic()
        if p is not None and self._last_p is not None and self._last_t is not None:
            dt = now - self._last_t
            if 0 < dt <= MAX_INTEGRATION_GAP_S:
                self._kwh += (self._last_p + p) / 2 * dt / 3_600_000
        if p is not None:
            self._last_p, self._last_t = p, now
        else:
            self._last_p = self._last_t = None

    @callback
    def _handle_coordinator_update(self) -> None:
        self._sample()
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> float:
        return round(self._kwh, 4)

    @property
    def available(self) -> bool:
        return True  # keep totals available so the Energy dashboard never sees a gap


class MangoSiteDailySensor(MangoEntity, SensorEntity):
    """Energy produced today, as worked out by the Mango cloud for the whole site."""

    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:solar-power-variant-outline"

    def __init__(self, coordinator, key: str, name: str) -> None:
        super().__init__(coordinator, key)
        self._attr_name = name

    @property
    def native_value(self):
        v = self.value
        return round(float(v), 3) if isinstance(v, (int, float)) else None


class MangoValueSensor(MangoEntity, SensorEntity):
    """Estimated money for an energy-today sensor: kWh x the price set in the options."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:cash"

    def __init__(self, coordinator, key: str, name: str, energy: MangoEnergySensor, price: float,
                 currency: str) -> None:
        super().__init__(coordinator, key)
        self._attr_name = name
        self._energy = energy
        self._price = price
        self._attr_native_unit_of_measurement = currency

    @property
    def native_value(self) -> float:
        return round(self._energy.native_value * self._price, 4)

    @property
    def available(self) -> bool:
        return True
