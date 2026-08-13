"""Shared entity mixins for the Meural integration."""

from __future__ import annotations

from typing import Any

from .const import DOMAIN


class MeuralDeviceInfoMixin:
    """Link an entity to its Meural device, keyed by productKey.

    Mix this into any entity class that already sets self._device to the
    device dict; it works alongside any CoordinatorEntity/platform base since
    device_info has no coordinator- or platform-specific logic.
    """

    _device: dict[str, Any]

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information to link this entity to the Meural device."""
        return {"identifiers": {(DOMAIN, self._device["productKey"])}}
