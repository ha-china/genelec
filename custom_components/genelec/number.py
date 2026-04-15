"""Number platform for Genelec Smart IP integration."""
from __future__ import annotations

from typing import Any

from aiohttp import ClientResponseError

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .const import (
    CONF_ENTRY_TYPE,
    CONF_ZONE_ID,
    CONF_ZONE_NAME,
    ENTRY_TYPE_DEVICE,
    ENTRY_TYPE_GROUP,
    GROUP_HUB_ID,
    SINGLE_HUB_ID,
)
from .zone_helpers import get_zone_info, iter_zone_sources, resolve_zone_targets


def _iter_zone_sources(hass: HomeAssistant) -> list[Any]:
    """Return all real device data objects for zone aggregation."""
    return iter_zone_sources(hass)


def _iter_persisted_zones(hass: HomeAssistant) -> dict[int, tuple[str, int]]:
    """Return persisted zone info from Genelec Devices records."""
    zone_index = hass.data.get(DOMAIN, {}).get("_zone_index", {})
    if isinstance(zone_index, dict) and zone_index:
        zones: dict[int, tuple[str, int]] = {}
        for zone_id, record in zone_index.items():
            try:
                zid = int(zone_id)
            except (TypeError, ValueError):
                continue
            if zid <= 0:
                continue
            zone_name = str((record or {}).get("name") or f"Zone {zid}").strip()
            member_count = len((record or {}).get("members", []))
            zones[zid] = (zone_name, member_count)
        if zones:
            return zones

    zones: dict[int, tuple[str, int]] = {}
    for cfg_entry in hass.config_entries.async_entries(DOMAIN):
        devices_cfg = cfg_entry.data.get("devices", [])
        if not isinstance(devices_cfg, list):
            continue
        for device_payload in devices_cfg:
            if not isinstance(device_payload, dict):
                continue
            try:
                zone_id = int(device_payload.get(CONF_ZONE_ID))
            except (TypeError, ValueError):
                continue
            if zone_id <= 0:
                continue
            zone_name = str(device_payload.get(CONF_ZONE_NAME) or f"Zone {zone_id}").strip()
            prev_name, prev_count = zones.get(zone_id, (zone_name, 0))
            zones[zone_id] = (prev_name, prev_count + 1)
    return zones


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Genelec Smart IP number entities."""
    data = hass.data[DOMAIN].get(entry.entry_id)
    entry_type = entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_DEVICE)

    if entry_type == ENTRY_TYPE_GROUP:
        zones: dict[int, tuple[str, int]] = _iter_persisted_zones(hass)
        led_capable_zones: set[int] = set()
        for data_item in _iter_zone_sources(hass):
            zone_info = get_zone_info(data_item)
            try:
                zone_id = int(zone_info.get("zone"))
            except (TypeError, ValueError):
                continue
            if zone_id <= 0:
                continue
            zone_name = str(zone_info.get("name") or f"Zone {zone_id}")
            prev_name, prev_count = zones.get(zone_id, (zone_name, 0))
            zones[zone_id] = (prev_name, prev_count + 1)
            if getattr(data_item, "led_supported", True):
                led_capable_zones.add(zone_id)

        async_add_entities([
            GenelecZoneLedIntensityNumber(hass, zone_id, zone_name)
            for zone_id, (zone_name, member_count) in sorted(zones.items())
            if zone_id in led_capable_zones
        ])
        return

    if hasattr(data, "devices"):
        entities = [
            GenelecLedIntensityNumber(dev_data.device, dev_data.device_info or {}, dev_data.coordinator)
            for dev_data in data.devices.values()
            if getattr(dev_data, "device", None) and getattr(dev_data, "led_supported", True)
        ]
        async_add_entities(entities)
        return

    _LOGGER.error("Legacy single-device entries are no longer supported")


class _LedBase:
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER
    _attr_translation_key = "led_intensity"
    _attr_icon = "mdi:brightness-6"


class GenelecLedIntensityNumber(_LedBase, CoordinatorEntity, NumberEntity):
    """LED intensity control per speaker."""

    _attr_entity_registry_enabled_default = True

    def __init__(self, device, device_info: dict[str, Any], coordinator) -> None:
        super().__init__(coordinator)
        self._device = device
        self._coordinator = coordinator
        self._attr_name = "LED Intensity"
        self._attr_unique_id = f"{device.unique_id}_led_intensity"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device_info.get("_device_identifier", device.unique_id))},
            "name": device_info.get("_device_name", "Genelec Device"),
            "manufacturer": "Genelec",
            "model": "Smart IP",
        }
        self._attr_has_entity_name = True
        self._attr_native_value = 100.0

        if coordinator and coordinator.data:
            self._update_from_data(coordinator.data)

    def _update_from_data(self, data: dict[str, Any]) -> None:
        led = data.get("led", {})
        level = led.get("ledIntensity")
        if isinstance(level, (int, float)):
            self._attr_native_value = float(level)

    def _handle_coordinator_update(self) -> None:
        if self._coordinator and self._coordinator.data:
            self._update_from_data(self._coordinator.data)
            self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        intensity = max(0, min(100, int(value)))
        try:
            await self._device.set_led_settings(led_intensity=intensity)
        except ClientResponseError as err:
            if err.status != 404:
                raise
            self._attr_available = False
            self.async_write_ha_state()
            return
        self._attr_native_value = float(intensity)

        if self._coordinator and self._coordinator.data:
            updated = dict(self._coordinator.data)
            led = dict(updated.get("led", {}))
            led["ledIntensity"] = intensity
            updated["led"] = led
            self._coordinator.async_set_updated_data(updated)
        self.async_write_ha_state()


class GenelecZoneLedIntensityNumber(_LedBase, NumberEntity):
    """LED intensity control for all speakers in a zone."""

    _attr_entity_registry_enabled_default = True

    def __init__(self, hass: HomeAssistant, zone_id: int, zone_name: str) -> None:
        self.hass = hass
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._attr_has_entity_name = True
        self._attr_name = f"{zone_name} LED Intensity"
        self._attr_unique_id = f"genelec_group_zone_{zone_id}_led_intensity"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"group_zone_{zone_id}")},
            "name": zone_name,
            "manufacturer": "Genelec",
            "model": "Zone Group",
        }
        self._attr_native_value = 100.0

    def _zone_targets(self) -> list[Any]:
        return resolve_zone_targets(self.hass, self._zone_id, self._zone_name)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        targets = self._zone_targets()
        return {
            "zone_id": self._zone_id,
            "zone_name": self._zone_name,
            "member_count": len(targets),
        }

    async def async_update(self) -> None:
        targets = self._zone_targets()
        if not targets:
            self._attr_available = False
            return
        self._attr_available = True
        sample = targets[0]
        if sample.coordinator and sample.coordinator.data:
            led = sample.coordinator.data.get("led", {})
            level = led.get("ledIntensity")
            if isinstance(level, (int, float)):
                self._attr_native_value = float(level)

    async def async_set_native_value(self, value: float) -> None:
        intensity = max(0, min(100, int(value)))
        for target in self._zone_targets():
            if not getattr(target, "led_supported", True):
                continue
            try:
                await target.device.set_led_settings(led_intensity=intensity)
            except ClientResponseError as err:
                if err.status != 404:
                    raise
                target.led_supported = False
                continue
            coordinator = getattr(target, "coordinator", None)
            if coordinator and coordinator.data:
                updated = dict(coordinator.data)
                led = dict(updated.get("led", {}))
                led["ledIntensity"] = intensity
                updated["led"] = led
                coordinator.async_set_updated_data(updated)

        self._attr_native_value = float(intensity)
        self.async_write_ha_state()
