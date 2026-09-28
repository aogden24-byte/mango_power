"""Binary sensors for Mango Power units."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import MangoConfigEntry
from .entity import MangoEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: MangoConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities(MangoOnline(c) for c in entry.runtime_data.coordinators)


class MangoOnline(MangoEntity, BinarySensorEntity):
    _attr_name = "Online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "online")

    @property
    def is_on(self) -> bool:
        return bool(self.value)
