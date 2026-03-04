"""Media Player platform for Genelec Smart IP integration."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, TYPE_CHECKING

from aiohttp import ClientResponseError

from homeassistant.components.media_player import MediaPlayerEntity
from homeassistant.components.media_player.const import (
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

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
    INPUT_AOIP_01,
    INPUT_AOIP_02,
    INPUT_ANALOG,
    INPUT_API_TO_DISPLAY,
    INPUT_DISPLAY_TO_API,
    INPUT_MIX,
    INPUT_NONE,
    LOGGER,
    MAX_VOLUME_DB,
    MIN_VOLUME_DB,
    POWER_STATE_ACTIVE,
    POWER_STATE_STANDBY,
)
from .device import GenelecSmartIPDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Genelec Smart IP media player entities."""
    # Get shared data from hass.data
    data = hass.data[DOMAIN].get(entry.entry_id)
    coordinator = data.coordinator if data else None
    entry_type = entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_DEVICE)

    if entry_type == ENTRY_TYPE_GROUP:
        zones: dict[int, str] = {}
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
                zones[zone_id] = zone_name
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
            zones[zone_id] = zone_name

        async_add_entities([
            GenelecZoneMediaPlayer(hass, zone_id, zone_name)
            for zone_id, zone_name in sorted(zones.items())
        ])
        return

    # Use shared device instance
    device = data.device if data and data.device else None
    if not device:
        _LOGGER.error("Shared device instance not found")
        return

    # Get device info from shared data
    device_info = data.device_info if data else {}

    async_add_entities([GenelecSmartIPMediaPlayer(device, device_info, coordinator)])


