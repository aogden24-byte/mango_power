"""Binary sensors for Mango Power units."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import MangoConfigEntry
from .const import MODEL_E, MODEL_GW, MODEL_S
from .entity import MangoEntity

# (data key, name, device class, icon)
BINARY = (
    ("online", "Online", BinarySensorDeviceClass.CONNECTIVITY, None),
    # settings the app doesn't show (read-only)
    ("boot", "Running", BinarySensorDeviceClass.RUNNING, None),
    ("discharge240Enable", "240V mode", None, "mdi:transmission-tower"),
    ("timerMode", "Timer mode", None, "mdi:timer-cog-outline"),
)

# Mango Power S microinverter
BINARY_S = (
    ("online", "Online", BinarySensorDeviceClass.CONNECTIVITY, None),
    ("isStandby", "Standby", None, "mdi:sleep"),
    ("antiBackflow", "Anti-backflow", None, "mdi:transmission-tower-off"),
)


# Gateway Lite
BINARY_GW = (("online", "Online", BinarySensorDeviceClass.CONNECTIVITY, None),)

BINARY_BY_MODEL = {MODEL_S: BINARY_S, MODEL_GW: BINARY_GW}


async def async_setup_entry(
    hass: HomeAssistant, entry: MangoConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    ents = [
        MangoBinary(c, *spec)
        for c in entry.runtime_data.coordinators
        for spec in BINARY_BY_MODEL.get(c.model, BINARY)
    ]
    ents += [MangoBleConnected(c) for c in entry.runtime_data.coordinators if c.model == MODEL_E]
    async_add_entities(ents)


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


class MangoBleConnected(MangoEntity, BinarySensorEntity):
    """Whether the local Bluetooth link is up."""

    _attr_name = "Bluetooth connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:bluetooth"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "ble_connected")

    @property
    def is_on(self) -> bool:
        link = self.coordinator.ble_link
        return bool(link and link.connected)

    @property
    def available(self) -> bool:
        return True
