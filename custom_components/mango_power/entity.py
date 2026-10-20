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
            model=coordinator.model_name,
            serial_number=coordinator.sn,
            sw_version=coordinator.firmware,
            suggested_area=coordinator.group_name,
        )

    @property
    def _src(self) -> dict:
        """Cloud data with Bluetooth overlaid (device coordinators); plain data otherwise."""
        merged = getattr(self.coordinator, "merged", None)
        return merged if merged is not None else (self.coordinator.data or {})

    @property
    def value(self):
        return self._src.get(self._key)

    @property
    def data_available(self) -> bool:
        """Cloud reachable or Bluetooth current."""
        return self.coordinator.last_update_success or getattr(self.coordinator, "ble_fresh", False)

    @property
    def available(self) -> bool:
        return self.data_available and self.value is not None
