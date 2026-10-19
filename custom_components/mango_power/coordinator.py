"""Polling coordinator: one per Mango Power unit."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import MangoApi, MangoAuthError, MangoError
from .const import DOMAIN, METER_KEYS, MODEL_E, MODEL_GW, MODEL_S, SCAN_INTERVAL, SUPPORTED_MODELS

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
