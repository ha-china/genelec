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
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_ENTRY_TYPE,
    CONF_API_VERSION,
    CONF_DEVICES,
    CONF_DEVICE_NAME,
    CONF_ZONE_ID,
    CONF_ZONE_NAME,
    ATTR_MODEL,
    DEFAULT_API_VERSION,
    DEFAULT_PASSWORD,
    DEFAULT_PORT,
    DEFAULT_USERNAME,
    DOMAIN,
    ENTRY_TYPE_DEVICE,
    ENTRY_TYPE_GROUP,
    LOGGER,
    MAX_VOLUME_DB,
    MIN_VOLUME_DB,
    POWER_STATE_ACTIVE,
    POWER_STATE_BOOT,
    POWER_STATE_STANDBY,
    PLATFORMS,
    SINGLE_HUB_ID,
    SINGLE_HUB_NAME,
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
        self.zone_persisted: bool = False
        self.group_bootstrapped: bool = False


class GenelecDevicesHubData:
    """Container for all single Genelec devices."""

    def __init__(self) -> None:
        self.devices: dict[str, GenelecSmartIPData] = {}


def _get_persisted_devices(entry: GenelecSmartIPConfigEntry) -> list[dict[str, Any]]:
    """Return persisted single-device payloads from the hub entry."""
    devices_cfg = entry.data.get(CONF_DEVICES, [])
    return list(devices_cfg) if isinstance(devices_cfg, list) else []


