"""Base entity: groups everything under one device per Mango unit."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import MangoDeviceCoordinator


class MangoEntity(CoordinatorEntity[MangoDeviceCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: MangoDeviceCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{coordinator.sn}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.sn)},
            name=coordinator.device_name,
            manufacturer="Mango Power",
            model="Mango Power E",
            serial_number=coordinator.sn,
            sw_version=coordinator.firmware,
        )

    @property
    def value(self):
        return (self.coordinator.data or {}).get(self._key)

    @property
    def available(self) -> bool:
        return super().available and self.value is not None
