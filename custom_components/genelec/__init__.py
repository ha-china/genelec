"""The Genelec Smart IP integration."""
from __future__ import annotations

import asyncio
import aiohttp
import ipaddress
from datetime import timedelta
from typing import Any, TYPE_CHECKING
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_API_VERSION,
    DEFAULT_API_VERSION,
    DEFAULT_PASSWORD,
    DEFAULT_PORT,
    DEFAULT_USERNAME,
    DOMAIN,
    LOGGER,
    MAX_VOLUME_DB,
    MIN_VOLUME_DB,
    POWER_STATE_ACTIVE,
    POWER_STATE_BOOT,
    POWER_STATE_STANDBY,
    PLATFORMS,
)
from .diagnostics import (
    async_get_config_entry_diagnostics,
    async_get_device_diagnostics,
)

if TYPE_CHECKING:
    from .device import GenelecSmartIPDevice

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_HOST): cv.string,
                vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
                vol.Optional(
                    CONF_USERNAME, default=DEFAULT_USERNAME
                ): cv.string,
                vol.Optional(
                    CONF_PASSWORD, default=DEFAULT_PASSWORD
                ): cv.string,
                vol.Optional(
                    CONF_API_VERSION, default=DEFAULT_API_VERSION
                ): cv.string,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


class GenelecSmartIPData:
    """Class to hold Genelec Smart IP data."""

    def __init__(self) -> None:
        """Initialize the data."""
        self.session: aiohttp.ClientSession | None = None
        self.coordinator: DataUpdateCoordinator | None = None
        self.device: GenelecSmartIPDevice | None = None  # Shared device instance
        self.volume_data: dict = {}
        self.power_data: dict = {}
        self.inputs_data: dict = {}
        self.events_data: dict = {}
        self.device_info: dict = {}
        self.device_id: dict = {}
        self.led_data: dict = {}
        self.led_initialized: bool = False  # Track if LED endpoint exists
        self.network_config: dict = {}
        self.aoip_ipv4: dict = {}
        self.aoip_identity: dict = {}
        self.zone_info: dict = {}
        self.profile_list: dict = {}
        self.api_root: dict | None = None
        self.api_root_checked: bool = False
        self.lock = asyncio.Lock()  # Lock to ensure only one request at a time
        self.poll_tick: int = 0


type GenelecSmartIPConfigEntry = ConfigEntry[GenelecSmartIPData]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Genelec Smart IP component."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault("_services_registered", False)
    LOGGER.info("Genelec Smart IP component loaded")
    return True


