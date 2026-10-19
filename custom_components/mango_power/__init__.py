"""Mango Power (unofficial cloud) integration for Home Assistant."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

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
    MODEL_GW,
    MODEL_S,
    REGIONS,
    SUPPORTED_MODELS,
)
from .coordinator import MangoDeviceCoordinator, MangoSiteCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SWITCH, Platform.SELECT]


@dataclass
class MangoRuntimeData:
    api: MangoApi
    coordinators: list[MangoDeviceCoordinator]
    options: dict
    sites: list[MangoSiteCoordinator] = field(default_factory=list)


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
    gateways: list[tuple[dict, dict]] = []  # set up after the inverters they are paired with
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
        if model == MODEL_GW:
            gateways.append((dev, info))
            continue
        group = info.get("deviceGroup") or {}
        root_id = group.get("rootDeviceId")
        coord = MangoDeviceCoordinator(
            hass,
            entry,
            api,
            {
                **dev,
                "name": info.get("name") or dev.get("name"),
                "model": model,
                "group_name": group.get("name"),
                # In a Wi-Fi mesh group only the root unit talks to the Gateway Lite meter.
                "is_group_root": bool(root_id) and str(root_id) == str(dev["id"]),
            },
        )
        await coord.async_config_entry_first_refresh()
        coordinators.append(coord)

    for dev, info in gateways:
        # Pair the gateway with the root inverter that reports its meter readings: the one whose
        # meter serial matches the gateway, or the only root inverter if there is just one.
        roots = [c for c in coordinators if c.model == MODEL_S and c.is_group_root and not c.meter_via_gateway]
        source = next((c for c in roots if (c.data or {}).get("meterSN") == info.get("iotSn")), None)
        if source is None and len(roots) == 1:
            source = roots[0]
        if source is None:
            _LOGGER.info("Gateway %s: no matching inverter found, grid readings will be unavailable", dev.get("sn"))
        coord = MangoDeviceCoordinator(
            hass,
            entry,
            api,
            {
                **dev,
                "name": info.get("name") or dev.get("name"),
                "model": MODEL_GW,
                "meter_source_id": source.device_id if source else None,
            },
        )
        if source is not None:
            source.meter_via_gateway = True
        await coord.async_config_entry_first_refresh()
        coordinators.append(coord)

    # Site-wide totals (both grid phases, solar, home load). Optional: never blocks setup.
    sites: list[MangoSiteCoordinator] = []
    if any(c.model in (MODEL_S, MODEL_GW) for c in coordinators):
        try:
            for site in await api.list_sites():
                site_coord = MangoSiteCoordinator(hass, entry, api, site)
                await site_coord.async_refresh()
                if site_coord.last_update_success:
                    sites.append(site_coord)
                else:
                    _LOGGER.info("Site totals for %s are not available: %s", site.get("name"),
                                 site_coord.last_exception)
        except MangoAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except MangoError as err:
            _LOGGER.info("Could not list sites: %s", err)

    if not coordinators:
        _LOGGER.warning("No supported Mango Power devices found on this account")

    entry.runtime_data = MangoRuntimeData(
        api=api, coordinators=coordinators, options=dict(entry.options), sites=sites
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: MangoConfigEntry) -> None:
    # Token refreshes also update the entry; only reload when the options changed.
    if dict(entry.options) != entry.runtime_data.options:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: MangoConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
