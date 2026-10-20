"""Polling coordinator: one per Mango Power unit."""
from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import MangoApi, MangoAuthError, MangoError
from .const import (
    BLE_OVERLAY,
    BLE_STALE_AFTER,
    CLOUD_LIVE_KEYS,
    CLOUD_STALE_AFTER,
    DOMAIN,
    METER_KEYS,
    MODEL_E,
    MODEL_GW,
    MODEL_S,
    SCAN_INTERVAL,
    SUPPORTED_MODELS,
)

_LOGGER = logging.getLogger(__name__)


class DailyEnergy:
    """Energy produced since local midnight, taken from the unit's lifetime counter.

    The cloud's own "today" value stays at 0 on the S, so the day's energy is the lifetime
    counter minus its value at the start of the day. The start-of-day value is restored by the
    sensor after a Home Assistant restart.
    """

    def __init__(self) -> None:
        self.day: str | None = None
        self.baseline: float | None = None  # lifetime kWh at the start of `day`
        self.last_total: float | None = None  # last lifetime kWh seen
        self.value: float | None = None  # kWh produced today

    def update(self, total: Any) -> None:
        if not isinstance(total, (int, float)):
            return
        today = dt_util.now().date().isoformat()
        if self.day != today or self.baseline is None or total < self.baseline:
            # new day, first reading, or the unit's counter went backwards (reset, firmware)
            self.day, self.baseline = today, float(total)
        self.last_total = float(total)
        self.value = round(self.last_total - self.baseline, 4)

    def restore(self, day: Any, baseline: Any) -> None:
        """Re-apply a start-of-day value saved before a restart (same day only)."""
        if day != dt_util.now().date().isoformat() or not isinstance(baseline, (int, float)):
            return
        if self.last_total is not None and self.last_total < baseline:
            return
        self.day, self.baseline = day, float(baseline)
        if self.last_total is not None:
            self.value = round(self.last_total - self.baseline, 4)


class MangoDeviceCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetches realtime data, device state and settings for one unit."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, api: MangoApi, device: dict[str, Any]
    ) -> None:
        self.api = api
        self.device_id: str = str(device["id"])
        self.sn: str = str(device.get("sn") or device.get("displaySn") or self.device_id)
        self.model: str = device.get("model") or MODEL_E
        self.model_name: str = SUPPORTED_MODELS.get(self.model, "Mango Power")
        self.group_name: str | None = device.get("group_name")
        self.is_group_root: bool = bool(device.get("is_group_root"))
        # Gateway Lite: id of the root inverter that reports its meter readings.
        self.meter_source_id: str | None = device.get("meter_source_id")
        # Inverter: True once a Gateway Lite device shows the meter readings instead.
        self.meter_via_gateway: bool = False
        # Inverter: energy produced today, worked out from the lifetime counter.
        self.daily = DailyEnergy()
        name = device.get("name")
        if self.model == MODEL_S:
            # Inverters are often just named "1", "2 (left)"... in the app; make them identifiable.
            self.device_name: str = f"{self.model_name} {name}" if name else f"{self.model_name} {self.sn[-3:]}"
        else:
            self.device_name = name or f"{self.model_name} {self.sn[-3:]}"
        self.firmware: str | None = None
        # Local Bluetooth overlay (Mango Power E only; stays empty for S / Gateway Lite).
        self.ble: dict[str, Any] = {}       # latest decoded Bluetooth values
        self.ble_time: float | None = None   # monotonic time of last Bluetooth update
        self.ble_link = None                 # MangoBleLink, set in __init__.py
        self.ble_wall = None                 # wall-clock time of last Bluetooth update
        self.raw: dict[str, object] = {}     # every protocol field seen (when raw fields are enabled)
        self.raw_new_cb = None               # set by sensor.py: called with new raw keys
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{self.sn}",
            update_interval=SCAN_INTERVAL,
        )

    @property
    def tz(self) -> str:
        return self.hass.config.time_zone or "UTC"

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            if self.model == MODEL_GW:
                rt = await self.api.realtime(self.meter_source_id, self.tz) if self.meter_source_id else {}
            else:
                rt = await self.api.realtime(self.device_id, self.tz)
            info = await self.api.device_info(self.device_id)
            # The S exposes no settings (the cloud returns an empty object), so skip that call.
            setting = await self.api.settings(self.device_id, self.tz) if self.model == MODEL_E else {}
        except MangoAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except MangoError as err:
            raise UpdateFailed(str(err)) from err

        state = info.get("state") or {}
        self.firmware = state.get("firmwareVersion") or self.firmware
        if self.model == MODEL_GW:
            return {
                "online": state.get("status") == 1,
                "lastActiveAt": state.get("lastActiveAt"),
                "firmware": self.firmware,
                **{k: rt.get(k) for k in METER_KEYS},
            }

        common = {
            **{k: v for k, v in rt.items() if not isinstance(v, (dict, list))},
            "online": state.get("status") == 1,
            "lastActiveAt": state.get("lastActiveAt"),
            "firmware": self.firmware,
        }
        if self.model == MODEL_S:
            self.daily.update(rt.get("totalEnergyProduced"))
            # Only the mesh level is taken: the rest of meshNetwork holds Wi-Fi credentials.
            return {**common, "meshLevel": (state.get("meshNetwork") or {}).get("meshLevel")}

        ems = setting.get("emsSetting") or {}
        pcs = setting.get("pcsSetting") or {}
        return {
            **common,
            "soc": state.get("soc"),
            **{k: ems.get(k) for k in (
                "acOutputEnable", "dcOutputEnable", "chargeEnable", "upsEnable",
                "maxChargeCurrent", "upsAutoChargeSoc",
                # not exposed by the app; read-only diagnostics
                "discharge240Enable", "timerMode", "ecoReservedSoc", "boot",
            )},
            "acOutputVolt": pcs.get("acOutputVolt"),
            "acFrequency": pcs.get("acFrequency"),
        }

    async def async_send(self, setting_type: str, setting_data: dict[str, Any]) -> None:
        """Send a control command, then refresh shortly after."""
        try:
            await self.api.send_cmd(self.device_id, setting_type, setting_data)
        except MangoAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except MangoError as err:
            raise HomeAssistantError(f"Mango Power command failed: {err}") from err

    # ---------------------------------------------------------------- Bluetooth merge

    @callback
    def async_ble_update(self, decoded: dict[str, Any], when: float) -> None:
        """New Bluetooth data (partial updates are merged)."""
        for k, v in decoded.items():
            if not k.startswith("_") and v is not None:
                self.ble[k] = v
        self.ble_time = when
        self.ble_wall = dt_util.utcnow()
        self.async_update_listeners()

    @property
    def ble_fresh(self) -> bool:
        return self.ble_time is not None and time.monotonic() - self.ble_time < BLE_STALE_AFTER

    def ble_get(self, key: str) -> Any:
        """Bluetooth-only value, None when Bluetooth isn't current."""
        return self.ble.get(key) if self.ble_fresh else None

    def cloud_get(self, key: str) -> Any:
        if not self.last_update_success:
            return None
        if key in CLOUD_LIVE_KEYS and not self.cloud_live:
            return None
        return (self.data or {}).get(key)

    def source_of(self, key: str) -> str | None:
        if key in BLE_OVERLAY and self.ble_fresh and self.ble.get(BLE_OVERLAY[key]) is not None:
            return "bluetooth"
        if key == "batteryPower":
            return "bluetooth" if self.ble_get("batteryPower") is not None else "estimate"
        return "cloud" if self.cloud_get(key) is not None else None

    @property
    def cloud_live(self) -> bool:
        """The unit itself is reporting to the cloud (the cloud keeps the last values when it's off)."""
        d = self.data or {}
        if not self.last_update_success or not d.get("online"):
            return False
        last = d.get("lastActiveAt")
        if not last:
            return True
        ts = dt_util.parse_datetime(last)
        return ts is None or (dt_util.utcnow() - ts).total_seconds() < CLOUD_STALE_AFTER

    @property
    def merged(self) -> dict[str, Any]:
        """Cloud data with Bluetooth values laid over it where Bluetooth is current."""
        d = dict(self.data or {}) if self.last_update_success else {}
        if not self.cloud_live:
            for key in CLOUD_LIVE_KEYS:  # frozen last values from a unit that's off
                d.pop(key, None)
        if self.ble_fresh:
            for key, bkey in BLE_OVERLAY.items():
                if self.ble.get(bkey) is not None:
                    d[key] = self.ble[bkey]
            grid = self.ble.get("gridPower")
            if grid is not None:  # positive = import (unconfirmed sign), negative = export
                d["gridImportPower"] = max(grid, 0)
                d["gridExportPower"] = max(-grid, 0)
            if self.ble.get("batteryPower") is not None:
                d["batteryPower"] = self.ble["batteryPower"]
        else:
            d.pop("batteryPower", None)  # cloud value is always 0 - never use it
        return d

    @property
    def active_source(self) -> str:
        if self.ble_fresh:
            return "bluetooth"
        return "cloud" if self.last_update_success else "none"

    @callback
    def async_raw_update(self, fields: dict[str, object]) -> None:
        new = [k for k in fields if k not in self.raw]
        self.raw.update(fields)
        if new and self.raw_new_cb:
            self.raw_new_cb(new)


class MangoSiteCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Whole-site totals from the cloud: solar, home load and grid power over both phases.

    Looks like a device coordinator (sn, device_name, ...) so the shared entity classes work.
    """

    model = "site"
    model_name = "Site totals (cloud)"
    group_name: str | None = None
    firmware: str | None = None

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: MangoApi, site: dict[str, Any]) -> None:
        self.api = api
        self.site_id: str = str(site["id"])
        self.sn = f"site_{self.site_id}"
        self.device_name = f"Mango Power site {site.get('name')}" if site.get("name") else "Mango Power site"
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=f"{DOMAIN}_{self.sn}", update_interval=SCAN_INTERVAL
        )

    @property
    def tz(self) -> str:
        return self.hass.config.time_zone or "UTC"

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            rt = await self.api.site_realtime(self.site_id, self.tz)
        except MangoAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except MangoError as err:
            raise UpdateFailed(str(err)) from err
        data: dict[str, Any] = dict(rt)
        try:
            data.update(await self.api.site_energy_today(self.site_id, self.tz))
        except MangoAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except MangoError as err:  # the realtime numbers are still good
            _LOGGER.debug("Site energy today unavailable: %s", err)
        data["online"] = not rt.get("is_stale", False)
        return data