async def async_setup_entry(hass: HomeAssistant,
                            entry: GenelecSmartIPConfigEntry) -> bool:
    """Set up Genelec Smart IP from a config entry."""
    LOGGER.info("Setting up Genelec Smart IP integration")

    hass.data.setdefault(DOMAIN, {})

    # Create a shared aiohttp session for this integration entry
    # Device supports max 4 connections, but we only need 1
    # Keep connections alive for reuse to avoid reconnect overhead
    connector = aiohttp.TCPConnector(
        limit=1,
        limit_per_host=1,
        force_close=True,
        enable_cleanup_closed=True,
        ttl_dns_cache=300,
    )
    timeout = aiohttp.ClientTimeout(total=10)
    session = aiohttp.ClientSession(connector=connector, timeout=timeout)

    # Store session in data
    data = GenelecSmartIPData()
    data.session = session
    hass.data[DOMAIN][entry.entry_id] = data

    # Create device instance with shared lock
    from .device import create_device_from_config_entry
    device = create_device_from_config_entry(
        entry.data, session=session, lock=data.lock
    )
    data.device = device  # Store shared device instance

    # Fetch device_id early to ensure consistent unique_id
    try:
        device_id_data = await device.get_device_id()
        data.device_id = device_id_data
        device._device_id = device_id_data  # Update device's cached device_id
    except Exception as e:
        LOGGER.warning("Failed to get device_id during setup: %s", e)

    # Fetch device_info early
    try:
        device_info_data = await device.get_device_info()
        data.device_info = device_info_data
        device._device_info = device_info_data
    except Exception as e:
        LOGGER.warning("Failed to get device_info during setup: %s", e)

    # Create coordinator for centralized updates
    async def async_update_data():
        """Fetch data from device."""
        try:
            data.poll_tick += 1
            # Fetch all data in sequence to avoid overwhelming the device
            volume_data = await device.get_volume()
            power_data = await device.get_power_state()
            power_state = power_data.get("state")
            if power_state == POWER_STATE_ACTIVE:
                inputs_data = await device.get_inputs()
            else:
                inputs_data = data.inputs_data
            if data.poll_tick % 3 == 0 or not data.events_data:
                events_data = await device.get_events()
                data.events_data = events_data
            else:
                events_data = data.events_data

            # Update cached data
            data.volume_data = volume_data
            data.power_data = power_data
            data.inputs_data = inputs_data
            data.events_data = events_data

            # Only fetch these once (they don't change often)
            # These endpoints are required and should work on all devices
            if not data.device_info:
                try:
                    data.device_info = await device.get_device_info()
                    LOGGER.debug("Device info: %s", data.device_info)
                except Exception as e:
                    LOGGER.warning("Failed to get device info: %s", e)
            if not data.device_id:
                try:
                    data.device_id = await device.get_device_id()
                    LOGGER.debug("Device ID: %s", data.device_id)
                except Exception as e:
                    LOGGER.debug("Failed to get device ID: %s", e)
            
            # Fetch LED settings once to check if endpoint exists
            if not data.led_initialized:
                try:
                    data.led_data = await device.get_led_settings()
                    data.led_initialized = True
                    LOGGER.debug("LED data: %s", data.led_data)
                except Exception as e:
                    LOGGER.debug("LED settings not available: %s", e)
                    data.led_initialized = True  # Mark as checked, even if failed

            # These endpoints may not exist on all device models
            # 404 errors are expected for devices without these features
            if not data.network_config:
                try:
                    data.network_config = await device.get_network_config()
                    LOGGER.debug("Network config: %s", data.network_config)
                except Exception as e:
                    # 404 is expected for devices without network config endpoint
                    LOGGER.debug("Network config not available: %s", e)
                    data.network_config = {}  # Set empty dict to prevent repeated attempts
            if not data.aoip_ipv4:
                try:
                    data.aoip_ipv4 = await device.get_aoip_ipv4()
                    LOGGER.debug("AoIP IPv4: %s", data.aoip_ipv4)
                except Exception as e:
                    # 404 is expected for devices without AoIP/Dante module
                    LOGGER.debug("AoIP IPv4 not available (device may not have Dante): %s", e)
                    data.aoip_ipv4 = {}  # Set empty dict to prevent repeated attempts
            if not data.aoip_identity:
                try:
                    data.aoip_identity = await device.get_aoip_identity()
                    LOGGER.debug("AoIP identity: %s", data.aoip_identity)
                except Exception as e:
                    # 404 is expected for devices without AoIP/Dante module
                    LOGGER.debug("AoIP identity not available (device may not have Dante): %s", e)
                    data.aoip_identity = {}  # Set empty dict to prevent repeated attempts
            if not data.zone_info:
                try:
                    data.zone_info = await device.get_zone_info()
                    LOGGER.debug("Zone info: %s", data.zone_info)
                except Exception as e:
                    LOGGER.debug("Zone info not available: %s", e)
                    data.zone_info = {}  # Set empty dict to prevent repeated attempts
            if not data.profile_list:
                try:
                    data.profile_list = await device.get_profile_list()
                    LOGGER.debug("Profile list: %s", data.profile_list)
                except Exception as e:
                    LOGGER.debug("Profile list not available: %s", e)
                    data.profile_list = {}  # Set empty dict to prevent repeated attempts
            if not data.api_root_checked:
                try:
                    data.api_root = await device.get_api_root()
                    LOGGER.debug("API root payload: %s", data.api_root)
                except Exception as e:
                    LOGGER.debug("API root payload not available: %s", e)
                    data.api_root = None
                finally:
                    data.api_root_checked = True

            return {
                "volume": volume_data,
                "power": power_data,
                "inputs": inputs_data,
                "events": events_data,
                "device_info": data.device_info,
                "device_id": data.device_id,
                "led": data.led_data,
                "network_ipv4": data.network_config,
                "aoip_ipv4": data.aoip_ipv4,
                "aoip_identity": data.aoip_identity,
                "zone_info": data.zone_info,
                "profile_list": data.profile_list,
                "api_root": data.api_root or {},
            }
        except aiohttp.ClientResponseError as e:
            if e.status == 503:
                LOGGER.warning("Device busy (503) while polling %s:%s. Possible extra clients or stale keepalive sessions.", entry.data.get(CONF_HOST), entry.data.get(CONF_PORT, DEFAULT_PORT))
            else:
                LOGGER.error("Error updating coordinator data: %s", e)
            # Return last known data if available
            return {
                "volume": data.volume_data,
                "power": data.power_data,
                "inputs": data.inputs_data,
                "events": data.events_data,
                "device_info": data.device_info,
                "device_id": data.device_id,
                "led": data.led_data,
                "network_ipv4": data.network_config,
                "aoip_ipv4": data.aoip_ipv4,
                "aoip_identity": data.aoip_identity,
                "zone_info": data.zone_info,
                "profile_list": data.profile_list,
                "api_root": data.api_root or {},
            }
        except Exception as e:
            LOGGER.error("Error updating coordinator data: %s", e)
            # Return last known data if available
            return {
                "volume": data.volume_data,
                "power": data.power_data,
                "inputs": data.inputs_data,
                "events": data.events_data,
                "device_info": data.device_info,
                "device_id": data.device_id,
                "led": data.led_data,
                "network_ipv4": data.network_config,
                "aoip_ipv4": data.aoip_ipv4,
                "aoip_identity": data.aoip_identity,
                "zone_info": data.zone_info,
                "profile_list": data.profile_list,
                "api_root": data.api_root or {},
            }

    async def handle_get_api_root(call):
        """Handle API root query service (/public/{version}/)."""
        entity_ids = call.data.get("entity_id", [])
        target_entry_ids = await _get_target_entry_ids_from_call(call)
        for target_entry_id in target_entry_ids:
            target_data = hass.data[DOMAIN].get(target_entry_id)
            if not target_data or not target_data.device:
                continue
            try:
                payload = await target_data.device.get_api_root()
            except aiohttp.ClientResponseError as err:
                if err.status != 404:
                    raise
                # Some firmwares do not expose /public/{version}/; use /device/info instead.
                payload = {
                    "apiVer": target_data.device_info.get("apiVer"),
                    "note": "fallback_from_device_info",
                }
            target_data.api_root = payload
            target_data.api_root_checked = True
            await _patch_coordinator(target_data, {"api_root": payload})

    coordinator = DataUpdateCoordinator(
        hass,
        LOGGER,
        name=DOMAIN,
        update_method=async_update_data,
        update_interval=timedelta(seconds=60),
    )

    data.coordinator = coordinator
    await coordinator.async_config_entry_first_refresh()

    async def _patch_coordinator(
        target_data: GenelecSmartIPData,
        patch: dict[str, Any],
    ) -> None:
        """Patch coordinator data without making extra requests."""
        if not target_data.coordinator or not target_data.coordinator.data:
            return

        updated = dict(target_data.coordinator.data)
        for key, value in patch.items():
            if isinstance(value, dict):
                merged = dict(updated.get(key, {}))
                merged.update(value)
                updated[key] = merged
            else:
                updated[key] = value
        target_data.coordinator.async_set_updated_data(updated)

    async def _resolve_multicast_endpoint(
        target_data: GenelecSmartIPData,
    ) -> tuple[str, int] | None:
        """Resolve multicast group/port from network config."""
        network = target_data.network_config
        if not network and target_data.coordinator and target_data.coordinator.data:
            network = target_data.coordinator.data.get("network_ipv4", {})

        if not network and target_data.device:
            try:
                network = await target_data.device.get_network_config()
                target_data.network_config = network
            except Exception as err:
                LOGGER.warning("Failed to fetch network config for multicast: %s", err)
                return None

        vol_ip = network.get("volIp")
        vol_port = network.get("volPort")
        if not vol_ip or not vol_port:
            LOGGER.debug("Multicast not configured on device (missing volIp/volPort)")
            return None
        if str(vol_ip) == "0.0.0.0":
            LOGGER.debug("Multicast is disabled on device (volIp=0.0.0.0)")
            return None

        try:
            ip_obj = ipaddress.ip_address(str(vol_ip))
            if not ip_obj.is_multicast:
                LOGGER.debug("Configured volIp is not multicast: %s", vol_ip)
                return None
            port = int(vol_port)
        except ValueError:
            LOGGER.debug("Invalid multicast endpoint volIp=%s volPort=%s", vol_ip, vol_port)
            return None

        return str(ip_obj), port

    async def handle_wake_up(call):
        """Handle wake up service."""
        entity_ids = call.data.get("entity_id", [])
        target_entry_ids = await _get_target_entry_ids_from_call(call)
        for target_entry_id in target_entry_ids:
            target_data = hass.data[DOMAIN].get(target_entry_id)
            if not target_data or not target_data.device:
                continue
            await target_data.device.wake_up()
            await _patch_coordinator(target_data, {"power": {"state": POWER_STATE_ACTIVE}})

    async def handle_set_standby(call):
        """Handle set standby service."""
        entity_ids = call.data.get("entity_id", [])
        target_entry_ids = await _get_target_entry_ids_from_call(call)
        for target_entry_id in target_entry_ids:
            target_data = hass.data[DOMAIN].get(target_entry_id)
            if not target_data or not target_data.device:
                continue
            await target_data.device.set_standby()
            await _patch_coordinator(target_data, {"power": {"state": POWER_STATE_STANDBY}})

    async def handle_boot_device(call):
        """Handle boot device service."""
        entity_ids = call.data.get("entity_id", [])
        target_entry_ids = await _get_target_entry_ids_from_call(call)
        for target_entry_id in target_entry_ids:
            target_data = hass.data[DOMAIN].get(target_entry_id)
            if not target_data or not target_data.device:
                continue
            await target_data.device.boot_device()
            await _patch_coordinator(target_data, {"power": {"state": POWER_STATE_BOOT}})

    async def _get_target_entry_ids(entity_ids: list[str]) -> set[str]:
        """Resolve config entry IDs from entity IDs."""
        if not entity_ids:
            return {
                key for key in hass.data.get(DOMAIN, {})
                if not key.startswith("_")
            }

        ent_reg = er.async_get(hass)
        resolved: set[str] = set()
        for entity_id in entity_ids:
            if reg_entry := ent_reg.async_get(entity_id):
                if reg_entry.config_entry_id:
                    resolved.add(reg_entry.config_entry_id)

        return resolved or {entry.entry_id}

    async def _get_target_entry_ids_from_call(call) -> set[str]:
        """Resolve targets from entity IDs and optional zone filters."""
        entity_ids = call.data.get("entity_id", [])
        zone_id_raw = call.data.get("zone_id")
        zone_name_raw = call.data.get("zone_name")

        target_entry_ids = await _get_target_entry_ids(entity_ids)
        if zone_id_raw is None and zone_name_raw is None:
            return target_entry_ids

        zone_id = int(zone_id_raw) if zone_id_raw is not None else None
        zone_name = str(zone_name_raw).strip().lower() if zone_name_raw is not None else None
        filtered: set[str] = set()

        for target_entry_id in target_entry_ids:
            target_data = hass.data[DOMAIN].get(target_entry_id)
            if not target_data:
                continue

            zone_info = target_data.zone_info
            if not zone_info and target_data.coordinator and target_data.coordinator.data:
                zone_info = target_data.coordinator.data.get("zone_info", {})
            if not zone_info and target_data.device:
                try:
                    zone_info = await target_data.device.get_zone_info()
                    target_data.zone_info = zone_info
                except Exception:
                    zone_info = {}

            zone_value = zone_info.get("zone")
            zone_label = str(zone_info.get("name", "")).strip().lower()
            if zone_id is not None and zone_value != zone_id:
                continue
            if zone_name is not None and zone_label != zone_name:
                continue
            filtered.add(target_entry_id)

        return filtered

    async def handle_set_volume_level(call):
        """Handle set volume level service."""
        entity_ids = call.data.get("entity_id", [])
        level = call.data.get("level")
        if level is None:
            return
        level = max(MIN_VOLUME_DB, min(MAX_VOLUME_DB, float(level)))
        target_entry_ids = await _get_target_entry_ids_from_call(call)
        for target_entry_id in target_entry_ids:
            target_data = hass.data[DOMAIN].get(target_entry_id)
            if not target_data or not target_data.device:
                continue
            await target_data.device.set_volume(level=level)
            await _patch_coordinator(target_data, {"volume": {"level": level}})

    async def handle_set_led_intensity(call):
        """Handle set LED intensity service."""
        entity_ids = call.data.get("entity_id", [])
        intensity = call.data.get("intensity")
        if intensity is None:
            return
        intensity = max(0, min(100, int(intensity)))
        target_entry_ids = await _get_target_entry_ids_from_call(call)

        for target_entry_id in target_entry_ids:
            target_data = hass.data[DOMAIN].get(target_entry_id)
            if not target_data or not target_data.device:
                continue
            await target_data.device.set_led_settings(led_intensity=intensity)
            await _patch_coordinator(target_data, {"led": {"ledIntensity": intensity}})

    async def handle_restore_profile(call):
        """Handle restore profile service."""
        profile_id = call.data.get("profile_id")
        if profile_id is None:
            return
        startup = bool(call.data.get("startup", False))
        profile_id = int(profile_id)
        if profile_id < 0 or profile_id > 5:
            LOGGER.warning("Invalid profile_id %s, must be 0..5", profile_id)
            return

        entity_ids = call.data.get("entity_id", [])
        target_entry_ids = await _get_target_entry_ids_from_call(call)
        for target_entry_id in target_entry_ids:
            target_data = hass.data[DOMAIN].get(target_entry_id)
            if not target_data or not target_data.device:
                continue
            await target_data.device.restore_profile(profile_id, startup)
            patch: dict[str, Any] = {"profile_list": {"selected": profile_id}}
            if startup:
                patch["profile_list"]["startup"] = profile_id
            await _patch_coordinator(target_data, patch)

    async def handle_set_network_ipv4(call):
        """Handle writing /network/ipv4 settings."""
        entity_ids = call.data.get("entity_id", [])
        mode = call.data.get("mode")
        ip_value = call.data.get("ip")
        mask = call.data.get("mask")
        gw = call.data.get("gw")

        if mode == "static" and (not ip_value or not mask or not gw):
            LOGGER.warning("mode=static requires ip, mask and gw")
            return

        target_entry_ids = await _get_target_entry_ids_from_call(call)
        for target_entry_id in target_entry_ids:
            target_data = hass.data[DOMAIN].get(target_entry_id)
            if not target_data or not target_data.device:
                continue

            await target_data.device.set_network_config(
                hostname=call.data.get("hostname"),
                mode=mode,
                ip=ip_value,
                mask=mask,
                gw=gw,
                vol_ip=call.data.get("vol_ip"),
                vol_port=call.data.get("vol_port"),
                auth=call.data.get("auth"),
            )

            patch_network: dict[str, Any] = {}
            for source_key, target_key in (
                ("hostname", "hostname"),
                ("mode", "mode"),
                ("ip", "ip"),
                ("mask", "mask"),
                ("gw", "gw"),
                ("vol_ip", "volIp"),
                ("vol_port", "volPort"),
                ("auth", "auth"),
            ):
                if source_key in call.data:
                    patch_network[target_key] = call.data[source_key]
            if patch_network:
                await _patch_coordinator(target_data, {"network_ipv4": patch_network})

    async def handle_multicast_set_volume(call):
        """Handle multicast volume command."""
        entity_ids = call.data.get("entity_id", [])
        level = float(call.data.get("level"))
        level = max(-130.0, min(0.0, level))
        target_entry_ids = await _get_target_entry_ids_from_call(call)
        for target_entry_id in target_entry_ids:
            target_data = hass.data[DOMAIN].get(target_entry_id)
            if not target_data or not target_data.device:
                continue
            endpoint = await _resolve_multicast_endpoint(target_data)
            if endpoint is None:
                continue
            ip_value, port_value = endpoint
            await target_data.device.send_multicast({"level": level}, ip_value, port_value)
            await _patch_coordinator(target_data, {"volume": {"level": level}})

    async def handle_multicast_set_mute(call):
        """Handle multicast mute command."""
        entity_ids = call.data.get("entity_id", [])
        mute = bool(call.data.get("mute", False))
        target_entry_ids = await _get_target_entry_ids_from_call(call)
        for target_entry_id in target_entry_ids:
            target_data = hass.data[DOMAIN].get(target_entry_id)
            if not target_data or not target_data.device:
                continue
            endpoint = await _resolve_multicast_endpoint(target_data)
            if endpoint is None:
                continue
            ip_value, port_value = endpoint
            await target_data.device.send_multicast({"mute": mute}, ip_value, port_value)
            await _patch_coordinator(target_data, {"volume": {"mute": mute}})

    async def handle_multicast_set_profile(call):
        """Handle multicast profile command."""
        entity_ids = call.data.get("entity_id", [])
        profile_id = int(call.data.get("profile_id"))
        if profile_id < 0 or profile_id > 5:
            LOGGER.warning("Invalid profile_id %s, must be 0..5", profile_id)
            return

        target_entry_ids = await _get_target_entry_ids_from_call(call)
        for target_entry_id in target_entry_ids:
            target_data = hass.data[DOMAIN].get(target_entry_id)
            if not target_data or not target_data.device:
                continue
            endpoint = await _resolve_multicast_endpoint(target_data)
            if endpoint is None:
                continue
            ip_value, port_value = endpoint
            await target_data.device.send_multicast({"profile": profile_id}, ip_value, port_value)
            await _patch_coordinator(target_data, {"profile_list": {"selected": profile_id}})

    async def handle_multicast_power(call):
        """Handle multicast power command."""
        entity_ids = call.data.get("entity_id", [])
        state = str(call.data.get("state", "")).upper()
        if state not in {"STANDBY", "BOOT"}:
            LOGGER.warning("Invalid multicast power state '%s', use STANDBY or BOOT", state)
            return

        target_entry_ids = await _get_target_entry_ids_from_call(call)
        for target_entry_id in target_entry_ids:
            target_data = hass.data[DOMAIN].get(target_entry_id)
            if not target_data or not target_data.device:
                continue
            endpoint = await _resolve_multicast_endpoint(target_data)
            if endpoint is None:
                continue
            ip_value, port_value = endpoint
            await target_data.device.send_multicast({"state": state}, ip_value, port_value)
            await _patch_coordinator(target_data, {"power": {"state": state}})

    if not hass.data[DOMAIN].get("_services_registered"):
        hass.services.async_register(DOMAIN, "wake_up", handle_wake_up)
        hass.services.async_register(DOMAIN, "set_standby", handle_set_standby)
        hass.services.async_register(DOMAIN, "boot_device", handle_boot_device)
        hass.services.async_register(DOMAIN, "set_volume_level", handle_set_volume_level)
        hass.services.async_register(DOMAIN, "set_led_intensity", handle_set_led_intensity)
        hass.services.async_register(DOMAIN, "restore_profile", handle_restore_profile)
        hass.services.async_register(DOMAIN, "set_network_ipv4", handle_set_network_ipv4)
        hass.services.async_register(DOMAIN, "multicast_set_volume", handle_multicast_set_volume)
        hass.services.async_register(DOMAIN, "multicast_set_mute", handle_multicast_set_mute)
        hass.services.async_register(DOMAIN, "multicast_set_profile", handle_multicast_set_profile)
        hass.services.async_register(DOMAIN, "multicast_power", handle_multicast_power)
        hass.services.async_register(DOMAIN, "get_api_root", handle_get_api_root)
        hass.data[DOMAIN]["_services_registered"] = True

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant,
                             entry: GenelecSmartIPConfigEntry) -> bool:
    """Unload a config entry."""
    LOGGER.info("Unloading Genelec Smart IP integration")

    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        data = hass.data[DOMAIN].pop(entry.entry_id, None)
        if data and hasattr(data, 'session') and data.session:
            await data.session.close()

        remaining_entries = [
            k for k in hass.data.get(DOMAIN, {})
            if not k.startswith("_")
        ]
        if not remaining_entries and hass.data[DOMAIN].get("_services_registered"):
            hass.services.async_remove(DOMAIN, "wake_up")
            hass.services.async_remove(DOMAIN, "set_standby")
            hass.services.async_remove(DOMAIN, "boot_device")
            hass.services.async_remove(DOMAIN, "set_volume_level")
            hass.services.async_remove(DOMAIN, "set_led_intensity")
            hass.services.async_remove(DOMAIN, "restore_profile")
            hass.services.async_remove(DOMAIN, "set_network_ipv4")
            hass.services.async_remove(DOMAIN, "multicast_set_volume")
            hass.services.async_remove(DOMAIN, "multicast_set_mute")
            hass.services.async_remove(DOMAIN, "multicast_set_profile")
            hass.services.async_remove(DOMAIN, "multicast_power")
            hass.services.async_remove(DOMAIN, "get_api_root")
            hass.data[DOMAIN]["_services_registered"] = False
            hass.data[DOMAIN].pop("_zone_media_entities", None)
            hass.data[DOMAIN].pop("_zone_profile_entities", None)
            hass.data[DOMAIN].pop("_zone_led_entities", None)

    return True


async def async_reload_entry(hass: HomeAssistant,
                             entry: GenelecSmartIPConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
