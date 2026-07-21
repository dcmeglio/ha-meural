"""Select platform for Meural integration."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import CloudDataUpdateCoordinator, LocalDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

ORIENTATION_PORTRAIT = "portrait"
ORIENTATION_LANDSCAPE = "landscape"


@dataclass(frozen=True, kw_only=True)
class MeuralSelectDescription:
    """Describe a Meural cloud-backed select setting."""

    key: str
    name: str
    unique_suffix: str
    icon: str
    options: tuple[str, ...]


SELECT_DESCRIPTIONS = (
    MeuralSelectDescription(
        key="fillMode",
        name="Image Fit Mode",
        unique_suffix="image_fit_mode",
        icon="mdi:fit-to-screen-outline",
        options=("contain", "auto crop", "as is", "stretch"),
    ),
    MeuralSelectDescription(
        key="backgroundColor",
        name="Letterbox Color",
        unique_suffix="letterbox_color",
        icon="mdi:palette-outline",
        options=("black", "grey", "white"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Meural select entities."""
    entry_data = hass.data[DOMAIN][config_entry.entry_id]
    meural = entry_data["meural"]
    cloud_coordinator: CloudDataUpdateCoordinator = entry_data["cloud_coordinator"]
    local_coordinators: dict[str, LocalDataUpdateCoordinator] = entry_data[
        "local_coordinators"
    ]

    entities = []
    for device in cloud_coordinator.data["devices"].values():
        local_coordinator = local_coordinators[str(device["id"])]
        entities.append(MeuralDisplayOrientationSelect(local_coordinator, device))
        for description in SELECT_DESCRIPTIONS:
            if description.key not in device:
                _LOGGER.debug(
                    "Meural device %s does not report %s support",
                    device["alias"],
                    description.name,
                )
                continue
            entities.append(
                MeuralSettingSelect(
                    meural,
                    cloud_coordinator,
                    device,
                    description,
                )
            )

    async_add_entities(entities)


class MeuralDisplayOrientationSelect(
    CoordinatorEntity[LocalDataUpdateCoordinator], SelectEntity
):
    """Configured display orientation of a Meural Canvas device."""

    _attr_options = [ORIENTATION_PORTRAIT, ORIENTATION_LANDSCAPE]
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:screen-rotation"

    def __init__(
        self,
        coordinator: LocalDataUpdateCoordinator,
        device: dict[str, Any],
    ) -> None:
        """Initialize the orientation select."""
        super().__init__(coordinator)
        self._device = device
        self._attr_name = f"{device['alias']} Display Orientation"
        self._attr_unique_id = f"{device['id']}_display_orientation"
        self._optimistic_option: str | None = None

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information to link this entity to the Meural device."""
        return {"identifiers": {(DOMAIN, self._device["productKey"])}}

    def _handle_coordinator_update(self) -> None:
        """Clear the optimistic option after the local coordinator updates."""
        self._optimistic_option = None
        super()._handle_coordinator_update()

    @property
    def current_option(self) -> str | None:
        """Return the configured display orientation."""
        if self._optimistic_option is not None:
            return self._optimistic_option
        if not self.coordinator.data:
            return None
        value = str(self.coordinator.data.get("orientation", "")).lower()
        return {
            "portrait": ORIENTATION_PORTRAIT,
            "vertical": ORIENTATION_PORTRAIT,
            "landscape": ORIENTATION_LANDSCAPE,
            "horizontal": ORIENTATION_LANDSCAPE,
        }.get(value)

    async def async_select_option(self, option: str) -> None:
        """Set the display orientation through the local Canvas API."""
        _LOGGER.info(
            "Meural device %s: Setting display orientation to %s",
            self._device["alias"],
            option,
        )
        if option == ORIENTATION_PORTRAIT:
            await self.coordinator.local_meural.send_set_portrait()
        else:
            await self.coordinator.local_meural.send_set_landscape()
        self._optimistic_option = option
        self.async_write_ha_state()
        await asyncio.sleep(0.5)
        await self.coordinator.async_refresh()


class MeuralSettingSelect(CoordinatorEntity[CloudDataUpdateCoordinator], SelectEntity):
    """Select-style cloud setting for a Meural Canvas device."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        meural: Any,
        coordinator: CloudDataUpdateCoordinator,
        device: dict[str, Any],
        description: MeuralSelectDescription,
    ) -> None:
        """Initialize the select entity."""
        super().__init__(coordinator)
        self.meural = meural
        self._device = device
        self._device_id = str(device["id"])
        self._description = description
        self._attr_name = f"{device['alias']} {description.name}"
        self._attr_unique_id = f"{device['id']}_{description.unique_suffix}"
        self._attr_icon = description.icon
        self._attr_options = list(description.options)

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information to link this entity to the Meural device."""
        return {"identifiers": {(DOMAIN, self._device["productKey"])}}

    @property
    def current_option(self) -> str | None:
        """Return the current cloud setting."""
        if not self.coordinator.data:
            return None
        device = self.coordinator.data.get("devices", {}).get(self._device_id)
        if not device:
            return None
        raw = device.get(self._description.key)
        if raw is None:
            return None
        value = str(raw).strip().lower()
        aliases = {
            "gray": "grey",
            "autocrop": "auto crop",
            "auto_crop": "auto crop",
            "asis": "as is",
            "as_is": "as is",
        }
        value = aliases.get(value, value)
        return value if value in self._attr_options else None

    async def async_select_option(self, option: str) -> None:
        """Set the cloud-backed option."""
        _LOGGER.info(
            "Meural device %s: Setting %s to %s",
            self._device["alias"],
            self._description.key,
            option,
        )
        await self.meural.update_device(
            self._device_id, {self._description.key: option}
        )

        if self.coordinator.data:
            device = self.coordinator.data.get("devices", {}).get(self._device_id)
            if device is not None:
                device[self._description.key] = option
                self.coordinator.async_set_updated_data(self.coordinator.data)