class GenelecSmartIPMediaPlayer(MediaPlayerEntity):
    """Representation of a Genelec Smart IP speaker."""

    # Entity is enabled by default
    _attr_entity_registry_enabled_default = True

    _attr_supported_features = (
        MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.SELECT_SOURCE
        | MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
    )
    _attr_volume_level = 1.0
    _attr_media_title = None
    _attr_media_artist = None
    _attr_media_album_name = None
    _attr_media_image_url = None
    _attr_media_content_id = None
    _attr_media_content_type = None

    def __init__(self, device: GenelecSmartIPDevice, device_info: dict[str, Any], coordinator: DataUpdateCoordinator | None = None) -> None:
        """Initialize the media player."""
        self._device = device
        self._device_info = device_info
        self._coordinator = coordinator
        self._attr_name = "Speaker"
        self._attr_unique_id = device.unique_id
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device.unique_id)},
            "name": device_info.get("_device_name", "Genelec Device"),
            "manufacturer": "Genelec",
            "model": "Smart IP",
        }
        self._volume = -5.0
        self._is_muted = False
        self._power_state = POWER_STATE_ACTIVE
        self._current_source = INPUT_ANALOG
        self._current_sources: list[str] = []  # Track all selected sources
        self._source_list = [
            INPUT_NONE,
            INPUT_ANALOG,
            INPUT_AOIP_01,
            INPUT_AOIP_02,
            INPUT_MIX,
        ]

        # Initialize from coordinator data if available
        if coordinator and coordinator.data:
            self._init_from_coordinator_data(coordinator.data)

    def _init_from_coordinator_data(self, data: dict[str, Any]) -> None:
        """Initialize from coordinator data."""
        volume_data = data.get("volume", {})
        power_data = data.get("power", {})
        inputs_data = data.get("inputs", {})

        if volume_data:
            self._volume = volume_data.get("level", -5.0)
            self._is_muted = volume_data.get("mute", False)
        
        if power_data:
            self._power_state = power_data.get("state", POWER_STATE_ACTIVE)
        
        inputs = inputs_data.get("input", [])
        self._current_sources = inputs
        self._current_source = self._sources_to_display(inputs)

        self._attr_state = (
            MediaPlayerState.ON
            if self._power_state == POWER_STATE_ACTIVE
            else MediaPlayerState.OFF
        )

    def _sources_to_display(self, api_sources: list[str]) -> str:
        """Convert API source list to display name."""
        if not api_sources:
            return INPUT_NONE
        if len(api_sources) > 1:
            return INPUT_MIX
        return INPUT_API_TO_DISPLAY.get(api_sources[0], api_sources[0])

    def _push_coordinator_patch(self, patch: dict[str, Any]) -> None:
        """Patch coordinator data locally to avoid extra API refresh calls."""
        if not self._coordinator or not self._coordinator.data:
            return

        updated = dict(self._coordinator.data)
        for key, value in patch.items():
            if isinstance(value, dict):
                merged = dict(updated.get(key, {}))
                merged.update(value)
                updated[key] = merged
            else:
                updated[key] = value
        self._coordinator.async_set_updated_data(updated)

    async def _set_volume_with_verify(
        self,
        *,
        level: float | None = None,
        mute: bool | None = None,
    ) -> dict[str, Any]:
        """Set volume/mute and verify by reading back current state."""
        if self._power_state != POWER_STATE_ACTIVE:
            await self._device.wake_up()
            # Wait briefly for the device to reach ACTIVE state before writing audio params.
            for _ in range(4):
                await asyncio.sleep(0.2)
                try:
                    state_data = await self._device.get_power_state()
                    self._power_state = state_data.get("state", self._power_state)
                except Exception:
                    pass
                if self._power_state == POWER_STATE_ACTIVE:
                    break

        await self._device.set_volume(level=level, mute=mute)
        await asyncio.sleep(0.12)
        current = await self._device.get_volume()

        level_ok = True
        if level is not None and isinstance(current.get("level"), (int, float)):
            level_ok = abs(float(current["level"]) - float(level)) <= 0.2

        mute_ok = True
        if mute is not None and isinstance(current.get("mute"), bool):
            mute_ok = bool(current["mute"]) == bool(mute)

        if not (level_ok and mute_ok):
            await self._device.set_volume(level=level, mute=mute)
            await asyncio.sleep(0.12)
            current = await self._device.get_volume()

        if level is not None and isinstance(current.get("level"), (int, float)):
            if abs(float(current["level"]) - float(level)) > 0.2:
                fallback_level = max(-130.0, min(0.0, float(level)))
                if abs(fallback_level - float(level)) > 0.05:
                    await self._device.set_volume(level=fallback_level, mute=mute)
                    await asyncio.sleep(0.12)
                    current = await self._device.get_volume()

        return current

    async def _set_inputs_with_verify(self, api_sources: list[str]) -> list[str]:
        """Set input sources and verify by reading back current inputs."""
        if self._power_state != POWER_STATE_ACTIVE:
            await self._device.wake_up()
            for _ in range(4):
                await asyncio.sleep(0.2)
                try:
                    state_data = await self._device.get_power_state()
                    self._power_state = state_data.get("state", self._power_state)
                except Exception:
                    pass
                if self._power_state == POWER_STATE_ACTIVE:
                    break

        try:
            await self._device.set_inputs(api_sources)
        except ClientResponseError as err:
            if err.status == 404:
                await self._device.wake_up()
                await self._device.set_inputs(api_sources)
            else:
                raise

        await asyncio.sleep(0.2)
        inputs_data = await self._device.get_inputs()
        current = inputs_data.get("input", []) if isinstance(inputs_data, dict) else []

        if list(current) != list(api_sources):
            if len(api_sources) == 1:
                await self._device.set_input_single(api_sources[0])
            else:
                await self._device.set_inputs(api_sources)
            await asyncio.sleep(0.2)
            inputs_data = await self._device.get_inputs()
            current = inputs_data.get("input", []) if isinstance(inputs_data, dict) else []

        if list(current) != list(api_sources):
            # Last try: wake again then re-apply once.
            await self._device.wake_up()
            await asyncio.sleep(0.3)
            await self._device.set_inputs(api_sources)
            await asyncio.sleep(0.2)
            inputs_data = await self._device.get_inputs()
            current = inputs_data.get("input", []) if isinstance(inputs_data, dict) else []

        return list(current)

    async def async_update(self) -> None:
        """Update the media player state."""
        if self._coordinator:
            # Use coordinator data
            coordinator_data = self._coordinator.data
            volume_data = coordinator_data.get("volume", {})
            power_data = coordinator_data.get("power", {})
            inputs_data = coordinator_data.get("inputs", {})

            self._volume = volume_data.get("level", -5.0)
            self._is_muted = volume_data.get("mute", False)
            self._power_state = power_data.get("state", POWER_STATE_STANDBY)

            inputs = inputs_data.get("input", [])
            self._current_sources = inputs
            self._current_source = self._sources_to_display(inputs)

            self._attr_state = (
                MediaPlayerState.ON
                if self._power_state == POWER_STATE_ACTIVE
                else MediaPlayerState.OFF
            )
        else:
            # Fallback to direct requests
            try:
                volume_data = await self._device.get_volume()
                self._volume = volume_data.get("level", -5.0)
                self._is_muted = volume_data.get("mute", False)

                power_data = await self._device.get_power_state()
                self._power_state = power_data.get("state", POWER_STATE_STANDBY)

                inputs_data = await self._device.get_inputs()
                inputs = inputs_data.get("input", [])
                self._current_sources = inputs
                self._current_source = self._sources_to_display(inputs)

                self._attr_state = (
                    MediaPlayerState.ON
                    if self._power_state == POWER_STATE_ACTIVE
                    else MediaPlayerState.OFF
                )
            except Exception as e:  # pylint: disable=broad-except
                _LOGGER.error("Error updating media player: %s", e)

    @property
    def source(self) -> str | None:
        """Return the current input source."""
        return self._current_source

    @property
    def source_list(self) -> list[str] | None:
        """List of available input sources."""
        return self._source_list

    @property
    def volume_level(self) -> float:
        """Volume level of the media player (0..1)."""
        span = MAX_VOLUME_DB - MIN_VOLUME_DB
        if span <= 0:
            return 0.0
        return max(0.0, min(1.0, (self._volume - MIN_VOLUME_DB) / span))

    @property
    def is_volume_muted(self) -> bool:
        """Boolean if volume is currently muted."""
        return self._is_muted

    async def async_select_source(self, source: str) -> None:
        """Select input source."""
        if source == INPUT_NONE:
            # No input - empty array
            api_sources = []
        elif source == INPUT_MIX:
            # Mix - select all inputs
            api_sources = list(INPUT_DISPLAY_TO_API.values())
        else:
            # Single source
            api_source = INPUT_DISPLAY_TO_API.get(source, source)
            api_sources = [api_source]
        
        applied = await self._set_inputs_with_verify(api_sources)
        self._current_sources = applied
        self._current_source = source
        self._push_coordinator_patch({"inputs": {"input": applied}})
        self.async_write_ha_state()

    async def async_mute_volume(self, mute: bool) -> None:
        """Mute or unmute media player."""
        current = await self._set_volume_with_verify(mute=mute)
        self._is_muted = bool(current.get("mute", mute))
        self._push_coordinator_patch({"volume": {"mute": self._is_muted}})
        self.async_write_ha_state()

    async def async_set_volume_level(self, volume: float) -> None:
        """Set volume level, range 0..1."""
        level = MIN_VOLUME_DB + (max(0.0, min(1.0, volume)) * (MAX_VOLUME_DB - MIN_VOLUME_DB))
        current = await self._set_volume_with_verify(level=level)
        applied_level = float(current.get("level", level))
        self._volume = applied_level
        self._push_coordinator_patch({"volume": {"level": applied_level}})
        self.async_write_ha_state()

    async def async_volume_up(self) -> None:
        """Volume up the media player."""
        new_level = min(0, self._volume + 1.0)
        current = await self._set_volume_with_verify(level=new_level)
        applied_level = float(current.get("level", new_level))
        self._volume = applied_level
        self._push_coordinator_patch({"volume": {"level": applied_level}})
        self.async_write_ha_state()

    async def async_volume_down(self) -> None:
        """Volume down the media player."""
        new_level = max(MIN_VOLUME_DB, self._volume - 1.0)
        current = await self._set_volume_with_verify(level=new_level)
        applied_level = float(current.get("level", new_level))
        self._volume = applied_level
        self._push_coordinator_patch({"volume": {"level": applied_level}})
        self.async_write_ha_state()

    async def async_turn_on(self) -> None:
        """Turn the media player on."""
        await self._device.wake_up()
        self._power_state = POWER_STATE_ACTIVE
        self._push_coordinator_patch({"power": {"state": POWER_STATE_ACTIVE}})
        self.async_write_ha_state()

    async def async_turn_off(self) -> None:
        """Turn the media player off."""
        await self._device.set_standby()
        self._power_state = POWER_STATE_STANDBY
        self._push_coordinator_patch({"power": {"state": POWER_STATE_STANDBY}})
        self.async_write_ha_state()


