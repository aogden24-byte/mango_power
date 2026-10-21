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
    BLE_OVERLAY, CLOUD_LIVE_KEYS, CONF_CAPACITY_WH, CONF_EXPORT_PRICE, CONF_IMPORT_PRICE,
    CONF_RAW_FIELDS, DEFAULT_CAPACITY_WH, DEFAULT_PRICE,
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
    """Battery power: + charging, - discharging.

    Real pack V x I when Bluetooth is current (batteryPower present), otherwise estimated as
    (solar + grid in) - (AC + DC out), which ignores conversion losses.
    """
    if isinstance(d.get("batteryPower"), (int, float)):
        return float(d["batteryPower"])
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
        # Bluetooth-only sensors, per-source comparison and link diagnostics (E only)
        ents += [MangoBleSensor(coord, desc) for desc in BLE_SENSORS]
        ents += [MangoSourceSensor(coord, key, name, src)
                 for key, name in SOURCE_COMPARE for src in ("cloud", "bluetooth")]
        ents += [MangoDiagSensor(coord, key) for key in ("active_source", "ble_via", "ble_rssi", "ble_last")]
        if entry.options.get(CONF_RAW_FIELDS, False):
            _setup_raw(coord, async_add_entities)
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
        return self.entity_description.value_fn(self._src, self._cap)

    @property
    def available(self) -> bool:
        # Live and calculated E values need a unit that is actually reporting (the cloud keeps
        # serving the last values after a unit is switched off). Derived values may still
        # legitimately be None (e.g. time to full while discharging) -> 'unknown'.
        if self._key in LIVE_OR_DERIVED and hasattr(self.coordinator, "cloud_live"):
            return self.coordinator.cloud_live or self.coordinator.ble_fresh
        return self.data_available

    @property
    def extra_state_attributes(self):
        src_of = getattr(self.coordinator, "source_of", None)
        if src_of is None:  # site coordinator has no Bluetooth source
            return None
        key = "batteryPower" if self._key == "net_power" else self._key
        src = src_of(key)
        return {"source": src} if src else None


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
        p = self._power_fn(self._src) if self.data_available else None
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


# ------------------------------------------------------------------ Bluetooth-only sensors (E)


@dataclass(frozen=True, kw_only=True)
class MangoBleDescription(SensorEntityDescription):
    ble_fn: Callable[[Data], Any]


def _pack(i: int, field: str) -> Callable[[Data], Any]:
    def fn(b: Data) -> Any:
        packs = b.get("packs") or []
        return packs[i].get(field) if i < len(packs) else None
    return fn


def _counter(src: str, field: str) -> Callable[[Data], Any]:
    return lambda b: ((b.get("energy") or {}).get(src) or {}).get(field)


_BMS_WARN = {0: "ok", 1: "no power", 2: "full", 3: "discharge over-current", 4: "charge over-current",
             5: "temperature gap", 6: "cell voltage gap", 7: "MOSFET over-temp", 8: "under-temp",
             9: "over-temp", 10: "cell under-voltage", 11: "cell over-voltage",
             12: "module under-voltage", 13: "module over-voltage"}
_BMS_FAULT = {0: "ok", 1: "cell invalid", 2: "discharge over-current", 3: "charge over-current",
              4: "short circuit", 5: "MOSFET over-temp", 6: "under-temp", 7: "over-temp",
              8: "cell over-voltage", 9: "cell under-voltage", 10: "module over-voltage",
              11: "module under-voltage"}


def _codes(b: Data, i: int, field: str) -> str | None:
    packs = b.get("packs") or []
    if i >= len(packs):
        return None
    codes = [c for c in (packs[i].get(field) or []) if c]
    if not codes:
        return "none"
    names = _BMS_WARN if field == "warnings" else _BMS_FAULT
    return ", ".join(names.get(c, f"code {c}") for c in codes)


_PCS_ERR = {1: "frequency lost", 2: "pre-charge failed", 3: "grid voltage lost", 4: "fan fault",
            5: "load over power", 6: "load over rate", 7: "load under rate", 8: "load under-voltage",
            9: "load over-voltage", 10: "bypass over rate", 11: "parallel rate mismatch",
            12: "grid over-voltage", 13: "grid under-voltage", 14: "grid lost", 15: "grid over rate",
            16: "grid under rate", 17: "AC bus over-voltage", 18: "AC bus under-voltage",
            19: "AC over-current", 20: "fuse fault", 21: "DC over-voltage", 22: "DC under-voltage",
            23: "DC short circuit", 24: "DC over-current", 25: "buck over-current",
            26: "MOSFET over-temp", 27: "PCB over-temp", 28: "PFC not ready",
            29: "DC under-voltage over power", 30: "DC bus over-voltage", 31: "DC bus under-voltage",
            32: "parallel comm fault", 33: "EEPROM fault", 34: "LLC over-current",
            35: "internal comm fault", 36: "PV over-voltage", 37: "PV under-voltage",
            38: "PV over-current", 39: "PV reverse", 50: "PFC temp fault", 51: "DC high temp",
            52: "DC low temp", 53: "parallel rate fault", 54: "PFC MOSFET over-temp",
            55: "DC MOSFET over-temp", 56: "PTC over-temp", 57: "AC load over-voltage",
            58: "AC load under-voltage"}