def _update_persisted_device_zone(
    hass: HomeAssistant,
    entry: GenelecSmartIPConfigEntry,
    device_unique_id: str,
    zone_id: int,
    zone_name: str,
) -> bool:
    """Persist per-device zone info into the Genelec Devices entry."""
    devices = _get_persisted_devices(entry)
    changed = False
    for idx, device_payload in enumerate(devices):
        if not isinstance(device_payload, dict):
            continue
        payload_unique_id = device_payload.get("unique_id") or device_payload.get(CONF_HOST)
        if payload_unique_id != device_unique_id:
            continue
        if device_payload.get(CONF_ZONE_ID) == zone_id and device_payload.get(CONF_ZONE_NAME) == zone_name:
            return False
        updated = dict(device_payload)
        updated[CONF_ZONE_ID] = zone_id
        updated[CONF_ZONE_NAME] = zone_name
        devices[idx] = updated
        changed = True
        break
    if changed:
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_DEVICES: devices},
        )
    return changed


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

    entry_type = entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_DEVICE)

    if entry_type == ENTRY_TYPE_GROUP and entry.title != "Genelec Zone":
        hass.config_entries.async_update_entry(entry, title="Genelec Zone")

    # Group-only entry: expose zone entities without creating direct device connection.
    if entry_type == ENTRY_TYPE_GROUP:
        data = GenelecSmartIPData()
        data.zone_info = {
            "zone": entry.data.get(CONF_ZONE_ID),
            "name": entry.data.get(CONF_ZONE_NAME, ""),
        }
        hass.data[DOMAIN][entry.entry_id] = data

        group_platforms = [
            Platform.MEDIA_PLAYER,
            Platform.SELECT,
            Platform.NUMBER,
        ]
        await hass.config_entries.async_forward_entry_setups(entry, group_platforms)
        return True

    # Devices hub entry: one config entry manages all single Genelec devices.
    devices_cfg = entry.data.get(CONF_DEVICES, [])
    if entry_type == ENTRY_TYPE_DEVICE and isinstance(devices_cfg, list):
        return await _async_setup_devices_hub_entry(hass, entry, devices_cfg)

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
        device_display_name = entry.data.get(CONF_DEVICE_NAME) or entry.title or device.name
        device_info_data["_device_name"] = device_display_name
        data.device_info = device_info_data
        device._device_info = device_info_data
    except Exception as e:
        LOGGER.warning("Failed to get device_info during setup: %s", e)

    # Stable identifier shared between entities and device registry.
    # Prefer entry.unique_id (MAC-based) when available.
    if not data.device_info:
        data.device_info = {"_device_name": entry.data.get(CONF_DEVICE_NAME) or entry.title or device.name}
    data.device_info["_device_identifier"] = entry.unique_id or device.unique_id

    if entry_type == ENTRY_TYPE_DEVICE:
        dev_reg = dr.async_get(hass)
        hub_device = dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, SINGLE_HUB_ID)},
            name=SINGLE_HUB_NAME,
            manufacturer="Genelec",
            model=SINGLE_HUB_NAME,
        )

        # Hub must always be top-level.
        if hub_device.via_device_id is not None:
            dev_reg.async_update_device(hub_device.id, via_device_id=None)

        device_identifier = data.device_info.get("_device_identifier", device.unique_id)
        speaker_device = dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, device_identifier)},
            name=device_display_name,
            manufacturer="Genelec",
            model=(data.device_info or {}).get(ATTR_MODEL, "Smart IP"),
        )
        if speaker_device.via_device_id != hub_device.id:
            dev_reg.async_update_device(speaker_device.id, via_device_id=hub_device.id)

    # Create coordinator for centralized updates
    async def async_update_data():
        """Fetch data from device."""
        try:
            data.poll_tick += 1
            # Fetch all data in sequence to avoid overwhelming the device
            volume_data = await device.get_volume()
            power_data = await device.get_power_state()
            inputs_data = await device.get_inputs()
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
            if data.poll_tick % 2 == 0 or not data.zone_info:
                try:
                    latest_zone = await device.get_zone_info()
                    if isinstance(latest_zone, dict) and latest_zone:
                        data.zone_info = latest_zone
                    LOGGER.debug("Zone info: %s", data.zone_info)
                except Exception as e:
                    LOGGER.debug("Zone info not available: %s", e)
                    if not data.zone_info:
                        data.zone_info = {}

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

            if entry_type == ENTRY_TYPE_DEVICE and data.zone_info:
                zone_id = data.zone_info.get("zone")
                zone_name = str(data.zone_info.get("name", "")).strip()
                if isinstance(zone_id, int) and zone_id > 0 and zone_name:
                    zone_changed = False
                    if entry.data.get(CONF_ZONE_ID) != zone_id or entry.data.get(CONF_ZONE_NAME) != zone_name:
                        updated_entry_data = dict(entry.data)
                        updated_entry_data[CONF_ZONE_ID] = zone_id
                        updated_entry_data[CONF_ZONE_NAME] = zone_name
                        hass.config_entries.async_update_entry(entry, data=updated_entry_data)
                        zone_changed = True
                    data.zone_persisted = True

                    if zone_changed:
                        for cfg_entry in hass.config_entries.async_entries(DOMAIN):
                            if cfg_entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_GROUP:
                                await hass.config_entries.async_reload(cfg_entry.entry_id)

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

            # Auto-bootstrap group hub entry once
            zone_id = data.zone_info.get("zone")
            zone_name = str(data.zone_info.get("name", "")).strip()
            if not data.group_bootstrapped and isinstance(zone_id, int) and zone_id > 0 and zone_name:
                await hass.config_entries.flow.async_init(
                    DOMAIN,
                    context={"source": "import"},
                    data={
                        CONF_ENTRY_TYPE: ENTRY_TYPE_GROUP,
                        CONF_ZONE_ID: zone_id,
                        CONF_ZONE_NAME: zone_name,
                    },
                )
                data.group_bootstrapped = True

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
        for target_data in await _resolve_target_datas(call.data.get("entity_id", []), call.data.get("device_id", [])):
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
        config_entry=entry,
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

    def _iter_device_datas() -> list[GenelecSmartIPData]:
        return [
            value for key, value in hass.data.get(DOMAIN, {}).items()
            if not key.startswith("_") and getattr(value, "device", None)
        ]

    async def _resolve_target_datas(entity_ids: list[str] | None = None, device_ids: list[str] | None = None) -> list[GenelecSmartIPData]:
        entity_ids = entity_ids or []
        device_ids = device_ids or []
        if not entity_ids and not device_ids:
            return _iter_device_datas()

        ent_reg = er.async_get(hass)
        target_device_ids: set[str] = set(device_ids)
        for entity_id in entity_ids:
            if reg_entry := ent_reg.async_get(entity_id):
                if reg_entry.device_id:
                    target_device_ids.add(reg_entry.device_id)

        resolved: list[GenelecSmartIPData] = []
        for value in _iter_device_datas():
            unique_id = (value.device_info or {}).get("_device_identifier")
            dev_reg = dr.async_get(hass)
            dev_entry = dev_reg.async_get_device(identifiers={(DOMAIN, unique_id)}) if unique_id else None
            if dev_entry and dev_entry.id in target_device_ids:
                resolved.append(value)
        return resolved

    async def handle_wake_up(call):
        """Handle wake up service."""
        for target_data in await _resolve_target_datas(call.data.get("entity_id", []), call.data.get("device_id", [])):
            await target_data.device.wake_up()
            await _patch_coordinator(target_data, {"power": {"state": POWER_STATE_ACTIVE}})

    async def handle_set_standby(call):
        """Handle set standby service."""
        for target_data in await _resolve_target_datas(call.data.get("entity_id", []), call.data.get("device_id", [])):
            await target_data.device.set_standby()
            await _patch_coordinator(target_data, {"power": {"state": POWER_STATE_STANDBY}})

    async def handle_boot_device(call):
        """Handle boot device service."""
        for target_data in await _resolve_target_datas(call.data.get("entity_id", []), call.data.get("device_id", [])):
            await target_data.device.boot_device()
            await _patch_coordinator(target_data, {"power": {"state": POWER_STATE_BOOT}})

    async def _get_target_datas_from_call(call) -> list[GenelecSmartIPData]:
        targets = await _resolve_target_datas(call.data.get("entity_id", []), call.data.get("device_id", []))
        zone_id_raw = call.data.get("zone_id")
        zone_name_raw = call.data.get("zone_name")
        if zone_id_raw is None and zone_name_raw is None:
            return targets

        zone_id = int(zone_id_raw) if zone_id_raw is not None else None
        zone_name = str(zone_name_raw).strip().lower() if zone_name_raw is not None else None
        filtered: list[GenelecSmartIPData] = []
        for target_data in targets:
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
            filtered.append(target_data)
        return filtered

    async def handle_set_volume_level(call):
        """Handle set volume level service."""
        has_entity = bool(call.data.get("entity_id"))
        has_device = bool(call.data.get("device_id"))
        has_zone = call.data.get("zone_id") is not None or call.data.get("zone_name") is not None
        if not has_entity and not has_device and not has_zone:
            LOGGER.warning("set_volume_level requires entity_id, zone_id, or zone_name")
            return

        level = call.data.get("level")
        if level is None:
            return
        level = max(MIN_VOLUME_DB, min(MAX_VOLUME_DB, float(level)))
        target_datas = await _get_target_datas_from_call(call)
        if not target_datas:
            LOGGER.warning("set_volume_level did not resolve any target entries")
            return
        for target_data in target_datas:

            # Ensure ACTIVE before applying sensitivity.
            try:
                power = await target_data.device.get_power_state()
                if power.get("state") != POWER_STATE_ACTIVE:
                    await target_data.device.set_power_state(POWER_STATE_ACTIVE)
                    await asyncio.sleep(0.6)
            except Exception:
                await target_data.device.wake_up()
                await asyncio.sleep(0.6)

            await target_data.device.set_volume(level=level)
            await asyncio.sleep(0.15)
            current = await target_data.device.get_volume()
            applied = float(current.get("level", level))

            # Firmware compatibility fallback to -130..0 if needed.
            if abs(applied - float(level)) > 0.2:
                fallback_level = max(-130.0, min(0.0, float(level)))
                if abs(fallback_level - float(level)) > 0.05:
                    await target_data.device.set_volume(level=fallback_level)
                    await asyncio.sleep(0.15)
                    current = await target_data.device.get_volume()
                    applied = float(current.get("level", fallback_level))

            await _patch_coordinator(target_data, {"volume": {"level": applied}})

    async def handle_set_led_intensity(call):
        """Handle set LED intensity service."""
        intensity = call.data.get("intensity")
        if intensity is None:
            return
        intensity = max(0, min(100, int(intensity)))
        for target_data in await _resolve_target_datas(call.data.get("entity_id", []), call.data.get("device_id", [])):
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

        for target_data in await _resolve_target_datas(call.data.get("entity_id", []), call.data.get("device_id", [])):
            await target_data.device.restore_profile(profile_id, startup)
            patch: dict[str, Any] = {"profile_list": {"selected": profile_id}}
            if startup:
                patch["profile_list"]["startup"] = profile_id
            await _patch_coordinator(target_data, patch)

    async def handle_set_network_ipv4(call):
        """Handle writing /network/ipv4 settings."""
        mode = call.data.get("mode")
        ip_value = call.data.get("ip")
        mask = call.data.get("mask")
        gw = call.data.get("gw")

        if mode == "static" and (not ip_value or not mask or not gw):
            LOGGER.warning("mode=static requires ip, mask and gw")
            return

        for target_data in await _resolve_target_datas(call.data.get("entity_id", []), call.data.get("device_id", [])):

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
        level = float(call.data.get("level"))
        level = max(-130.0, min(0.0, level))
        for target_data in await _get_target_datas_from_call(call):
            endpoint = await _resolve_multicast_endpoint(target_data)
            if endpoint is None:
                continue
            ip_value, port_value = endpoint
            await target_data.device.send_multicast({"level": level}, ip_value, port_value)
            await _patch_coordinator(target_data, {"volume": {"level": level}})

    async def handle_multicast_set_mute(call):
        """Handle multicast mute command."""
        mute = bool(call.data.get("mute", False))
        for target_data in await _get_target_datas_from_call(call):
            endpoint = await _resolve_multicast_endpoint(target_data)
            if endpoint is None:
                continue
            ip_value, port_value = endpoint
            await target_data.device.send_multicast({"mute": mute}, ip_value, port_value)
            await _patch_coordinator(target_data, {"volume": {"mute": mute}})

    async def handle_multicast_set_profile(call):
        """Handle multicast profile command."""
        profile_id = int(call.data.get("profile_id"))
        if profile_id < 0 or profile_id > 5:
            LOGGER.warning("Invalid profile_id %s, must be 0..5", profile_id)
            return

        for target_data in await _get_target_datas_from_call(call):
            endpoint = await _resolve_multicast_endpoint(target_data)
            if endpoint is None:
                continue
            ip_value, port_value = endpoint
            await target_data.device.send_multicast({"profile": profile_id}, ip_value, port_value)
            await _patch_coordinator(target_data, {"profile_list": {"selected": profile_id}})

    async def handle_multicast_power(call):
        """Handle multicast power command."""
        state = str(call.data.get("state", "")).upper()
        if state not in {"STANDBY", "BOOT"}:
            LOGGER.warning("Invalid multicast power state '%s', use STANDBY or BOOT", state)
            return

        for target_data in await _get_target_datas_from_call(call):
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