class GenelecZoneMediaPlayer(MediaPlayerEntity):
    """Virtual media player that controls all speakers in a zone."""

    _attr_supported_features = (
        MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.SELECT_SOURCE
        | MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
    )
    _attr_should_poll = True

    def __init__(self, hass: HomeAssistant, zone_id: int, zone_name: str) -> None:
        self.hass = hass
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._attr_has_entity_name = True
        self._attr_name = f"{zone_name} Group"
        self._attr_unique_id = f"genelec_group_zone_{zone_id}_media"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"group_zone_{zone_id}")},
            "name": zone_name,
            "manufacturer": "Genelec",
            "model": "Zone Group",
        }

        self._volume = -5.0
        self._is_muted = False
        self._power_state = POWER_STATE_STANDBY
        self._current_source = INPUT_NONE
        self._source_list = [INPUT_NONE, INPUT_ANALOG, INPUT_AOIP_01, INPUT_AOIP_02, INPUT_MIX]

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

    async def _wake_target_if_needed(self, target: Any) -> None:
        """Wake target if it is not ACTIVE before control commands."""
        state = None
        if target.coordinator and target.coordinator.data:
            state = (target.coordinator.data.get("power", {}) or {}).get("state")
        if state != POWER_STATE_ACTIVE:
            await target.device.wake_up()
            for _ in range(4):
                await asyncio.sleep(0.2)
                try:
                    state_data = await target.device.get_power_state()
                    state = state_data.get("state", state)
                except Exception:
                    pass
                if state == POWER_STATE_ACTIVE:
                    break
            self._patch_target(target, {"power": {"state": POWER_STATE_ACTIVE}})

    async def _set_target_volume_with_verify(
        self,
        target: Any,
        *,
        level: float | None = None,
        mute: bool | None = None,
    ) -> dict[str, Any]:
        """Set volume/mute and verify by reading device state."""
        await self._wake_target_if_needed(target)
        await target.device.set_volume(level=level, mute=mute)
        await asyncio.sleep(0.15)
        current = await target.device.get_volume()

        level_ok = True
        if level is not None and isinstance(current.get("level"), (int, float)):
            level_ok = abs(float(current["level"]) - float(level)) <= 0.2

        mute_ok = True
        if mute is not None and isinstance(current.get("mute"), bool):
            mute_ok = bool(current["mute"]) == bool(mute)

        if not (level_ok and mute_ok):
            await target.device.set_volume(level=level, mute=mute)
            await asyncio.sleep(0.15)
            current = await target.device.get_volume()

        # Some firmware builds effectively use -130..0 even when docs say -200..0.
        # If write still does not stick, retry once with -130 clamp.
        if level is not None and isinstance(current.get("level"), (int, float)):
            if abs(float(current["level"]) - float(level)) > 0.2:
                fallback_level = max(-130.0, min(0.0, float(level)))
                if abs(fallback_level - float(level)) > 0.05:
                    await target.device.set_volume(level=fallback_level, mute=mute)
                    await asyncio.sleep(0.15)
                    current = await target.device.get_volume()

        return current

    async def _set_target_inputs_with_verify(self, target: Any, api_sources: list[str]) -> list[str]:
        """Set inputs and verify by reading /audio/inputs."""
        await self._wake_target_if_needed(target)
        try:
            await target.device.set_inputs(api_sources)
        except ClientResponseError as err:
            if err.status == 404:
                await target.device.wake_up()
                await target.device.set_inputs(api_sources)
            else:
                raise

        await asyncio.sleep(0.2)
        current_inputs = await target.device.get_inputs()
        current = current_inputs.get("input", []) if isinstance(current_inputs, dict) else []
        if list(current) != list(api_sources):
            if len(api_sources) == 1:
                await target.device.set_input_single(api_sources[0])
            else:
                await target.device.set_inputs(api_sources)
            await asyncio.sleep(0.2)
            current_inputs = await target.device.get_inputs()
            current = current_inputs.get("input", []) if isinstance(current_inputs, dict) else []

        if list(current) != list(api_sources):
            await target.device.wake_up()
            await asyncio.sleep(0.3)
            await target.device.set_inputs(api_sources)
            await asyncio.sleep(0.2)
            current_inputs = await target.device.get_inputs()
            current = current_inputs.get("input", []) if isinstance(current_inputs, dict) else []

        return list(current)

    def _zone_diagnostics(self, targets: list[Any]) -> dict[str, Any]:
        """Build diagnostics payload for zone controls."""
        members: list[str] = []
        hosts: list[str] = []
        endpoints: list[str] = []
        for target in targets:
            device = getattr(target, "device", None)
            if device:
                members.append(getattr(device, "name", "unknown"))
                hosts.append(getattr(device, "_host", "unknown"))

            network = getattr(target, "network_config", {}) or {}
            if not network and target.coordinator and target.coordinator.data:
                network = target.coordinator.data.get("network_ipv4", {}) or {}
            vol_ip = network.get("volIp")
            vol_port = network.get("volPort")
            if vol_ip and vol_port:
                endpoints.append(f"{vol_ip}:{vol_port}")

        unique_endpoints = sorted(set(endpoints))
        return {
            "zone_id": self._zone_id,
            "zone_name": self._zone_name,
            "member_count": len(targets),
            "members": members,
            "hosts": hosts,
            "multicast_endpoints": unique_endpoints,
            "multicast_consistent": len(unique_endpoints) <= 1,
        }

    def _patch_target(self, target: Any, patch: dict[str, Any]) -> None:
        coordinator = getattr(target, "coordinator", None)
        if not coordinator or not coordinator.data:
            return
        updated = dict(coordinator.data)
        for key, value in patch.items():
            merged = dict(updated.get(key, {}))
            merged.update(value)
            updated[key] = merged
        coordinator.async_set_updated_data(updated)

    async def async_update(self) -> None:
        targets = self._zone_targets()
        if not targets:
            self._attr_available = False
            return

        self._attr_available = True
        sample = targets[0]
        payload = sample.coordinator.data if sample.coordinator and sample.coordinator.data else {}
        volume_data = payload.get("volume", {})
        power_data = payload.get("power", {})
        inputs_data = payload.get("inputs", {})

        self._volume = volume_data.get("level", self._volume)
        self._is_muted = volume_data.get("mute", self._is_muted)
        self._power_state = power_data.get("state", self._power_state)

        inputs = inputs_data.get("input", [])
        if not inputs:
            self._current_source = INPUT_NONE
        elif len(inputs) > 1:
            self._current_source = INPUT_MIX
        else:
            self._current_source = INPUT_API_TO_DISPLAY.get(inputs[0], inputs[0])

        self._attr_state = MediaPlayerState.ON if self._power_state == POWER_STATE_ACTIVE else MediaPlayerState.OFF

    @property
    def source(self) -> str | None:
        return self._current_source

    @property
    def source_list(self) -> list[str] | None:
        return self._source_list

    @property
    def volume_level(self) -> float:
        span = MAX_VOLUME_DB - MIN_VOLUME_DB
        if span <= 0:
            return 0.0
        return max(0.0, min(1.0, (self._volume - MIN_VOLUME_DB) / span))

    @property
    def is_volume_muted(self) -> bool:
        return self._is_muted

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return zone diagnostics to help troubleshooting."""
        return self._zone_diagnostics(self._zone_targets())

    async def async_select_source(self, source: str) -> None:
        if source == INPUT_NONE:
            api_sources = []
        elif source == INPUT_MIX:
            api_sources = list(INPUT_DISPLAY_TO_API.values())
        else:
            api_sources = [INPUT_DISPLAY_TO_API.get(source, source)]

        applied = api_sources
        for target in self._zone_targets():
            applied = await self._set_target_inputs_with_verify(target, api_sources)
            self._patch_target(target, {"inputs": {"input": applied}})

        self._current_source = source
        self.async_write_ha_state()

    async def async_mute_volume(self, mute: bool) -> None:
        for target in self._zone_targets():
            current = await self._set_target_volume_with_verify(target, mute=mute)
            self._patch_target(target, {"volume": {"mute": current.get("mute", mute)}})

        self._is_muted = mute
        self.async_write_ha_state()

    async def async_set_volume_level(self, volume: float) -> None:
        level = MIN_VOLUME_DB + (max(0.0, min(1.0, volume)) * (MAX_VOLUME_DB - MIN_VOLUME_DB))
        for target in self._zone_targets():
            current = await self._set_target_volume_with_verify(target, level=level)
            self._patch_target(target, {"volume": {"level": current.get("level", level)}})

        self._volume = level
        self.async_write_ha_state()

    async def async_turn_on(self) -> None:
        for target in self._zone_targets():
            await target.device.wake_up()
            self._patch_target(target, {"power": {"state": POWER_STATE_ACTIVE}})

        self._power_state = POWER_STATE_ACTIVE
        self._attr_state = MediaPlayerState.ON
        self.async_write_ha_state()

    async def async_turn_off(self) -> None:
        for target in self._zone_targets():
            await target.device.set_standby()
            self._patch_target(target, {"power": {"state": POWER_STATE_STANDBY}})

        self._power_state = POWER_STATE_STANDBY
        self._attr_state = MediaPlayerState.OFF
        self.async_write_ha_state()
