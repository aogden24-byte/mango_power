"""Selects (max AC input current, backup SOC) for Mango Power units."""
from __future__ import annotations

import asyncio

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import MangoConfigEntry
from .const import BACKUP_SOC_OPTIONS, MAX_AC_INPUT_OPTIONS
from .entity import MangoEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: MangoConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    ents = []
    for c in entry.runtime_data.coordinators:
        ents.append(MangoSelect(c, "maxChargeCurrent", "maxACInput", "Max AC input current",
                                MAX_AC_INPUT_OPTIONS, "mdi:current-ac", str))
        ents.append(MangoSelect(c, "upsAutoChargeSoc", "backupSoc", "Backup SOC",
                                BACKUP_SOC_OPTIONS, "mdi:battery-lock", int))
    async_add_entities(ents)


class MangoSelect(MangoEntity, SelectEntity):
    def __init__(self, coordinator, key, setting, name, options, icon, cast) -> None:
        super().__init__(coordinator, key)
        self._setting = setting
        self._cast = cast  # app sends maxACInput as string, backupSoc as number
        self._attr_name = name
        self._attr_options = options
        self._attr_icon = icon

    @property
    def current_option(self) -> str | None:
        v = self.value
        if v is None:
            return None
        s = str(int(v)) if isinstance(v, (int, float)) else str(v)
        return s if s in self.options else None

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_send(self._setting, {self._setting: self._cast(option)})
        self.coordinator.data[self._key] = int(option)
        self.async_write_ha_state()
        await asyncio.sleep(5)
        await self.coordinator.async_request_refresh()
