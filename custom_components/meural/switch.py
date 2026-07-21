"""Switch platform for Meural integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import CloudDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Meural switch entities."""
    entry_data = hass.data[DOMAIN][config_entry.entry_id]
    meural = entry_data["meural"]
    cloud_coordinator: CloudDataUpdateCoordinator = entry_data["cloud_coordinator"]

    entities = []
    for device in cloud_coordinator.data["devices"].values():
        if "alsEnabled" not in device:
            _LOGGER.debug(
                "Meural device %s does not report automatic brightness support",
                device["alias"],
            )
            continue
        _LOGGER.info(
            "Adding Meural automatic brightness switch for device %s",
            device["alias"],
        )
        entities.append(MeuralAutoBrightnessSwitch(meural, cloud_coordinator, device))

    async_add_entities(entities)


class MeuralAutoBrightnessSwitch(
    CoordinatorEntity[CloudDataUpdateCoordinator], SwitchEntity
):
    """Automatic brightness control for a Meural Canvas device."""

    _attr_icon = "mdi:brightness-auto"

    def __init__(
        self,
        meural: Any,
        coordinator: CloudDataUpdateCoordinator,
        device: dict[str, Any],
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self.meural = meural
        self._device = device
        self._device_id = str(device["id"])
        self._attr_name = f"{device['alias']} Auto Brightness"
        self._attr_unique_id = f"{device['id']}_auto_brightness"

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information to link this entity to the Meural device."""
        return {
            "identifiers": {(DOMAIN, self._device["productKey"])},
        }

    @property
    def is_on(self) -> bool | None:
        """Return whether automatic brightness is enabled."""
        if not self.coordinator.data:
            return None
        device = self.coordinator.data.get("devices", {}).get(self._device_id)
        if not device or "alsEnabled" not in device:
            return None
        value = device["alsEnabled"]
        if isinstance(value, str):
            return value.lower() in {"1", "true", "on", "yes"}
        return bool(value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable automatic brightness."""
        await self._async_set_auto_brightness(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable automatic brightness."""
        await self._async_set_auto_brightness(False)

    async def _async_set_auto_brightness(self, enabled: bool) -> None:
        """Update automatic brightness and reflect it immediately in Home Assistant."""
        _LOGGER.info(
            "Meural device %s: Setting automatic brightness to %s",
            self._device["alias"],
            enabled,
        )
        await self.meural.update_device(self._device_id, {"alsEnabled": enabled})

        if self.coordinator.data:
            device = self.coordinator.data.get("devices", {}).get(self._device_id)
            if device is not None:
                device["alsEnabled"] = enabled
                self.coordinator.async_set_updated_data(self.coordinator.data)
