"""Number platform for Genelec Smart IP integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .const import CONF_ENTRY_TYPE, CONF_ZONE_ID, CONF_ZONE_NAME, ENTRY_TYPE_DEVICE, ENTRY_TYPE_GROUP, GROUP_HUB_ID


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Genelec Smart IP number entities."""
    data = hass.data[DOMAIN].get(entry.entry_id)
    coordinator = data.coordinator if data else None
    entry_type = entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_DEVICE)

    if entry_type == ENTRY_TYPE_GROUP:
        zones: dict[int, tuple[str, int]] = {}
        for device_entry in hass.config_entries.async_entries(DOMAIN):
            if device_entry.entry_id == entry.entry_id:
                continue
            if device_entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_DEVICE) != ENTRY_TYPE_DEVICE:
                continue

            zone_id = device_entry.data.get(CONF_ZONE_ID)
            zone_name = str(device_entry.data.get(CONF_ZONE_NAME, "")).strip()
            try:
                zone_id = int(zone_id)
            except (TypeError, ValueError):
                zone_id = None
            if zone_id and zone_name:
                prev_name, prev_count = zones.get(zone_id, (zone_name, 0))
                zones[zone_id] = (prev_name, prev_count + 1)
                continue

            data_item = hass.data.get(DOMAIN, {}).get(device_entry.entry_id)
            if not data_item:
                continue
            zone_info = getattr(data_item, "zone_info", {}) or {}
            if not zone_info and getattr(data_item, "coordinator", None) and data_item.coordinator.data:
                zone_info = data_item.coordinator.data.get("zone_info", {}) or {}
            try:
                zone_id = int(zone_info.get("zone"))
            except (TypeError, ValueError):
                continue
            if zone_id <= 0:
                continue
            zone_name = str(zone_info.get("name") or f"Zone {zone_id}")
            prev_name, prev_count = zones.get(zone_id, (zone_name, 0))
            zones[zone_id] = (prev_name, prev_count + 1)

        async_add_entities([
            GenelecZoneLedIntensityNumber(hass, zone_id, zone_name)
            for zone_id, (zone_name, member_count) in sorted(zones.items())
        ])
        return

    device = data.device if data and data.device else None
    if not device:
        return

    device_info = data.device_info if data else {}
    async_add_entities([GenelecLedIntensityNumber(device, device_info, coordinator)])


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
            "identifiers": {(DOMAIN, device.unique_id)},
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
        await self._device.set_led_settings(led_intensity=intensity)
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
        targets: list[Any] = []
        expected_name = self._zone_name.strip().lower()
        for key, value in self.hass.data.get(DOMAIN, {}).items():
            if key.startswith("_"):
                continue
            zone_info = getattr(value, "zone_info", {}) or {}
            if not zone_info:
                coordinator = getattr(value, "coordinator", None)
                if coordinator and coordinator.data:
                    zone_info = coordinator.data.get("zone_info", {}) or {}
            try:
                zone_value = int(zone_info.get("zone"))
            except (TypeError, ValueError):
                zone_value = None
            zone_name = str(zone_info.get("name", "")).strip().lower()
            same_zone = zone_value == self._zone_id
            same_name = bool(expected_name) and zone_name == expected_name
            if (same_zone or same_name) and getattr(value, "device", None):
                targets.append(value)
        return targets

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
            await target.device.set_led_settings(led_intensity=intensity)
            coordinator = getattr(target, "coordinator", None)
            if coordinator and coordinator.data:
                updated = dict(coordinator.data)
                led = dict(updated.get("led", {}))
                led["ledIntensity"] = intensity
                updated["led"] = led
                coordinator.async_set_updated_data(updated)

        self._attr_native_value = float(intensity)
        self.async_write_ha_state()
