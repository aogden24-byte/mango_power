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
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from . import MangoConfigEntry
from .const import CONF_CAPACITY_WH, DEFAULT_CAPACITY_WH, MAX_INTEGRATION_GAP_S
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


async def async_setup_entry(
    hass: HomeAssistant, entry: MangoConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    cap = float(entry.options.get(CONF_CAPACITY_WH, DEFAULT_CAPACITY_WH))
    ents: list[SensorEntity] = []
    for coord in entry.runtime_data.coordinators:
        ents += [MangoSensor(coord, desc, cap) for desc in SENSORS]
        ents += [MangoEnergySensor(coord, *spec) for spec in ENERGY]
        # daily totals, reset at local midnight (solar enabled by default, others optional)
        ents += [
            MangoEnergySensor(coord, f"{key}_today", f"{name} today", fn, icon,
                              daily=True, enabled=(key == "solar_energy"))
            for key, name, fn, icon in ENERGY
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
