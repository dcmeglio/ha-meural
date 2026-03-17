"""Sensor platform for Meural integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import LIGHT_LUX, UnitOfInformation
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import CloudDataUpdateCoordinator, LocalDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Meural sensor entities."""
    entry_data = hass.data[DOMAIN][config_entry.entry_id]
    cloud_coordinator: CloudDataUpdateCoordinator = entry_data["cloud_coordinator"]
    local_coordinators: dict[str, LocalDataUpdateCoordinator] = entry_data["local_coordinators"]

    devices = list(cloud_coordinator.data["devices"].values())

    entities = []
    for device in devices:
        local_coordinator = local_coordinators[str(device["id"])]
        entities.append(MeuralLuxSensor(local_coordinator, device))
        entities.append(MeuralFreeSpaceSensor(local_coordinator, device))

    async_add_entities(entities)


class MeuralLuxSensor(CoordinatorEntity[LocalDataUpdateCoordinator], SensorEntity):
    """Ambient light sensor for a Meural Canvas device."""

    _attr_device_class = SensorDeviceClass.ILLUMINANCE
    _attr_native_unit_of_measurement = LIGHT_LUX
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: LocalDataUpdateCoordinator,
        device: dict[str, Any],
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._device = device
        self._attr_name = f"{device['alias']} Ambient Light"
        self._attr_unique_id = f"{device['id']}_lux"

    @property
    def native_value(self) -> float | None:
        """Return the current lux value."""
        if not self.coordinator.data:
            return None
        raw = self.coordinator.data.get("lux")
        try:
            return float(raw) if raw is not None else None
        except (ValueError, TypeError):
            return None


class MeuralFreeSpaceSensor(CoordinatorEntity[LocalDataUpdateCoordinator], SensorEntity):
    """Free storage space sensor for a Meural Canvas device."""

    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_native_unit_of_measurement = UnitOfInformation.MEGABYTES
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: LocalDataUpdateCoordinator,
        device: dict[str, Any],
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._device = device
        self._attr_name = f"{device['alias']} Free Space"
        self._attr_unique_id = f"{device['id']}_free_space"

    @property
    def native_value(self) -> int | None:
        """Return the current free space in MB."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("free_space")


