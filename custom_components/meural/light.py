"""Light platform for Meural integration — controls Canvas backlight brightness."""
from __future__ import annotations

import math
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
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
    """Set up Meural light entities."""
    entry_data = hass.data[DOMAIN][config_entry.entry_id]
    cloud_coordinator: CloudDataUpdateCoordinator = entry_data["cloud_coordinator"]
    local_coordinators: dict[str, LocalDataUpdateCoordinator] = entry_data["local_coordinators"]

    devices = list(cloud_coordinator.data["devices"].values())

    entities = []
    for device in devices:
        local_coordinator = local_coordinators[str(device["id"])]
        entities.append(MeuralBacklightLight(local_coordinator, device))

    async_add_entities(entities)


class MeuralBacklightLight(CoordinatorEntity[LocalDataUpdateCoordinator], LightEntity):
    """Backlight brightness control for a Meural Canvas device."""

    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    def __init__(
        self,
        coordinator: LocalDataUpdateCoordinator,
        device: dict[str, Any],
    ) -> None:
        """Initialize the light."""
        super().__init__(coordinator)
        self._device = device
        self._attr_name = f"{device['alias']} Backlight"
        self._attr_unique_id = f"{device['id']}_backlight"

    def _meural_brightness(self) -> int | None:
        """Return current backlight as a Meural value (0-100), or None if unavailable."""
        if not self.coordinator.data:
            return None
        raw = self.coordinator.data.get("backlight")
        try:
            return int(raw) if raw is not None else None
        except (ValueError, TypeError):
            return None

    @property
    def is_on(self) -> bool:
        """Return true if backlight is on (brightness > 0)."""
        level = self._meural_brightness()
        return level is not None and level > 0

    @property
    def brightness(self) -> int | None:
        """Return brightness scaled to HA range (0-255)."""
        level = self._meural_brightness()
        if level is None:
            return None
        return math.floor(level * 255 / 100)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on / set brightness."""
        ha_brightness = kwargs.get(ATTR_BRIGHTNESS)
        if ha_brightness is not None:
            meural_brightness = round(ha_brightness * 100 / 255)
        else:
            # Default to full brightness if no value given
            meural_brightness = 100
        await self.coordinator.local_meural.send_control_backlight(meural_brightness)
        await self.coordinator.async_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off backlight (set brightness to 0)."""
        await self.coordinator.local_meural.send_control_backlight(0)
        await self.coordinator.async_refresh()
