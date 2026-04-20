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
        self.led_supported: bool = True
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
        self.poll_failures: int = 0


class GenelecDevicesHubData:
    """Container for all single Genelec devices."""

    def __init__(self) -> None:
        self.devices: dict[str, GenelecSmartIPData] = {}


def _normalize_device_display_name(raw_name: str | None, fallback_name: str) -> str:
    """Return the stored display name or a fallback."""
    display_name = str(raw_name or "").strip()
    return display_name or fallback_name


def _get_zone_index(hass: HomeAssistant) -> dict[int, dict[str, Any]]:
    """Return mutable global zone index."""
    hass.data.setdefault(DOMAIN, {})
    return hass.data[DOMAIN].setdefault("_zone_index", {})


def _update_zone_index(
    hass: HomeAssistant,
    device_unique_id: str,
    zone_id: int,
    zone_name: str,
) -> bool:
    """Update global zone index from a device and report change."""
    def _select_zone_name(current_name: str, candidate_name: str) -> str:
        current = str(current_name).strip()
        candidate = str(candidate_name).strip()
        placeholder = f"Zone {zone_id}"

        if not candidate:
            return current
        if not current or current == placeholder:
            return candidate
        if candidate == placeholder:
            return current
        if len(candidate) > len(current) + 1:
            return candidate
        return current

    zone_index = _get_zone_index(hass)
    changed = False
    previous_zone_id: int | None = None

    for existing_zone_id in list(zone_index):
        record = dict(zone_index.get(existing_zone_id, {}))
        members = set(record.get("members", []))
        if device_unique_id not in members:
            continue
        previous_zone_id = existing_zone_id
        members.remove(device_unique_id)
        if existing_zone_id != zone_id:
            changed = True
        if members:
            record["members"] = sorted(members)
            zone_index[existing_zone_id] = record
        else:
            zone_index.pop(existing_zone_id, None)

    record = dict(zone_index.get(zone_id, {}))
    members = set(record.get("members", []))
    old_members = set(members)
    members.add(device_unique_id)
    record["name"] = _select_zone_name(record.get("name", ""), zone_name)
    record["members"] = sorted(members)
    zone_index[zone_id] = record
    if previous_zone_id == zone_id:
        return changed
    return changed or old_members != members


def _remove_from_zone_index(hass: HomeAssistant, device_unique_id: str) -> bool:
    """Remove a device from the global zone index and report change."""
    zone_index = _get_zone_index(hass)
    changed = False

    for existing_zone_id in list(zone_index):
        record = dict(zone_index.get(existing_zone_id, {}))
        members = set(record.get("members", []))
        if device_unique_id not in members:
            continue
        members.remove(device_unique_id)
        changed = True
        if members:
            record["members"] = sorted(members)
            zone_index[existing_zone_id] = record
        else:
            zone_index.pop(existing_zone_id, None)

    return changed


async def _reload_group_entries(hass: HomeAssistant) -> None:
    """Reload all Genelec Zone entries."""
    for cfg_entry in hass.config_entries.async_entries(DOMAIN):
        if cfg_entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_GROUP:
            await hass.config_entries.async_reload(cfg_entry.entry_id)


def _get_persisted_devices(entry: GenelecSmartIPConfigEntry) -> list[dict[str, Any]]:
    """Return persisted single-device payloads from the hub entry."""
    devices_cfg = entry.data.get(CONF_DEVICES, [])
    return list(devices_cfg) if isinstance(devices_cfg, list) else []


def _update_persisted_device_zone(
    hass: HomeAssistant,
    entry: GenelecSmartIPConfigEntry,
    device_unique_id: str,
    zone_id: int | None,
    zone_name: str,
) -> bool:
    """Persist per-device zone info into the Genelec Devices entry."""
    devices = _get_persisted_devices(entry)
    changed = False
    valid_zone = isinstance(zone_id, int) and zone_id > 0 and bool(zone_name)
    for idx, device_payload in enumerate(devices):
        if not isinstance(device_payload, dict):
            continue
        payload_unique_id = device_payload.get("unique_id") or device_payload.get(CONF_HOST)
        if payload_unique_id != device_unique_id:
            continue
        current_zone_id = device_payload.get(CONF_ZONE_ID)
        current_zone_name = device_payload.get(CONF_ZONE_NAME)
        if valid_zone and current_zone_id == zone_id and current_zone_name == zone_name:
            return False
        if not valid_zone and CONF_ZONE_ID not in device_payload and CONF_ZONE_NAME not in device_payload:
            return False
        updated = dict(device_payload)
        if valid_zone:
            updated[CONF_ZONE_ID] = zone_id
            updated[CONF_ZONE_NAME] = zone_name
        else:
            updated.pop(CONF_ZONE_ID, None)
            updated.pop(CONF_ZONE_NAME, None)
        devices[idx] = updated
        changed = True
        break
    if changed:
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_DEVICES: devices},
        )
    return changed


