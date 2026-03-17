"""Select platform for Genelec Smart IP integration."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, TYPE_CHECKING

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

if TYPE_CHECKING:
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_ENTRY_TYPE,
    CONF_ZONE_ID,
    CONF_ZONE_NAME,
    DOMAIN,
    GROUP_HUB_ID,
    ENTRY_TYPE_DEVICE,
    ENTRY_TYPE_GROUP,
    LOGGER,
    POWER_STATE_ACTIVE,
    POWER_STATE_AOIPBOOT,
    POWER_STATE_BOOT,
    POWER_STATE_ISS_SLEEP,
    POWER_STATE_PWR_FAIL,
    POWER_STATE_STANDBY,
    SENSOR_KEYS_PROFILE,
)
from .device import GenelecSmartIPDevice

_LOGGER = logging.getLogger(__name__)

POWER_STATE_API_TO_OPTION = {
    POWER_STATE_ACTIVE: "active",
    POWER_STATE_STANDBY: "standby",
    POWER_STATE_BOOT: "boot",
    POWER_STATE_AOIPBOOT: "aoipboot",
    POWER_STATE_ISS_SLEEP: "iss_sleep",
    POWER_STATE_PWR_FAIL: "pwr_fail",
}
POWER_STATE_OPTION_TO_API = {option: api for api, option in POWER_STATE_API_TO_OPTION.items()}
SETTABLE_POWER_STATES = {
    POWER_STATE_ACTIVE,
    POWER_STATE_STANDBY,
    POWER_STATE_BOOT,
    POWER_STATE_AOIPBOOT,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Genelec Smart IP select entities."""
    # Get shared data from hass.data
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
            GenelecZoneProfileSelect(hass, zone_id, zone_name)
            for zone_id, (zone_name, member_count) in sorted(zones.items())
        ])
        return

    # Use shared device instance
    device = data.device if data and data.device else None
    if not device:
        _LOGGER.error("Shared device instance not found")
        return

    # Get device info from shared data
    device_info = data.device_info if data else {}

    entities = [
        GenelecPowerStateSelect(device, device_info, coordinator),
        GenelecProfileSelect(device, device_info, coordinator),
    ]

    async_add_entities(entities)


class GenelecPowerStateSelect(CoordinatorEntity, SelectEntity):
    """Select entity for power state."""

    # Entity is enabled by default
    _attr_entity_registry_enabled_default = True

    _attr_options = list(POWER_STATE_OPTION_TO_API.keys())
    _attr_translation_key = "power_state"
    _attr_icon = "mdi:power"

    def __init__(self, device: GenelecSmartIPDevice,
                 device_info: dict[str, Any], coordinator: DataUpdateCoordinator | None = None) -> None:
        """Initialize the select entity."""
        super().__init__(coordinator)
        self._device = device
        self._device_info = device_info
        self._coordinator = coordinator
        self._attr_name = "Power State"
        self._attr_unique_id = f"{device.unique_id}_power_state"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device.unique_id)},
            "name": device_info.get("_device_name", "Genelec Device"),
            "manufacturer": "Genelec",
            "model": "Smart IP",
        }
        self._attr_has_entity_name = True
        self._current_option: str | None = POWER_STATE_API_TO_OPTION[POWER_STATE_ACTIVE]

        # Initialize from coordinator data if available
        if coordinator and coordinator.data:
            self._init_from_coordinator_data(coordinator.data)

    def _init_from_coordinator_data(self, data: dict[str, Any]) -> None:
        """Initialize from coordinator data."""
        power_data = data.get("power", {})
        state = power_data.get("state", POWER_STATE_ACTIVE)
        self._current_option = POWER_STATE_API_TO_OPTION.get(state)

    @property
    def should_poll(self) -> bool:
        """Return False as this entity is updated by the coordinator."""
        return not bool(self._coordinator)

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self._coordinator and self._coordinator.data:
            power_data = self._coordinator.data.get("power", {})
            state = power_data.get("state", POWER_STATE_ACTIVE)
            self._current_option = POWER_STATE_API_TO_OPTION.get(state)
            self.async_write_ha_state()

    def _push_power_patch(self, state: str) -> None:
        """Patch coordinator power state locally."""
        if not self._coordinator or not self._coordinator.data:
            return
        updated = dict(self._coordinator.data)
        power = dict(updated.get("power", {}))
        power["state"] = state
        updated["power"] = power
        self._coordinator.async_set_updated_data(updated)

    async def async_update(self) -> None:
        """Update the select entity (fallback when no coordinator)."""
        if self._coordinator:
            return
        try:
            power_data = await self._device.get_power_state()
            state = power_data.get("state", POWER_STATE_ACTIVE)
            self._current_option = POWER_STATE_API_TO_OPTION.get(state)
        except Exception as e:
            _LOGGER.error("Error updating power state: %s", e)
            self._current_option = None

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        api_state = POWER_STATE_OPTION_TO_API.get(option)
        if api_state is None:
            _LOGGER.warning("Unknown power state option selected: %s", option)
            return
        if api_state not in SETTABLE_POWER_STATES:
            _LOGGER.warning("Power state '%s' is read-only", option)
            return

        await self._device.set_power_state(api_state)
        self._current_option = option
        self._push_power_patch(api_state)
        self.async_write_ha_state()

    @property
    def current_option(self) -> str | None:
        """Return the selected option."""
        return self._current_option


