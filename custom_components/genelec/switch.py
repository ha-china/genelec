"""Switch platform for Genelec Smart IP integration."""
from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from aiohttp import ClientResponseError
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

if TYPE_CHECKING:
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN, LOGGER, SINGLE_HUB_ID
from .device import GenelecSmartIPDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Genelec Smart IP switch entities."""
    data = hass.data[DOMAIN].get(entry.entry_id)

    if hasattr(data, "devices"):
        entities: list[SwitchEntity] = []
        for dev_data in data.devices.values():
            if not getattr(dev_data, "device", None):
                continue
            if not getattr(dev_data, "led_supported", True):
                continue
            entities.extend([
                GenelecRJ45LedsSwitch(dev_data.device, dev_data.device_info or {}, dev_data.coordinator),
                GenelecClipLedSwitch(dev_data.device, dev_data.device_info or {}, dev_data.coordinator),
            ])
        async_add_entities(entities)
        return

    _LOGGER.error("Legacy single-device entries are no longer supported")


class GenelecRJ45LedsSwitch(CoordinatorEntity, SwitchEntity):
    """Switch entity for RJ45 LED control."""

    # Entity is enabled by default
    _attr_entity_registry_enabled_default = True

    _attr_translation_key = "rj45_leds"
    _attr_icon = "mdi:led-on"

    def __init__(self, device: GenelecSmartIPDevice, device_info: dict[str, Any], coordinator: DataUpdateCoordinator | None = None) -> None:
        """Initialize the switch entity."""
        super().__init__(coordinator)
        self._device = device
        self._device_info = device_info
        self._coordinator = coordinator
        self._attr_name = "RJ45 LEDs"
        self._attr_unique_id = f"{device.unique_id}_rj45_leds"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device_info.get("_device_identifier", device.unique_id))},
            "name": device_info.get("_device_name", "Genelec Device"),
            "manufacturer": "Genelec",
            "model": "Smart IP",
        }
        self._attr_has_entity_name = True
        self._rj45_enabled = True

        # Initialize from coordinator data if available
        if coordinator and coordinator.data:
            self._init_from_coordinator_data(coordinator.data)

    def _init_from_coordinator_data(self, data: dict[str, Any]) -> None:
        """Initialize from coordinator data."""
        led_data = data.get("led", {})
        if led_data:
            # rj45Leds: true means LEDs are enabled
            self._rj45_enabled = led_data.get("rj45Leds", True)

    @property
    def should_poll(self) -> bool:
        """Return False as this entity is updated by the coordinator."""
        return not bool(self._coordinator)

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self._coordinator and self._coordinator.data:
            led_data = self._coordinator.data.get("led", {})
            if led_data:
                self._rj45_enabled = led_data.get("rj45Leds", True)
            self.async_write_ha_state()

    def _push_led_patch(self, patch: dict[str, Any]) -> None:
        """Patch coordinator LED data locally."""
        if not self._coordinator or not self._coordinator.data:
            return
        updated = dict(self._coordinator.data)
        led = dict(updated.get("led", {}))
        led.update(patch)
        updated["led"] = led
        self._coordinator.async_set_updated_data(updated)

    async def async_update(self) -> None:
        """Update the switch entity (fallback when no coordinator)."""
        if self._coordinator:
            return
        # Skip automatic update - LED endpoint may not exist
        pass

    async def async_turn_on(self) -> None:
        """Turn the switch on (enable RJ45 LEDs)."""
        try:
            await self._device.set_led_settings(rj45_leds=True)
        except ClientResponseError as err:
            if err.status != 404:
                raise
            self._attr_available = False
            self.async_write_ha_state()
            return
        self._rj45_enabled = True
        self._push_led_patch({"rj45Leds": True})
        self.async_write_ha_state()

    async def async_turn_off(self) -> None:
        """Turn the switch off (disable RJ45 LEDs)."""
        try:
            await self._device.set_led_settings(rj45_leds=False)
        except ClientResponseError as err:
            if err.status != 404:
                raise
            self._attr_available = False
            self.async_write_ha_state()
            return
        self._rj45_enabled = False
        self._push_led_patch({"rj45Leds": False})
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        """Return True if entity is on."""
        return self._rj45_enabled


class GenelecClipLedSwitch(CoordinatorEntity, SwitchEntity):
    """Switch entity for clip LED control (subwoofer only)."""

    # Entity is enabled by default
    _attr_entity_registry_enabled_default = True

    _attr_translation_key = "clip_led"
    _attr_icon = "mdi:led-variant-on"

    def __init__(self, device: GenelecSmartIPDevice, device_info: dict[str, Any], coordinator: DataUpdateCoordinator | None = None) -> None:
        """Initialize the switch entity."""
        super().__init__(coordinator)
        self._device = device
        self._device_info = device_info
        self._coordinator = coordinator
        self._attr_name = "Clip LED"
        self._attr_unique_id = f"{device.unique_id}_clip_led"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device_info.get("_device_identifier", device.unique_id))},
            "name": device_info.get("_device_name", "Genelec Device"),
            "manufacturer": "Genelec",
            "model": "Smart IP",
        }
        self._attr_has_entity_name = True
        self._clip_enabled = False

        # Initialize from coordinator data if available
        if coordinator and coordinator.data:
            self._init_from_coordinator_data(coordinator.data)

    def _init_from_coordinator_data(self, data: dict[str, Any]) -> None:
        """Initialize from coordinator data."""
        led_data = data.get("led", {})
        if led_data:
            # hideClip: false means clip LED is visible (enabled)
            # hideClip: true means clip LED is hidden (disabled)
            self._clip_enabled = not led_data.get("hideClip", True)

    @property
    def should_poll(self) -> bool:
        """Return False as this entity is updated by the coordinator."""
        return not bool(self._coordinator)

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self._coordinator and self._coordinator.data:
            led_data = self._coordinator.data.get("led", {})
            if led_data:
                # hideClip: false means clip LED is visible (enabled)
                # hideClip: true means clip LED is hidden (disabled)
                self._clip_enabled = not led_data.get("hideClip", True)
            self.async_write_ha_state()

    def _push_led_patch(self, patch: dict[str, Any]) -> None:
        """Patch coordinator LED data locally."""
        if not self._coordinator or not self._coordinator.data:
            return
        updated = dict(self._coordinator.data)
        led = dict(updated.get("led", {}))
        led.update(patch)
        updated["led"] = led
        self._coordinator.async_set_updated_data(updated)

    async def async_update(self) -> None:
        """Update the switch entity (fallback when no coordinator)."""
        if self._coordinator:
            return
        # Skip automatic update - LED endpoint may not exist
        pass

    async def async_turn_on(self) -> None:
        """Turn the switch on (enable clip LED)."""
        try:
            await self._device.set_led_settings(hide_clip=False)
        except ClientResponseError as err:
            if err.status != 404:
                raise
            self._attr_available = False
            self.async_write_ha_state()
            return
        self._clip_enabled = True
        self._push_led_patch({"hideClip": False})
        self.async_write_ha_state()

    async def async_turn_off(self) -> None:
        """Turn the switch off (disable clip LED)."""
        try:
            await self._device.set_led_settings(hide_clip=True)
        except ClientResponseError as err:
            if err.status != 404:
                raise
            self._attr_available = False
            self.async_write_ha_state()
            return
        self._clip_enabled = False
        self._push_led_patch({"hideClip": True})
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        """Return True if entity is on."""
        return self._clip_enabled
