"""Polling coordinator: one per Mango Power unit."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import MangoApi, MangoAuthError, MangoError
from .const import DOMAIN, SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)


class MangoDeviceCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetches realtime data, device state and settings for one unit."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, api: MangoApi, device: dict[str, Any]
    ) -> None:
        self.api = api
        self.device_id: str = str(device["id"])
        self.sn: str = str(device.get("sn") or device.get("displaySn") or self.device_id)
        self.device_name: str = device.get("name") or f"Mango Power E {self.sn[-3:]}"
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
            rt = await self.api.realtime(self.device_id, self.tz)
            info = await self.api.device_info(self.device_id)
            setting = await self.api.settings(self.device_id, self.tz)
        except MangoAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except MangoError as err:
            raise UpdateFailed(str(err)) from err

        state = info.get("state") or {}
        ems = setting.get("emsSetting") or {}
        self.firmware = state.get("firmwareVersion") or self.firmware
        return {
            **{k: v for k, v in rt.items() if not isinstance(v, (dict, list))},
            "soc": state.get("soc"),
            "online": state.get("status") == 1,
            "lastActiveAt": state.get("lastActiveAt"),
            "firmware": self.firmware,
            **{k: ems.get(k) for k in (
                "acOutputEnable", "dcOutputEnable", "chargeEnable", "upsEnable",
                "maxChargeCurrent", "upsAutoChargeSoc",
            )},
        }

    async def async_send(self, setting_type: str, setting_data: dict[str, Any]) -> None:
        """Send a control command, then refresh shortly after."""
        try:
            await self.api.send_cmd(self.device_id, setting_type, setting_data)
        except MangoAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except MangoError as err:
            raise HomeAssistantError(f"Mango Power command failed: {err}") from err