def _select_entry_zone_name(existing_name: str, candidate_name: str, zone_id: int) -> str:
    """Keep a stable entry-level zone name when devices disagree."""
    existing = str(existing_name).strip()
    candidate = str(candidate_name).strip()
    placeholder = f"Zone {zone_id}"

    if not candidate:
        return existing
    if not existing or existing == placeholder:
        return candidate
    if candidate == placeholder:
        return existing
    if len(candidate) > len(existing) + 1:
        return candidate
    return existing


async def _ensure_group_entry_exists(
    hass: HomeAssistant,
    devices: list[dict[str, Any]],
) -> None:
    """Ensure the single Genelec Zone entry exists when zones are known."""
    has_group_entry = any(
        cfg_entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_GROUP
        for cfg_entry in hass.config_entries.async_entries(DOMAIN)
    )
    if has_group_entry:
        return

    zone_index = _get_zone_index(hass)
    if zone_index:
        first_zone_id = sorted(zone_index)[0]
        first_zone_name = str(zone_index[first_zone_id].get("name", "")).strip()
        if first_zone_name:
            await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": "import"},
                data={
                    CONF_ENTRY_TYPE: ENTRY_TYPE_GROUP,
                    CONF_ZONE_ID: first_zone_id,
                    CONF_ZONE_NAME: first_zone_name,
                },
            )
            return

    for device_payload in devices:
        if not isinstance(device_payload, dict):
            continue
        zone_id = device_payload.get(CONF_ZONE_ID)
        zone_name = str(device_payload.get(CONF_ZONE_NAME, "")).strip()
        if isinstance(zone_id, int) and zone_id > 0 and zone_name:
            await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": "import"},
                data={
                    CONF_ENTRY_TYPE: ENTRY_TYPE_GROUP,
                    CONF_ZONE_ID: zone_id,
                    CONF_ZONE_NAME: zone_name,
                },
            )
            return


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
        device_display_name = _normalize_device_display_name(
            entry.data.get(CONF_DEVICE_NAME),
            entry.title or device.name,
        )
        device_info_data["_device_name"] = device_display_name
        data.device_info = device_info_data
        device._device_info = device_info_data
    except Exception as e:
        LOGGER.warning("Failed to get device_info during setup: %s", e)

    # Stable identifier shared between entities and device registry.
    # Prefer entry.unique_id (MAC-based) when available.
    if not data.device_info:
        data.device_info = {
            "_device_name": _normalize_device_display_name(
                entry.data.get(CONF_DEVICE_NAME),
                entry.title or device.name,
            )
        }
    data.device_info["_device_identifier"] = entry.unique_id or device.unique_id

    # Fetch zone early so Genelec Zone can appear without waiting for a full poll cycle.
    try:
        early_zone = await device.get_zone_info()
        if isinstance(early_zone, dict) and early_zone:
            data.zone_info = early_zone
    except Exception as e:
        LOGGER.debug("Early zone info not available during setup: %s", e)

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
        if speaker_device.name != device_display_name:
            dev_reg.async_update_device(speaker_device.id, name=device_display_name)
        if speaker_device.via_device_id != hub_device.id:
            dev_reg.async_update_device(speaker_device.id, via_device_id=hub_device.id)

    early_zone_id = data.zone_info.get("zone") if isinstance(data.zone_info, dict) else None
    early_zone_name = str(data.zone_info.get("name", "")).strip() if isinstance(data.zone_info, dict) else ""
    if isinstance(early_zone_id, int) and early_zone_id > 0 and early_zone_name:
        _update_zone_index(hass, data.device_info.get("_device_identifier", device.unique_id), early_zone_id, early_zone_name)
        if entry.data.get(CONF_ZONE_ID) != early_zone_id or entry.data.get(CONF_ZONE_NAME) != early_zone_name:
            updated_entry_data = dict(entry.data)
            updated_entry_data[CONF_ZONE_ID] = early_zone_id
            updated_entry_data[CONF_ZONE_NAME] = early_zone_name
            hass.config_entries.async_update_entry(entry, data=updated_entry_data)
        if not data.group_bootstrapped:
            await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": "import"},
                data={
                    CONF_ENTRY_TYPE: ENTRY_TYPE_GROUP,
                    CONF_ZONE_ID: early_zone_id,
                    CONF_ZONE_NAME: early_zone_name,
                },
            )
            data.group_bootstrapped = True

    # Create coordinator for centralized updates
    async def async_update_data():
        """Fetch data from device."""
        data.poll_tick += 1
        host = entry.data.get(CONF_HOST)
        port = entry.data.get(CONF_PORT, DEFAULT_PORT)
        poll_had_error = False

        def _coordinator_payload() -> dict[str, Any]:
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

        def _log_poll_exception(stage: str, err: Exception, *, optional: bool = False) -> None:
            nonlocal poll_had_error
            poll_had_error = True
            if isinstance(err, aiohttp.ClientResponseError) and err.status == 503:
                LOGGER.warning(
                    "Coordinator poll busy for %s:%s during %s: %r",
                    host,
                    port,
                    stage,
                    err,
                )
                return

            log_fn = LOGGER.debug if optional else LOGGER.warning
            log_fn(
                "Coordinator poll failed for %s:%s during %s (%s): %r",
                host,
                port,
                stage,
                type(err).__name__,
                err,
            )

        async def _fetch_required(stage: str, method, attr_name: str) -> dict[str, Any]:
            try:
                result = await method()
            except Exception as err:  # pylint: disable=broad-except
                _log_poll_exception(stage, err)
                return getattr(data, attr_name)
            setattr(data, attr_name, result)
            return result

        async def _fetch_optional_once(stage: str, method, attr_name: str, empty_value: Any) -> Any:
            try:
                result = await method()
                setattr(data, attr_name, result)
                return result
            except Exception as err:  # pylint: disable=broad-except
                _log_poll_exception(stage, err, optional=True)
                setattr(data, attr_name, empty_value)
                return empty_value

        volume_data = await _fetch_required("audio/volume", device.get_volume, "volume_data")
        power_data = await _fetch_required("device/pwr", device.get_power_state, "power_data")
        inputs_data = await _fetch_required("audio/inputs", device.get_inputs, "inputs_data")
        if data.poll_tick % 3 == 0 or not data.events_data:
            events_data = await _fetch_required("events", device.get_events, "events_data")
        else:
            events_data = data.events_data

        if not data.device_info:
            try:
                data.device_info = await device.get_device_info()
                LOGGER.debug("Device info: %s", data.device_info)
            except Exception as err:  # pylint: disable=broad-except
                _log_poll_exception("device/info", err, optional=True)

        if not data.device_id:
            try:
                data.device_id = await device.get_device_id()
                LOGGER.debug("Device ID: %s", data.device_id)
            except Exception as err:  # pylint: disable=broad-except
                _log_poll_exception("device/id", err, optional=True)

        if not data.led_initialized:
            try:
                data.led_data = await device.get_led_settings()
                data.led_supported = True
                data.led_initialized = True
                LOGGER.debug("LED data: %s", data.led_data)
            except Exception as err:  # pylint: disable=broad-except
                _log_poll_exception("device/led", err, optional=True)
                data.led_data = {}
                data.led_supported = False
                data.led_initialized = True

        if data.poll_tick % 2 == 0 or not data.zone_info:
            try:
                latest_zone = await device.get_zone_info()
                if isinstance(latest_zone, dict):
                    data.zone_info = latest_zone
                LOGGER.debug("Zone info: %s", data.zone_info)
            except Exception as err:  # pylint: disable=broad-except
                _log_poll_exception("network/zone", err, optional=True)
                if not data.zone_info:
                    data.zone_info = {}

        if not data.network_config:
            network = await _fetch_optional_once("network/ipv4", device.get_network_config, "network_config", {})
            LOGGER.debug("Network config: %s", network)
        if not data.aoip_ipv4:
            aoip_ipv4 = await _fetch_optional_once("aoip/ipv4", device.get_aoip_ipv4, "aoip_ipv4", {})
            LOGGER.debug("AoIP IPv4: %s", aoip_ipv4)
        if not data.aoip_identity:
            aoip_identity = await _fetch_optional_once("aoip/identity", device.get_aoip_identity, "aoip_identity", {})
            LOGGER.debug("AoIP identity: %s", aoip_identity)

        if entry_type == ENTRY_TYPE_DEVICE:
            try:
                zone_id = data.zone_info.get("zone")
                zone_name = str(data.zone_info.get("name", "")).strip()
                if isinstance(zone_id, int) and zone_id > 0 and zone_name:
                    zone_topology_changed = _update_zone_index(
                        hass,
                        data.device_info.get("_device_identifier", device.unique_id),
                        zone_id,
                        zone_name,
                    )
                    current_entry_zone_id = entry.data.get(CONF_ZONE_ID)
                    current_entry_zone_name = str(entry.data.get(CONF_ZONE_NAME, "")).strip()
                    resolved_zone_name = _select_entry_zone_name(current_entry_zone_name, zone_name, zone_id)
                    zone_changed = False
                    if current_entry_zone_id != zone_id or current_entry_zone_name != resolved_zone_name:
                        updated_entry_data = dict(entry.data)
                        updated_entry_data[CONF_ZONE_ID] = zone_id
                        updated_entry_data[CONF_ZONE_NAME] = resolved_zone_name
                        hass.config_entries.async_update_entry(entry, data=updated_entry_data)
                        zone_changed = current_entry_zone_id != zone_id
                    data.zone_persisted = True

                    if zone_topology_changed or zone_changed:
                        for cfg_entry in hass.config_entries.async_entries(DOMAIN):
                            if cfg_entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_GROUP:
                                await hass.config_entries.async_reload(cfg_entry.entry_id)
                else:
                    device_identifier = data.device_info.get("_device_identifier", device.unique_id)
                    zone_topology_changed = _remove_from_zone_index(hass, device_identifier)
                    zone_changed = _update_persisted_device_zone(
                        hass,
                        entry,
                        device_identifier,
                        None,
                        "",
                    )
                    if zone_topology_changed or zone_changed:
                        for cfg_entry in hass.config_entries.async_entries(DOMAIN):
                            if cfg_entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_GROUP:
                                await hass.config_entries.async_reload(cfg_entry.entry_id)
            except Exception as err:  # pylint: disable=broad-except
                _log_poll_exception("zone persistence", err, optional=True)

        if not data.profile_list:
            profile_list = await _fetch_optional_once("profile/list", device.get_profile_list, "profile_list", {})
            LOGGER.debug("Profile list: %s", profile_list)
        if not data.api_root_checked:
            try:
                data.api_root = await device.get_api_root()
                LOGGER.debug("API root payload: %s", data.api_root)
            except Exception as err:  # pylint: disable=broad-except
                _log_poll_exception("api root", err, optional=True)
                data.api_root = None
            finally:
                data.api_root_checked = True

        zone_id = data.zone_info.get("zone")
        zone_name = str(data.zone_info.get("name", "")).strip()
        if not data.group_bootstrapped and isinstance(zone_id, int) and zone_id > 0 and zone_name:
            try:
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
            except Exception as err:  # pylint: disable=broad-except
                _log_poll_exception("group bootstrap", err, optional=True)

        if poll_had_error:
            data.poll_failures += 1
        else:
            data.poll_failures = 0

        return _coordinator_payload()

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

    async def _resolve_group_multicast_endpoint(
        target_datas: list[GenelecSmartIPData],
    ) -> tuple[str, int] | None:
        for target_data in target_datas:
            endpoint = await _resolve_multicast_endpoint(target_data)
            if endpoint is not None:
                return endpoint
        return None

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

        if len(target_datas) > 1:
            multicast_endpoint = await _resolve_group_multicast_endpoint(target_datas)
            if multicast_endpoint is None:
                LOGGER.warning("Group set_volume_level has no usable multicast endpoint")
                return

            ip_value, port_value = multicast_endpoint
            multicast_level = max(-130.0, min(0.0, float(level)))
            await target_datas[0].device.send_multicast({"level": multicast_level}, ip_value, port_value)
            for target_data in target_datas:
                await _patch_coordinator(target_data, {"volume": {"level": level}})
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
        target_datas = await _get_target_datas_from_call(call)
        if len(target_datas) > 1:
            multicast_endpoint = await _resolve_group_multicast_endpoint(target_datas)
            if multicast_endpoint is None:
                LOGGER.warning("Group multicast_set_mute has no usable multicast endpoint")
                return

            ip_value, port_value = multicast_endpoint
            await target_datas[0].device.send_multicast({"mute": mute}, ip_value, port_value)
            for target_data in target_datas:
                await _patch_coordinator(target_data, {"volume": {"mute": mute}})
            return

        for target_data in target_datas:
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
    zone_data_changed = [False]

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

        device_display_name = _normalize_device_display_name(
            device_cfg.get(CONF_DEVICE_NAME),
            str(device_cfg.get(CONF_HOST) or device.name),
        )
        try:
            device_info_data = await device.get_device_info()
            device_info_data["_device_name"] = device_display_name
            data.device_info = device_info_data
            device._device_info = device_info_data
        except Exception as e:
            LOGGER.warning("Failed to get device_info during hub setup: %s", e)
            data.device_info = {"_device_name": device_display_name}

        data.device_info["_device_identifier"] = device_unique_id

        try:
            early_zone = await device.get_zone_info()
            if isinstance(early_zone, dict) and early_zone:
                data.zone_info = early_zone
        except Exception as e:
            LOGGER.debug("Early zone info not available during hub setup: %s", e)

        speaker_device = dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, device_unique_id)},
            name=device_display_name,
            manufacturer="Genelec",
            model=(data.device_info or {}).get(ATTR_MODEL, "Smart IP"),
        )
        if speaker_device.name != device_display_name:
            dev_reg.async_update_device(speaker_device.id, name=device_display_name)
        if speaker_device.via_device_id is not None:
            dev_reg.async_update_device(speaker_device.id, via_device_id=None)

        early_zone_id = data.zone_info.get("zone") if isinstance(data.zone_info, dict) else None
        early_zone_name = str(data.zone_info.get("name", "")).strip() if isinstance(data.zone_info, dict) else ""
        if isinstance(early_zone_id, int) and early_zone_id > 0 and early_zone_name:
            zone_index_changed = _update_zone_index(hass, device_unique_id, early_zone_id, early_zone_name)
            zone_changed = _update_persisted_device_zone(
                hass,
                entry,
                device_unique_id,
                early_zone_id,
                early_zone_name,
            )
            zone_data_changed[0] = zone_data_changed[0] or zone_index_changed
            if not data.group_bootstrapped:
                await hass.config_entries.flow.async_init(
                    DOMAIN,
                    context={"source": "import"},
                    data={
                        CONF_ENTRY_TYPE: ENTRY_TYPE_GROUP,
                        CONF_ZONE_ID: early_zone_id,
                        CONF_ZONE_NAME: _select_entry_zone_name("", early_zone_name, early_zone_id),
                    },
                )
                data.group_bootstrapped = True
            elif zone_index_changed:
                await _reload_group_entries(hass)

        async def _make_update(target_data: GenelecSmartIPData, target_device: GenelecSmartIPDevice):
            async def async_update_data() -> dict[str, Any]:
                target_host = getattr(target_device, "_host", device_cfg.get(CONF_HOST, "unknown"))
                target_port = getattr(target_device, "_port", device_cfg.get(CONF_PORT, DEFAULT_PORT))
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
                            if isinstance(latest_zone, dict):
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
                        zone_index_changed = _update_zone_index(hass, device_unique_id, zone_id, zone_name)
                        zone_changed = _update_persisted_device_zone(
                            hass,
                            entry,
                            device_unique_id,
                            zone_id,
                            zone_name,
                        )
                    else:
                        zone_index_changed = _remove_from_zone_index(hass, device_unique_id)
                        zone_changed = _update_persisted_device_zone(
                            hass,
                            entry,
                            device_unique_id,
                            None,
                            "",
                        )

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
                                CONF_ZONE_NAME: _select_entry_zone_name("", zone_name, zone_id),
                            },
                        )
                        target_data.group_bootstrapped = True
                    elif zone_index_changed:
                        await _reload_group_entries(hass)
                    if not target_data.profile_list:
                        try:
                            target_data.profile_list = await target_device.get_profile_list()
                        except Exception:
                            target_data.profile_list = {}
                    if not target_data.led_initialized:
                        try:
                            target_data.led_data = await target_device.get_led_settings()
                            target_data.led_supported = True
                        except Exception:
                            target_data.led_data = {}
                            target_data.led_supported = False
                        finally:
                            target_data.led_initialized = True

                    if target_data.poll_failures:
                        LOGGER.info(
                            "Coordinator poll recovered for %s:%s after %s failures",
                            target_host,
                            target_port,
                            target_data.poll_failures,
                        )
                        target_data.poll_failures = 0

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
                    target_data.poll_failures += 1
                    log_fn = LOGGER.warning if target_data.poll_failures == 1 else LOGGER.debug
                    log_fn(
                        "Coordinator poll failed for %s:%s (%s): %r",
                        target_host,
                        target_port,
                        type(e).__name__,
                        e,
                    )
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

    # Ensure Zone container exists immediately from persisted zone data.
    await _ensure_group_entry_exists(hass, _get_persisted_devices(entry))

    if zone_data_changed[0]:
        await _reload_group_entries(hass)

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