def _build_profile_options(profile_data: dict[str, Any]) -> tuple[list[str], dict[str, int], dict[int, str]]:
    """Build profile option labels and maps from API payload."""
    profiles: dict[int, str] = {0: "Default"}

    # Always expose all API-valid profile IDs so selection still works
    # even when firmware returns an empty list payload.
    for pid in range(1, 6):
        profiles[pid] = f"Profile {pid}"

    for item in profile_data.get("list", []):
        pid = item.get("id")
        name = item.get("name")
        if isinstance(pid, int) and 0 <= pid <= 5 and isinstance(name, str) and name:
            profiles[pid] = name

    for key in ("selected", "startup"):
        pid = profile_data.get(key)
        if isinstance(pid, int) and 0 <= pid <= 5 and pid not in profiles:
            profiles[pid] = "Default" if pid == 0 else f"Profile {pid}"

    ordered = sorted(profiles.items(), key=lambda item: item[0])
    options = [f"{name} ({pid})" for pid, name in ordered]
    option_to_id = {f"{name} ({pid})": pid for pid, name in ordered}
    id_to_option = {pid: f"{name} ({pid})" for pid, name in ordered}
    return options, option_to_id, id_to_option


class GenelecProfileSelect(CoordinatorEntity, SelectEntity):
    """Select entity for active profile by profile name."""

    _attr_entity_registry_enabled_default = True
    _attr_translation_key = "profile"
    _attr_icon = "mdi:playlist-play"

    def __init__(
        self,
        device: GenelecSmartIPDevice,
        device_info: dict[str, Any],
        coordinator: DataUpdateCoordinator | None = None,
    ) -> None:
        """Initialize profile select entity."""
        super().__init__(coordinator)
        self._device = device
        self._coordinator = coordinator
        self._attr_name = "Profile"
        self._attr_unique_id = f"{device.unique_id}_profile"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device.unique_id)},
            "name": device_info.get("_device_name", "Genelec Device"),
            "manufacturer": "Genelec",
            "model": "Smart IP",
        }
        self._attr_has_entity_name = True
        self._attr_options = ["Default (0)"]
        self._option_to_id: dict[str, int] = {"Default (0)": 0}
        self._id_to_option: dict[int, str] = {0: "Default (0)"}
        self._current_option: str | None = "Default (0)"

        if coordinator and coordinator.data:
            self._update_from_profile_data(coordinator.data.get(SENSOR_KEYS_PROFILE, {}))

    @property
    def should_poll(self) -> bool:
        """Return False as this entity is updated by the coordinator."""
        return not bool(self._coordinator)

    def _update_from_profile_data(self, profile_data: dict[str, Any]) -> None:
        """Refresh options and current option from profile payload."""
        options, option_to_id, id_to_option = _build_profile_options(profile_data)
        self._attr_options = options
        self._option_to_id = option_to_id
        self._id_to_option = id_to_option

        selected_id = profile_data.get("selected")
        if isinstance(selected_id, int) and selected_id in self._id_to_option:
            self._current_option = self._id_to_option[selected_id]
        elif self._attr_options:
            self._current_option = self._attr_options[0]
        else:
            self._current_option = None

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self._coordinator and self._coordinator.data:
            self._update_from_profile_data(self._coordinator.data.get(SENSOR_KEYS_PROFILE, {}))
            self.async_write_ha_state()

    def _push_profile_patch(self, profile_id: int) -> None:
        """Patch coordinator profile selection locally."""
        if not self._coordinator or not self._coordinator.data:
            return
        updated = dict(self._coordinator.data)
        profile = dict(updated.get(SENSOR_KEYS_PROFILE, {}))
        profile["selected"] = profile_id
        updated[SENSOR_KEYS_PROFILE] = profile
        self._coordinator.async_set_updated_data(updated)

    async def async_update(self) -> None:
        """Update the select entity (fallback when no coordinator)."""
        if self._coordinator:
            return
        try:
            profile_data = await self._device.get_profile_list()
            self._update_from_profile_data(profile_data)
        except Exception as e:
            _LOGGER.error("Error updating profile select: %s", e)

    async def async_select_option(self, option: str) -> None:
        """Change active profile by selecting its profile name."""
        profile_id = self._option_to_id.get(option)
        if profile_id is None:
            _LOGGER.warning("Unknown profile option selected: %s", option)
            return

        await self._device.restore_profile(profile_id, startup=False)
        self._current_option = option
        self._push_profile_patch(profile_id)
        self.async_write_ha_state()

    @property
    def current_option(self) -> str | None:
        """Return the selected option."""
        return self._current_option