async def _async_setup_devices_hub_entry(
    hass: HomeAssistant,
    entry: GenelecSmartIPConfigEntry,
    devices_cfg: list[dict[str, Any]],
) -> bool:
    """Set up the single top-level Genelec Devices entry."""
    hass.data.setdefault(DOMAIN, {})

    hub_data = GenelecDevicesHubData()
    hass.data[DOMAIN][entry.entry_id] = hub_data

    dev_reg = dr.async_get(hass)

    from .device import create_device_from_config_entry

    async def _setup_one_device(raw_cfg: dict[str, Any]) -> None:
        if not isinstance(raw_cfg, dict):
            return

        device_cfg = dict(raw_cfg)
        device_unique_id = device_cfg.get("unique_id") or device_cfg.get(CONF_HOST)
        if not isinstance(device_unique_id, str) or not device_unique_id:
            return

        connector = aiohttp.TCPConnector(
            limit=1,
            limit_per_host=1,
            force_close=True,
            enable_cleanup_closed=True,
            ttl_dns_cache=300,
        )
        timeout = aiohttp.ClientTimeout(total=10)
        session = aiohttp.ClientSession(connector=connector, timeout=timeout)

        data = GenelecSmartIPData()
        data.session = session
        hub_data.devices[device_unique_id] = data
        hass.data[DOMAIN][device_unique_id] = data

        device = create_device_from_config_entry(device_cfg, session=session, lock=data.lock)
        data.device = device

        try:
            device_id_data = await device.get_device_id()
            data.device_id = device_id_data
            device._device_id = device_id_data
        except Exception as e:
            LOGGER.warning("Failed to get device_id during hub setup: %s", e)

        device_display_name = device_cfg.get(CONF_DEVICE_NAME) or device_cfg.get(CONF_HOST) or device.name
        try:
            device_info_data = await device.get_device_info()
            device_info_data["_device_name"] = device_display_name
            data.device_info = device_info_data
            device._device_info = device_info_data
        except Exception as e:
            LOGGER.warning("Failed to get device_info during hub setup: %s", e)
            data.device_info = {"_device_name": device_display_name}

        data.device_info["_device_identifier"] = device_unique_id

        speaker_device = dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, device_unique_id)},
            name=device_display_name,
            manufacturer="Genelec",
            model=(data.device_info or {}).get(ATTR_MODEL, "Smart IP"),
        )
        if speaker_device.via_device_id is not None:
            dev_reg.async_update_device(speaker_device.id, via_device_id=None)

        async def _make_update(target_data: GenelecSmartIPData, target_device: GenelecSmartIPDevice):
            async def async_update_data() -> dict[str, Any]:
                try:
                    target_data.poll_tick += 1
                    volume_data = await target_device.get_volume()
                    power_data = await target_device.get_power_state()
                    inputs_data = await target_device.get_inputs()
                    if target_data.poll_tick % 3 == 0 or not target_data.events_data:
                        events_data = await target_device.get_events()
                        target_data.events_data = events_data
                    else:
                        events_data = target_data.events_data

                    target_data.volume_data = volume_data
                    target_data.power_data = power_data
                    target_data.inputs_data = inputs_data
                    target_data.events_data = events_data

                    if target_data.poll_tick % 2 == 0 or not target_data.zone_info:
                        try:
                            latest_zone = await target_device.get_zone_info()
                            if isinstance(latest_zone, dict) and latest_zone:
                                target_data.zone_info = latest_zone
                        except Exception:
                            if not target_data.zone_info:
                                target_data.zone_info = {}

                    if not target_data.network_config:
                        try:
                            target_data.network_config = await target_device.get_network_config()
                        except Exception:
                            target_data.network_config = {}
                    if not target_data.aoip_ipv4:
                        try:
                            target_data.aoip_ipv4 = await target_device.get_aoip_ipv4()
                        except Exception:
                            target_data.aoip_ipv4 = {}
                    if not target_data.aoip_identity:
                        try:
                            target_data.aoip_identity = await target_device.get_aoip_identity()
                        except Exception:
                            target_data.aoip_identity = {}

                    zone_id = target_data.zone_info.get("zone")
                    zone_name = str(target_data.zone_info.get("name", "")).strip()
                    if isinstance(zone_id, int) and zone_id > 0 and zone_name:
                        zone_changed = _update_persisted_device_zone(
                            hass,
                            entry,
                            device_unique_id,
                            zone_id,
                            zone_name,
                        )
                    else:
                        zone_changed = False

                    if (
                        not target_data.group_bootstrapped
                        and isinstance(zone_id, int)
                        and zone_id > 0
                        and zone_name
                    ):
                        await hass.config_entries.flow.async_init(
                            DOMAIN,
                            context={"source": "import"},
                            data={
                                CONF_ENTRY_TYPE: ENTRY_TYPE_GROUP,
                                CONF_ZONE_ID: zone_id,
                                CONF_ZONE_NAME: zone_name,
                            },
                        )
                        target_data.group_bootstrapped = True
                    elif zone_changed:
                        for cfg_entry in hass.config_entries.async_entries(DOMAIN):
                            if cfg_entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_GROUP:
                                await hass.config_entries.async_reload(cfg_entry.entry_id)
                    if not target_data.profile_list:
                        try:
                            target_data.profile_list = await target_device.get_profile_list()
                        except Exception:
                            target_data.profile_list = {}
                    if not target_data.led_initialized:
                        try:
                            target_data.led_data = await target_device.get_led_settings()
                        except Exception:
                            target_data.led_data = {}
                        finally:
                            target_data.led_initialized = True

                    return {
                        "volume": volume_data,
                        "power": power_data,
                        "inputs": inputs_data,
                        "events": events_data,
                        "device_info": target_data.device_info,
                        "device_id": target_data.device_id,
                        "led": target_data.led_data,
                        "network_ipv4": target_data.network_config,
                        "aoip_ipv4": target_data.aoip_ipv4,
                        "aoip_identity": target_data.aoip_identity,
                        "zone_info": target_data.zone_info,
                        "profile_list": target_data.profile_list,
                    }
                except Exception as e:
                    LOGGER.error("Error updating coordinator data: %s", e)
                    return {
                        "volume": target_data.volume_data,
                        "power": target_data.power_data,
                        "inputs": target_data.inputs_data,
                        "events": target_data.events_data,
                        "device_info": target_data.device_info,
                        "device_id": target_data.device_id,
                        "led": target_data.led_data,
                        "network_ipv4": target_data.network_config,
                        "aoip_ipv4": target_data.aoip_ipv4,
                        "aoip_identity": target_data.aoip_identity,
                        "zone_info": target_data.zone_info,
                        "profile_list": target_data.profile_list,
                    }

            return async_update_data

        coordinator = DataUpdateCoordinator(
            hass,
            LOGGER,
            name=f"{DOMAIN}_{device_unique_id}",
            update_method=await _make_update(data, device),
            update_interval=timedelta(seconds=60),
            config_entry=entry,
        )
        data.coordinator = coordinator
        await coordinator.async_config_entry_first_refresh()

    await asyncio.gather(
        *[_setup_one_device(raw_cfg) for raw_cfg in devices_cfg if isinstance(raw_cfg, dict)],
        return_exceptions=False,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Remove the old hub device if it exists; the config entry itself is now the
    # top-level Genelec Devices container and no extra child device is needed.
    stale_hub = dev_reg.async_get_device(identifiers={(DOMAIN, SINGLE_HUB_ID)})
    if stale_hub is not None:
        try:
            dev_reg.async_remove_device(stale_hub.id)
        except Exception:
            LOGGER.debug("Failed to remove stale Genelec Devices device", exc_info=True)

    return True


async def async_unload_entry(hass: HomeAssistant,
                             entry: GenelecSmartIPConfigEntry) -> bool:
    """Unload a config entry."""
    LOGGER.info("Unloading Genelec Smart IP integration")

    entry_type = entry.data.get(CONF_ENTRY_TYPE, ENTRY_TYPE_DEVICE)
    unload_platforms = (
        [Platform.MEDIA_PLAYER, Platform.SELECT, Platform.NUMBER]
        if entry_type == ENTRY_TYPE_GROUP
        else PLATFORMS
    )

    if unload_ok := await hass.config_entries.async_unload_platforms(entry, unload_platforms):
        data = hass.data[DOMAIN].pop(entry.entry_id, None)
        if isinstance(data, GenelecDevicesHubData):
            for unique_id, dev_data in data.devices.items():
                hass.data[DOMAIN].pop(unique_id, None)
                if dev_data.session:
                    await dev_data.session.close()
        elif data and hasattr(data, 'session') and data.session:
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
