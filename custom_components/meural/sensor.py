"""Sensor platform for Meural integration."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

_LOGGER = logging.getLogger(__name__)

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import LIGHT_LUX, SIGNAL_STRENGTH_DECIBELS_MILLIWATT, UnitOfInformation
from homeassistant.helpers.entity import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.dt import parse_datetime

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
        _LOGGER.info("Adding Meural sensors for device %s", device["alias"])
        local_coordinator = local_coordinators[str(device["id"])]
        entities.append(MeuralLuxSensor(local_coordinator, device))
        entities.append(MeuralFreeSpaceSensor(local_coordinator, device))
        entities.append(MeuralWifiSignalSensor(local_coordinator, device))
        entities.append(MeuralLastSeenSensor(cloud_coordinator, device))

    async_add_entities(entities)


class MeuralCloudSensorBase(CoordinatorEntity[CloudDataUpdateCoordinator], SensorEntity):
    """Base class for Meural cloud-sourced sensor entities."""

    def __init__(
        self,
        coordinator: CloudDataUpdateCoordinator,
        device: dict[str, Any],
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._device = device

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information to link this entity to the Meural device."""
        return {
            "identifiers": {(DOMAIN, self._device["productKey"])},
        }


class MeuralSensorBase(CoordinatorEntity[LocalDataUpdateCoordinator], SensorEntity):
    """Base class for Meural sensor entities."""

    def __init__(
        self,
        coordinator: LocalDataUpdateCoordinator,
        device: dict[str, Any],
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._device = device

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information to link this entity to the Meural device."""
        return {
            "identifiers": {(DOMAIN, self._device["productKey"])},
        }


class MeuralLuxSensor(MeuralSensorBase):
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
        super().__init__(coordinator, device)
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


class MeuralFreeSpaceSensor(MeuralSensorBase):
    """Free storage space sensor for a Meural Canvas device."""

    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = UnitOfInformation.MEGABYTES
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: LocalDataUpdateCoordinator,
        device: dict[str, Any],
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device)
        self._attr_name = f"{device['alias']} Free Space"
        self._attr_unique_id = f"{device['id']}_free_space"

    @property
    def native_value(self) -> int | None:
        """Return the current free space in MB."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("free_space")


class MeuralWifiSignalSensor(MeuralSensorBase):
    """WiFi signal strength sensor for a Meural Canvas device."""

    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: LocalDataUpdateCoordinator,
        device: dict[str, Any],
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device)
        self._attr_name = f"{device['alias']} WiFi Signal"
        self._attr_unique_id = f"{device['id']}_wifi_signal"

    @property
    def native_value(self) -> float | None:
        """Return the current WiFi signal strength in dBm."""
        if not self.coordinator.data:
            return None
        raw = self.coordinator.data.get("wifi_signal")
        try:
            return float(raw) if raw is not None else None
        except (ValueError, TypeError):
            return None


class MeuralLastSeenSensor(MeuralCloudSensorBase):
    """Last seen timestamp sensor for a Meural Canvas device."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True

    def __init__(
        self,
        coordinator: CloudDataUpdateCoordinator,
        device: dict[str, Any],
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device)
        self._attr_name = f"{device['alias']} Last Cloud Contact"
        self._attr_unique_id = f"{device['id']}_last_seen"

    @property
    def native_value(self) -> datetime | None:
        """Return the last seen timestamp from cloud frameStatus."""
        if not self.coordinator.data:
            return None
        device = self.coordinator.data.get("devices", {}).get(str(self._device["id"]))
        if not device:
            return None
        raw = device.get("frameStatus", {}).get("lastSeen")
        if not raw:
            return None
        return parse_datetime(raw)

