"""Mango Power (unofficial cloud) integration for Home Assistant."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import MangoApi, MangoAuthError, MangoError
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_EXPIRES_AT,
    CONF_REFRESH_TOKEN,
    CONF_REGION,
    DEFAULT_REGION,
    REGIONS,
    SUPPORTED_MODELS,
)
from .coordinator import MangoDeviceCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SWITCH, Platform.SELECT]


@dataclass
class MangoRuntimeData:
    api: MangoApi
    coordinators: list[MangoDeviceCoordinator]
    options: dict


type MangoConfigEntry = ConfigEntry[MangoRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: MangoConfigEntry) -> bool:
    session = async_get_clientsession(hass)

    def save_tokens(tokens: dict) -> None:
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_ACCESS_TOKEN: tokens["access_token"],
                CONF_REFRESH_TOKEN: tokens["refresh_token"],
                CONF_EXPIRES_AT: tokens["expires_at"],
            },
        )

    api = MangoApi(
        session,
        REGIONS.get(entry.data.get(CONF_REGION, DEFAULT_REGION), REGIONS[DEFAULT_REGION]),
        {
            "access_token": entry.data[CONF_ACCESS_TOKEN],
            "refresh_token": entry.data[CONF_REFRESH_TOKEN],
            "expires_at": entry.data.get(CONF_EXPIRES_AT, 0),
        },
        save_tokens,
    )

    try:
        devices = await api.list_devices()
    except MangoAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except MangoError as err:
        raise ConfigEntryNotReady(str(err)) from err

    coordinators: list[MangoDeviceCoordinator] = []
    for dev in devices:
        try:
            info = await api.device_info(str(dev["id"]))
        except MangoError as err:
            _LOGGER.warning("Skipping device %s: %s", dev.get("sn"), err)
            continue
        model = ((info.get("modelData") or {}).get("name") or "").lower()
        if model not in SUPPORTED_MODELS:
            _LOGGER.info("Skipping %s: model '%s' is not supported yet", dev.get("sn"), model)
            continue
        coord = MangoDeviceCoordinator(hass, entry, api, {**dev, "name": info.get("name") or dev.get("name")})
        await coord.async_config_entry_first_refresh()
        coordinators.append(coord)

    if not coordinators:
        _LOGGER.warning("No supported Mango Power devices found on this account")

    entry.runtime_data = MangoRuntimeData(api=api, coordinators=coordinators, options=dict(entry.options))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: MangoConfigEntry) -> None:
    # Token refreshes also update the entry; only reload when the options changed.
    if dict(entry.options) != entry.runtime_data.options:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: MangoConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
