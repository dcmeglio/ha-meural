"""DataUpdateCoordinator for Meural integration."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CLOUD_UPDATE_INTERVAL,
    CLOUD_UPDATE_INTERVAL_SLEEPING,
    GALLERY_UPDATE_INTERVAL,
    LOCAL_UPDATE_INTERVAL,
)
from .netgear_auth import CannotConnect, InvalidAuth
from .pymeural import DeviceTurnedOff, LocalMeural, PyMeural

_LOGGER = logging.getLogger(__name__)

LOCAL_FAILURE_WARNING_THRESHOLD = 3


def _normalize_backlight(value: Any) -> int | None:
    """Normalize a Canvas backlight response to a percentage."""
    if isinstance(value, dict):
        for key in ("backlight", "brightness", "value"):
            if key in value:
                value = value[key]
                break
        else:
            return None
    if isinstance(value, bool) or value is None:
        return None
    try:
        level = round(float(str(value).rstrip("%")))
    except (TypeError, ValueError):
        return None
    return max(0, min(100, level))


class CloudDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Class to manage fetching Meural cloud API data."""

    def __init__(
        self,
        hass: HomeAssistant,
        meural: PyMeural,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the coordinator."""
        self.meural = meural
        self.entry = entry
        self._update_interval = timedelta(seconds=CLOUD_UPDATE_INTERVAL)
        self._local_coordinators: dict[str, Any] = {}
        self._last_gallery_fetch: float = 0.0
        self._gallery_refresh_in_progress: bool = False

        super().__init__(
            hass,
            _LOGGER,
            name="Meural Cloud",
            update_interval=self._update_interval,
        )

    def register_local_coordinator(
        self, device_id: str, local_coordinator: LocalDataUpdateCoordinator
    ) -> None:
        """Register a local coordinator for sleep state tracking."""
        self._local_coordinators[device_id] = local_coordinator
        self._update_polling_interval()

    def unregister_local_coordinator(self, device_id: str) -> None:
        """Unregister a local coordinator."""
        self._local_coordinators.pop(device_id, None)
        self._update_polling_interval()

    def _update_polling_interval(self) -> None:
        """Update polling interval based on all devices' sleep states."""
        awake_count = sum(
            1 for coord in self._local_coordinators.values() if not coord.sleeping
        )
        new_interval = timedelta(
            seconds=CLOUD_UPDATE_INTERVAL
            if awake_count
            else CLOUD_UPDATE_INTERVAL_SLEEPING
        )

        if self.update_interval != new_interval:
            _LOGGER.debug(
                "Meural Cloud: Adjusting update interval to %s seconds (%d awake devices)",
                new_interval.total_seconds(),
                awake_count,
            )
            self.update_interval = new_interval

    def notify_sleep_state_changed(self) -> None:
        """Called when a local coordinator's sleep state may have changed."""
        self._update_polling_interval()

    @property
    def galleries_stale(self) -> bool:
        """Return True if gallery data should be refreshed."""
        if self._last_gallery_fetch == 0.0:
            return True
        return (time.monotonic() - self._last_gallery_fetch) > GALLERY_UPDATE_INTERVAL

    async def async_refresh_galleries(self) -> None:
        """Fetch gallery data and update coordinator data in-place.

        Called after synchronize() service, when media browser opens with stale data,
        or as a background task when the regular poll detects stale gallery data.
        """
        if self._gallery_refresh_in_progress:
            return
        self._gallery_refresh_in_progress = True
        try:
            existing = self.data or {}
            devices = list(existing.get("devices", {}).values())
            if not devices:
                return

            device_galleries_by_device: dict[str, list[dict[str, Any]]] = {}
            for device in devices:
                device_id = device["id"]
                device_galleries = await self.meural.get_device_galleries(device_id)
                device_galleries_by_device[str(device_id)] = device_galleries

            user_galleries = await self.meural.get_user_galleries()

            self._last_gallery_fetch = time.monotonic()

            if self.data:
                self.data["device_galleries"] = device_galleries_by_device
                self.data["user_galleries"] = user_galleries
                self.async_set_updated_data(self.data)

            _LOGGER.debug(
                "Meural Cloud: Gallery data refreshed (%d user galleries)",
                len(user_galleries),
            )
        except (
            InvalidAuth,
            CannotConnect,
            aiohttp.ClientError,
            asyncio.TimeoutError,
        ) as err:
            _LOGGER.warning("Meural Cloud: Failed to refresh gallery data: %s", err)
        finally:
            self._gallery_refresh_in_progress = False

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Meural cloud API."""
        try:
            # Only fetch device settings on the regular poll interval.
            # Gallery data is fetched separately via async_refresh_galleries().
            devices = await self.meural.get_user_devices()
            devices_by_id = {str(device["id"]): device for device in devices}

            # Keep the local clients on the IP currently reported by the cloud.
            # Canvas IP addresses can change after DHCP lease renewals.
            for device_id, local_coordinator in self._local_coordinators.items():
                if device := devices_by_id.get(device_id):
                    local_coordinator.update_device(device)

            # Preserve existing gallery data between polls
            existing = self.data or {}
            device_galleries = existing.get("device_galleries", {})
            user_galleries = existing.get("user_galleries", [])

            # Schedule a background gallery refresh if data is stale
            if self.galleries_stale:
                self.hass.async_create_task(self.async_refresh_galleries())

            return {
                "devices": devices_by_id,
                "device_galleries": device_galleries,
                "user_galleries": user_galleries,
            }

        except InvalidAuth as err:
            # Authentication failed - trigger reauth flow
            raise ConfigEntryAuthFailed(
                "Authentication failed. Please reauthenticate."
            ) from err
        except (CannotConnect, aiohttp.ClientError, asyncio.TimeoutError) as err:
            # Network error - raise UpdateFailed for retry
            raise UpdateFailed(
                f"Error communicating with Meural cloud API: {err}"
            ) from err
        except Exception as err:
            # Unexpected error
            _LOGGER.exception("Unexpected error updating Meural cloud data")
            raise UpdateFailed(f"Unexpected error: {err}") from err


class LocalDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Class to manage fetching Meural local device data."""

    def __init__(
        self,
        hass: HomeAssistant,
        device: dict[str, Any],
        session: aiohttp.ClientSession,
    ) -> None:
        """Initialize the coordinator."""
        self.device = device
        self.device_id = str(device["id"])
        self.local_meural = LocalMeural(device, session)
        self._sleeping = True
        self._local_failure_count = 0
        self.cloud_coordinator: CloudDataUpdateCoordinator | None = None

        super().__init__(
            hass,
            _LOGGER,
            name=f"Meural Local {device['alias']}",
            update_interval=timedelta(seconds=LOCAL_UPDATE_INTERVAL),
        )

    def update_device(self, device: dict[str, Any]) -> None:
        """Update device reference with latest cloud data."""
        self.device = device
        previous_ip = self.local_meural.ip
        if self.local_meural.update_device(device):
            _LOGGER.info(
                "Meural device %s: Local IP changed from %s to %s",
                device.get("alias", self.device_id),
                previous_ip,
                self.local_meural.ip,
            )
            self._local_failure_count = 0

    @property
    def sleeping(self) -> bool:
        """Return if device is sleeping."""
        return self._sleeping

    def set_sleeping_optimistic(self, sleeping: bool) -> None:
        """Set sleep state optimistically and notify all subscribed entities."""
        self._sleeping = sleeping
        if self.cloud_coordinator is not None:
            self.cloud_coordinator.notify_sleep_state_changed()
        self.async_update_listeners()

    async def _async_get_backlight(
        self, system_value: Any, fallback: Any = None
    ) -> int | None:
        """Get brightness from system data or the dedicated local endpoint."""
        backlight = _normalize_backlight(system_value)
        if backlight is not None:
            return backlight

        try:
            backlight = _normalize_backlight(
                await self.local_meural.send_get_backlight()
            )
        except (DeviceTurnedOff, aiohttp.ClientError, asyncio.TimeoutError):
            backlight = None

        return backlight if backlight is not None else _normalize_backlight(fallback)

    def _mark_local_update_successful(self) -> None:
        """Clear the transient local failure counter after a successful update."""
        if self._local_failure_count:
            _LOGGER.debug(
                "Meural device %s: Local connection recovered after %d failed update(s)",
                self.device.get("alias", self.device_id),
                self._local_failure_count,
            )
            self._local_failure_count = 0

    def _cached_data_after_connection_failure(self, err: Exception) -> dict[str, Any]:
        """Return cached data briefly, then mark the local coordinator unavailable."""
        self._local_failure_count += 1
        reason = str(err).strip() or err.__class__.__name__
        _LOGGER.debug(
            "Meural device %s: Local update failed %d consecutive time(s); "
            "using cached data from %s (%s)",
            self.device.get("alias", self.device_id),
            self._local_failure_count,
            self.local_meural.ip,
            reason,
        )

        if self._local_failure_count >= LOCAL_FAILURE_WARNING_THRESHOLD:
            raise UpdateFailed(
                f"Cannot reach {self.device.get('alias', self.device_id)} at "
                f"{self.local_meural.ip} after {self._local_failure_count} updates: "
                f"{reason}"
            ) from err

        cached = self.data or {}
        return {
            "sleeping": self._sleeping,
            "galleries": cached.get("galleries", []),
            "gallery_status": cached.get("gallery_status", {}),
            "gsensor": cached.get("gsensor"),
            "orientation": cached.get("orientation"),
            "lux": cached.get("lux"),
            "backlight": cached.get("backlight"),
            "free_space": cached.get("free_space"),
            "wifi_signal": cached.get("wifi_signal"),
            "version": cached.get("version"),
        }

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Meural local device API."""
        try:
            # Get sleep status
            prev_sleeping = self._sleeping
            self._sleeping = await self.local_meural.send_get_sleep()
            if prev_sleeping != self._sleeping and self.cloud_coordinator is not None:
                self.cloud_coordinator.notify_sleep_state_changed()

            if self._sleeping:
                # Device is sleeping; skip gallery fetches but still poll sensor data —
                # the local web server remains running during sleep mode.
                cached = self.data or {}
                gsensor = cached.get("gsensor")
                orientation = cached.get("orientation")
                lux = cached.get("lux")
                backlight = cached.get("backlight")
                system_backlight = None
                free_space = cached.get("free_space")
                wifi_signal = cached.get("wifi_signal")
                version = cached.get("version")
                try:
                    system_info = await self.local_meural.send_get_system()
                    gsensor = system_info.get("gsensor")
                    orientation = system_info.get("orientation")
                    lux = system_info.get("lux")
                    system_backlight = system_info.get("backlight")
                    free_space = system_info.get("free_space")
                    wifi_status = system_info.get("wifi_status", {})
                    wifi_signal = wifi_status.get("signal")
                    version = system_info.get("version")
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    pass  # Fall back to cached values initialized above
                backlight = await self._async_get_backlight(system_backlight, backlight)
                self._mark_local_update_successful()
                return {
                    "sleeping": True,
                    "galleries": cached.get("galleries", []),
                    "gallery_status": cached.get("gallery_status", {}),
                    "gsensor": gsensor,
                    "orientation": orientation,
                    "lux": lux,
                    "backlight": backlight,
                    "free_space": free_space,
                    "wifi_signal": wifi_signal,
                    "version": version,
                }

            # Device is awake, get full data
            galleries = await self.local_meural.send_get_galleries()
            gallery_status = await self.local_meural.send_get_gallery_status()

            # Get gsensor orientation for orientationMatch detection and lux for illuminance sensor.
            # Failure here is non-critical; omit the keys so callers can detect absence.
            gsensor = None
            orientation = None
            lux = None
            cached_backlight = (self.data or {}).get("backlight")
            system_backlight = None
            free_space = None
            wifi_signal = None
            version = None
            try:
                system_info = await self.local_meural.send_get_system()
                gsensor = system_info.get("gsensor")
                orientation = system_info.get("orientation")
                lux = system_info.get("lux")
                system_backlight = system_info.get("backlight")
                free_space = system_info.get("free_space")
                wifi_status = system_info.get("wifi_status", {})
                wifi_signal = wifi_status.get("signal")
                version = system_info.get("version")
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass

            backlight = await self._async_get_backlight(
                system_backlight, cached_backlight
            )

            self._mark_local_update_successful()
            return {
                "sleeping": False,
                "galleries": sorted(galleries, key=lambda i: i["name"]),
                "gallery_status": gallery_status,
                "gsensor": gsensor,
                "orientation": orientation,
                "lux": lux,
                "backlight": backlight,
                "free_space": free_space,
                "wifi_signal": wifi_signal,
                "version": version,
            }

        except (DeviceTurnedOff, aiohttp.ClientError, asyncio.TimeoutError) as err:
            # Network or connection error - preserve last known sleeping state to avoid
            # flickering between STATE_PLAYING and STATE_OFF on transient failures.
            # DeviceTurnedOff (ClientConnectorError) is also transient - the local web
            # server remains running during Meural sleep mode, so this only means the
            # device temporarily dropped off the network, not that it is genuinely sleeping.
            return self._cached_data_after_connection_failure(err)
        except Exception:
            # Unexpected error
            _LOGGER.exception(
                "Unexpected error updating Meural local device %s",
                self.device.get("alias", self.device_id),
            )
            # Don't fail integration, just return last known data
            return self.data or {
                "sleeping": True,
                "galleries": [],
                "gallery_status": {},
            }