class GenelecZoneProfileSelect(SelectEntity):
    """Select entity for zone-wide profile control."""

    _attr_entity_registry_enabled_default = True
    _attr_translation_key = "zone_profile"
    _attr_icon = "mdi:playlist-play"

    def __init__(self, hass: HomeAssistant, zone_id: int, zone_name: str) -> None:
        self.hass = hass
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._attr_has_entity_name = True
        self._attr_name = f"{zone_name} Profile"
        self._attr_unique_id = f"genelec_group_zone_{zone_id}_profile"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"group_zone_{zone_id}")},
            "name": zone_name,
            "manufacturer": "Genelec",
            "model": "Zone Group",
        }
        self._attr_options = ["Default (0)"]
        self._option_to_id: dict[str, int] = {"Default (0)": 0}
        self._id_to_option: dict[int, str] = {0: "Default (0)"}
        self._current_option: str | None = "Default (0)"

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
        """Return zone profile diagnostics."""
        targets = self._zone_targets()
        return {
            "zone_id": self._zone_id,
            "zone_name": self._zone_name,
            "member_count": len(targets),
            "options_count": len(self._attr_options),
            "options": self._attr_options,
        }

    def _patch_target_profile(self, target: Any, profile_id: int) -> None:
        coordinator = getattr(target, "coordinator", None)
        if not coordinator or not coordinator.data:
            return
        updated = dict(coordinator.data)
        profile = dict(updated.get(SENSOR_KEYS_PROFILE, {}))
        profile["selected"] = profile_id
        updated[SENSOR_KEYS_PROFILE] = profile
        coordinator.async_set_updated_data(updated)

    @staticmethod
    def _merge_profile_data(candidates: list[dict[str, Any]]) -> dict[str, Any]:
        """Merge profile payloads and keep the richest profile name set."""
        merged: dict[str, Any] = {"selected": 0, "startup": 0, "list": []}
        names_by_id: dict[int, str] = {}

        for item in candidates:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("selected"), int):
                merged["selected"] = item.get("selected")
            if isinstance(item.get("startup"), int):
                merged["startup"] = item.get("startup")
            for profile in item.get("list", []):
                pid = profile.get("id")
                name = profile.get("name")
                if isinstance(pid, int) and 0 <= pid <= 5 and isinstance(name, str) and name:
                    names_by_id[pid] = name

        merged["list"] = [
            {"id": pid, "name": name}
            for pid, name in sorted(names_by_id.items(), key=lambda kv: kv[0])
        ]
        return merged

    async def async_update(self) -> None:
        targets = self._zone_targets()
        if not targets:
            self._attr_available = False
            return

        self._attr_available = True
        candidates: list[dict[str, Any]] = []
        for target in targets:
            profile_data = {}
            if target.coordinator and target.coordinator.data:
                profile_data = target.coordinator.data.get(SENSOR_KEYS_PROFILE, {})
            if profile_data:
                candidates.append(profile_data)

        has_named_profiles = any(
            isinstance(item, dict) and len(item.get("list", [])) > 0
            for item in candidates
        )
        if not has_named_profiles:
            for target in targets:
                try:
                    profile_data = await target.device.get_profile_list()
                except Exception:
                    profile_data = {}
                if profile_data:
                    candidates.append(profile_data)

        profile_data = self._merge_profile_data(candidates)

        options, option_to_id, id_to_option = _build_profile_options(profile_data)
        self._attr_options = options
        self._option_to_id = option_to_id
        self._id_to_option = id_to_option

        selected_id = profile_data.get("selected")
        if isinstance(selected_id, int) and selected_id in self._id_to_option:
            self._current_option = self._id_to_option[selected_id]
        elif self._attr_options:
            self._current_option = self._attr_options[0]
        else:
            self._current_option = None

    async def async_select_option(self, option: str) -> None:
        profile_id = self._option_to_id.get(option)
        if profile_id is None:
            return

        for target in self._zone_targets():
            await target.device.restore_profile(profile_id, startup=False)
            await asyncio.sleep(0.15)
            try:
                current = await target.device.get_profile_list()
            except Exception:
                current = {}
            selected = current.get("selected") if isinstance(current, dict) else None
            if selected != profile_id:
                await target.device.restore_profile(profile_id, startup=False)
            self._patch_target_profile(target, profile_id)

        self._current_option = option
        self.async_write_ha_state()

    @property
    def current_option(self) -> str | None:
        return self._current_option