def _pcs_codes(b: Data) -> str | None:
    codes = b.get("pcsErrors")
    if codes is None:
        return None
    codes = [c for c in codes if c]
    return ", ".join(_PCS_ERR.get(c, f"code {c}") for c in codes) if codes else "none"


LIVE_OR_DERIVED = set(CLOUD_LIVE_KEYS) | {
    "input_power", "output_power", "net_power", "stored_energy", "energy_to_full",
    "time_to_full", "time_to_empty",
}

MPPT_MIN_V = 20  # below this the input is floating (reads ~13-17 V with nothing connected)


def _mppt_volt(b: Data) -> float | None:
    v = b.get("mpptVoltRaw")
    if v is None:
        return None
    return 0 if v < MPPT_MIN_V else v


def _mppt_current(b: Data) -> float | None:
    v, p = b.get("mpptVoltRaw"), b.get("pvPower")
    if v is None or p is None:
        return None
    return 0.0 if v < MPPT_MIN_V else round(p / v, 2)


def _ble(k: str) -> Callable[[Data], Any]:
    return lambda b: b.get(k)


def _ble_sensor(key, name, fn, unit=None, dc=None, sc=SensorStateClass.MEASUREMENT, enabled=True,
                diag=False, icon=None, prec=None) -> MangoBleDescription:
    return MangoBleDescription(
        key=f"ble_{key}", name=name, ble_fn=fn, native_unit_of_measurement=unit, device_class=dc,
        state_class=sc, entity_registry_enabled_default=enabled, icon=icon,
        suggested_display_precision=prec,
        entity_category=EntityCategory.DIAGNOSTIC if diag else None,
    )


_V, _A, _W, _C = UnitOfElectricPotential.VOLT, "A", UnitOfPower.WATT, UnitOfTemperature.CELSIUS
_pack_sensors = []
for _i in range(2):  # pack 1 = main battery, pack 2 = first expansion battery (disabled by default)
    _n, _en = f"Battery pack {_i + 1}", _i == 0
    _pack_sensors += [
        _ble_sensor(f"pack{_i+1}_voltage", f"{_n} voltage", _pack(_i, "volt"), _V, SensorDeviceClass.VOLTAGE, enabled=_en, prec=3),
        _ble_sensor(f"pack{_i+1}_current", f"{_n} current", _pack(_i, "current"), _A, SensorDeviceClass.CURRENT, enabled=_en, prec=2),
        _ble_sensor(f"pack{_i+1}_power", f"{_n} power", _pack(_i, "power"), _W, SensorDeviceClass.POWER, enabled=_en, prec=1),
        _ble_sensor(f"pack{_i+1}_temperature", f"{_n} temperature", _pack(_i, "temp"), _C, SensorDeviceClass.TEMPERATURE, enabled=_en, prec=1),
        _ble_sensor(f"pack{_i+1}_soc", f"{_n} state of charge", _pack(_i, "soc"), PERCENTAGE, SensorDeviceClass.BATTERY, enabled=_en, prec=0),
        _ble_sensor(f"pack{_i+1}_capacity", f"{_n} capacity", _pack(_i, "capacity"), "Ah", None, enabled=_en, diag=True, icon="mdi:battery-heart-variant", prec=2),
    ]

