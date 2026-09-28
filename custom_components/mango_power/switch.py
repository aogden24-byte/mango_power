"""Switches (AC/DC output, smart charge, UPS mode) for Mango Power units."""
from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import MangoConfigEntry
from .entity import MangoEntity

# (state key from settings, app settingType, name, icon)
SWITCHES = (
    ("acOutputEnable", "acOutput", "AC output", "mdi:power-socket-us"),
    ("dcOutputEnable", "dcOutput", "DC output", "mdi:current-dc"),
    ("chargeEnable", "smartCharge", "Smart charge", "mdi:battery-charging-wireless"),
    ("upsEnable", "upsMode", "UPS mode", "mdi:power-plug-battery"),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: MangoConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities(
        MangoSwitch(c, *spec) for c in entry.runtime_data.coordinators for spec in SWITCHES
    )


class MangoSwitch(MangoEntity, SwitchEntity):
    def __init__(self, coordinator, key: str, setting: str, name: str, icon: str) -> None:
        super().__init__(coordinator, key)
        self._setting = setting
        self._attr_name = name
        self._attr_icon = icon

    @property
    def is_on(self) -> bool:
        return bool(self.value)

    async def _set(self, on: bool) -> None:
        await self.coordinator.async_send(self._setting, {"status": on})
        # optimistic until the next poll confirms
        self.coordinator.data[self._key] = on
        self.async_write_ha_state()
        await asyncio.sleep(5)
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)
