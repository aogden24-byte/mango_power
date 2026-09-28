"""Binary sensors for Mango Power units."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import MangoConfigEntry
from .entity import MangoEntity

# (data key, name, device class, icon)
BINARY = (
    ("online", "Online", BinarySensorDeviceClass.CONNECTIVITY, None),
    # settings the app doesn't show (read-only)
    ("boot", "Running", BinarySensorDeviceClass.RUNNING, None),
    ("discharge240Enable", "240V mode", None, "mdi:transmission-tower"),
    ("timerMode", "Timer mode", None, "mdi:timer-cog-outline"),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: MangoConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities(
        MangoBinary(c, *spec) for c in entry.runtime_data.coordinators for spec in BINARY
    )


class MangoBinary(MangoEntity, BinarySensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, key, name, device_class, icon) -> None:
        super().__init__(coordinator, key)
        self._attr_name = name
        self._attr_device_class = device_class
        if icon:
            self._attr_icon = icon

    @property
    def is_on(self) -> bool:
        return bool(self.value)