BLE_SENSORS: tuple[MangoBleDescription, ...] = (
    *_pack_sensors,
    _ble_sensor("ac_voltage", "Inverter AC voltage", _ble("acVolt"), _V, SensorDeviceClass.VOLTAGE, prec=1),
    _ble_sensor("dc_voltage", "Inverter DC voltage", _ble("dcVolt"), _V, SensorDeviceClass.VOLTAGE, prec=2),
    _ble_sensor("dc_current", "Inverter DC current", _ble("dcCurrent"), _A, SensorDeviceClass.CURRENT, prec=2),
    _ble_sensor("inlet_temp", "Air inlet temperature", _ble("inletTemp"), _C, SensorDeviceClass.TEMPERATURE, prec=1),
    _ble_sensor("outlet_temp", "Air outlet temperature", _ble("outletTemp"), _C, SensorDeviceClass.TEMPERATURE, prec=1),
    _ble_sensor("soc_real", "Real state of charge", _ble("socReal"), PERCENTAGE, SensorDeviceClass.BATTERY, diag=True, prec=0),
    _ble_sensor("work_state", "Work state", _ble("workState"), sc=None, diag=True, icon="mdi:state-machine"),
    _ble_sensor("system_state", "System state", _ble("sysState"), sc=None, diag=True, icon="mdi:state-machine"),
    _ble_sensor("warning_code", "Warning code", _ble("warning"), sc=None, diag=True, icon="mdi:alert"),
    _ble_sensor("error_code", "Error code", _ble("error"), sc=None, diag=True, icon="mdi:alert-circle"),
    _ble_sensor("mppt_voltage", "Solar input voltage", _mppt_volt, _V, SensorDeviceClass.VOLTAGE, prec=0),
    _ble_sensor("mppt_current_calc", "Solar input current (calculated)", _mppt_current, _A, SensorDeviceClass.CURRENT, prec=2),
    _ble_sensor("mppt_volt_raw", "MPPT input voltage (raw)", _ble("mpptVoltRaw"), enabled=False, diag=True, icon="mdi:solar-power-variant", prec=0),
    _ble_sensor("mppt_current_raw", "MPPT input current (raw)", _ble("mpptCurrentRaw"), enabled=False, diag=True, icon="mdi:solar-power-variant", prec=0),
    _ble_sensor("mppt_power_raw", "MPPT input power (raw)", _ble("mpptPowerRaw"), enabled=False, diag=True, icon="mdi:solar-power-variant", prec=0),
    _ble_sensor("mppt_total_raw", "MPPT input total (raw)", _ble("mpptTotalRaw"), enabled=False, diag=True, icon="mdi:solar-power-variant", prec=0),
    _ble_sensor("solar_volt_raw", "Solar input voltage (collector, raw)", _counter("solar", "volt"), enabled=False, diag=True, icon="mdi:solar-power-variant", prec=0),
    _ble_sensor("solar_current_raw", "Solar input current (collector, raw)", _counter("solar", "current"), enabled=False, diag=True, icon="mdi:solar-power-variant", prec=0),
    _ble_sensor("pack1_state_raw", "Battery pack 1 state (raw)", _pack(0, "stateRaw"), sc=None, diag=True, icon="mdi:numeric"),
    _ble_sensor("report_interval", "Report interval (setting)", _ble("reportInterval"), "s", None, sc=None, diag=True, icon="mdi:timer-cog-outline"),
    _ble_sensor("battery_soh", "Battery state of health", _ble("batterySoh"), PERCENTAGE, diag=True, icon="mdi:battery-heart", prec=0),
    _ble_sensor("pack1_warnings", "Battery pack 1 warning codes", lambda b: _codes(b, 0, "warnings"), sc=None, diag=True, icon="mdi:alert"),
    _ble_sensor("pack1_faults", "Battery pack 1 fault codes", lambda b: _codes(b, 0, "faults"), sc=None, diag=True, icon="mdi:alert-circle"),
    _ble_sensor("pcs_status", "Inverter status codes", lambda b: _pcs_codes(b), sc=None, diag=True, icon="mdi:information-outline"),
    _ble_sensor("set_max_charge_current", "Max AC input (unit setting)", _ble("setMaxChargeCurrent"), "A", None, sc=None, diag=True, icon="mdi:current-ac"),
    _ble_sensor("set_backup_soc", "Backup SOC (unit setting)", _ble("setUpsAutoChargeSoc"), PERCENTAGE, None, sc=None, diag=True, icon="mdi:battery-lock"),
    *[_ble_sensor(f"ver_{k.lower()}", f"Firmware {label}", _ble(f"ver{k}"), sc=None, diag=True, icon="mdi:chip")
      for k, label in (("Ems", "EMS"), ("PcsPfc", "inverter PFC"), ("PcsDc", "inverter DC"), ("Mppt", "MPPT"), ("Bms", "BMS"))],
    _ble_sensor("factory_time", "Factory date", _ble("factoryTime"), sc=None, diag=True, icon="mdi:factory"),
    *[
        _ble_sensor(f"counter_{src}_{f}", f"{label} {f} (device counter, raw)", _counter(src, f),
                    sc=SensorStateClass.TOTAL_INCREASING, enabled=False, diag=True, icon="mdi:counter")
        for src, label in (("solar", "Solar"), ("load", "Load"), ("grid_in", "Grid in"),
                           ("grid_out", "Grid out"), ("battery_in", "Battery in"), ("battery_out", "Battery out"))
        for f in ("today", "total")
    ],
)


class MangoBleSensor(MangoEntity, SensorEntity):
    """Only available while Bluetooth is connected and current."""

    entity_description: MangoBleDescription

    def __init__(self, coordinator, description: MangoBleDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self):
        return self.entity_description.ble_fn(self.coordinator.ble) if self.coordinator.ble_fresh else None

    @property
    def available(self) -> bool:
        return self.native_value is not None


# ------------------------------------------------------------------ per-source comparison (power users)

SOURCE_COMPARE = (
    ("soc", "State of charge"),
    ("pvPower", "Solar power"),
    ("gridImportPower", "Grid input power"),
    ("loadPower", "AC output power"),
    ("loadDcPower", "DC output power"),
    ("batteryPower", "Battery power"),
)


class MangoSourceSensor(MangoEntity, SensorEntity):
    """The same measurement from one specific source. Disabled by default."""

    _attr_entity_registry_enabled_default = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, key: str, name: str, source: str) -> None:
        super().__init__(coordinator, f"{key}_{source}")
        self._src_key, self._source = key, source
        self._attr_name = f"{name} ({'Bluetooth' if source == 'bluetooth' else 'cloud'})"
        if key == "soc":
            self._attr_native_unit_of_measurement = PERCENTAGE
            self._attr_device_class = SensorDeviceClass.BATTERY
            self._attr_suggested_display_precision = 0
        else:
            self._attr_native_unit_of_measurement = UnitOfPower.WATT
            self._attr_device_class = SensorDeviceClass.POWER
            self._attr_suggested_display_precision = 0 if key != "batteryPower" else 1
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self):
        c = self.coordinator
        if self._source == "cloud":
            if self._src_key == "batteryPower":  # cloud has none; show the estimate it would fall back to
                d = dict(c.data or {})
                d.pop("batteryPower", None)
                return net_power(d) if c.last_update_success else None
            return c.cloud_get(self._src_key)
        b = c.ble if c.ble_fresh else {}
        if self._src_key == "gridImportPower":
            g = b.get("gridPower")
            return None if g is None else max(g, 0)
        bkey = BLE_OVERLAY.get(self._src_key, self._src_key)
        return b.get(bkey)

    @property
    def available(self) -> bool:
        return self.native_value is not None


# ------------------------------------------------------------------ connection diagnostics


class MangoDiagSensor(MangoEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    NAMES = {
        "active_source": ("Data source", "mdi:swap-horizontal"),
        "ble_via": ("Bluetooth proxy", "mdi:bluetooth-connect"),
        "ble_rssi": ("Bluetooth signal", "mdi:signal"),
        "ble_last": ("Bluetooth last update", "mdi:clock-outline"),
    }

    def __init__(self, coordinator, key: str) -> None:
        super().__init__(coordinator, key)
        self._attr_name, self._attr_icon = self.NAMES[key]
        if key == "ble_rssi":
            self._attr_native_unit_of_measurement = "dBm"
            self._attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
            self._attr_entity_registry_enabled_default = False
        if key == "ble_last":
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
        if key == "active_source":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = ["bluetooth", "cloud", "none"]

    @property
    def native_value(self):
        c, link = self.coordinator, self.coordinator.ble_link
        if self._key == "active_source":
            return c.active_source
        if self._key == "ble_via":
            return link.via if link and link.connected else None
        if self._key == "ble_rssi":
            return link.rssi if link and link.connected else None
        return c.ble_wall

    @property
    def available(self) -> bool:
        return True


# ------------------------------------------------------------------ raw protocol fields (option)


def _setup_raw(coord, async_add_entities) -> None:
    """Create a sensor for every protocol field as it first appears (Configure -> raw fields)."""
    created: set[str] = set()

    @callback
    def _add(keys: list[str]) -> None:
        new = [k for k in keys if k not in created]
        created.update(new)
        if new:
            async_add_entities([MangoRawSensor(coord, k) for k in new])

    coord.raw_new_cb = _add
    if coord.raw:
        _add(list(coord.raw))


class MangoRawSensor(MangoEntity, SensorEntity):
    """One raw Bluetooth protocol field, e.g. "rt.5.3.2" (paths use mango.proto field numbers)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:code-braces"

    def __init__(self, coordinator, path: str) -> None:
        super().__init__(coordinator, f"raw_{path.replace('.', '_').replace('[', '_').replace(']', '')}")
        self._path = path
        self._attr_name = f"Raw {path}"
        if isinstance(coordinator.raw.get(path), int):
            self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self):
        v = self.coordinator.raw.get(self._path)
        return v if self.coordinator.ble_fresh or self._path.split(".")[0] in ("static", "setting") else None

    @property
    def extra_state_attributes(self):
        v = self.coordinator.raw.get(self._path)
        if isinstance(v, int):
            return {"zigzag": (v >> 1) ^ -(v & 1) if v >= 0 else None, "hex": hex(v & ((1 << 64) - 1))}
        return None

    @property
    def available(self) -> bool:
        return self._path in self.coordinator.raw
