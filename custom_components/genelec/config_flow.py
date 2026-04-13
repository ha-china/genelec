"""Config flow for Genelec Smart IP integration."""
from __future__ import annotations

import asyncio
import ipaddress
import logging
from typing import Any

import voluptuous as vol
from zeroconf import ServiceBrowser, ServiceInfo, ServiceStateChange, Zeroconf
from zeroconf.asyncio import AsyncZeroconf

from homeassistant import config_entries
from homeassistant.components import zeroconf as hass_zeroconf
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_ENTRY_TYPE,
    CONF_API_VERSION,
    CONF_DEVICES,
    CONF_DEVICE_NAME,
    DEFAULT_API_VERSION,
    DEFAULT_PASSWORD,
    DEFAULT_PORT,
    DEFAULT_USERNAME,
    DOMAIN,
    ENTRY_TYPE_DEVICE,
    ENTRY_TYPE_GROUP,
    GENELEC_OUI,
    LOGGER,
    MDNS_SERVICE,
    SINGLE_HUB_ID,
    SINGLE_HUB_NAME,
)
from .device import GenelecSmartIPDevice

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Optional(CONF_USERNAME, default=DEFAULT_USERNAME): str,
        vol.Optional(CONF_PASSWORD, default=DEFAULT_PASSWORD): str,
        vol.Optional(CONF_API_VERSION, default=DEFAULT_API_VERSION): str,
    }
)

class GenelecSmartIPConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Genelec Smart IP."""

    VERSION = 1
    MINOR_VERSION = 0

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovered_devices: list[dict[str, Any]] = []
        self._zeroconf: AsyncZeroconf | None = None

    def is_matching(self, other_flow: config_entries.ConfigFlow) -> bool:
        """Match only the same discovered device, not all Genelec devices."""
        if not isinstance(other_flow, GenelecSmartIPConfigFlow):
            return False
        if not self._discovered_devices or not other_flow._discovered_devices:
            return False
        current = self._discovered_devices[0]
        other = other_flow._discovered_devices[0]
        return (
            current.get(CONF_HOST) == other.get(CONF_HOST)
            or (
                current.get("mac")
                and current.get("mac") == other.get("mac")
            )
        )

    def _get_devices_entry(self) -> config_entries.ConfigEntry | None:
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_DEVICE and entry.title == SINGLE_HUB_NAME:
                return entry
            if entry.unique_id == SINGLE_HUB_ID:
                return entry
        return None

    def _hub_has_device(self, unique_id: str | None, host: str | None) -> bool:
        hub_entry = self._get_devices_entry()
        if hub_entry is None:
            return False
        for existing in list(hub_entry.data.get(CONF_DEVICES, [])):
            if unique_id and existing.get("unique_id") == unique_id:
                return True
            if host and existing.get(CONF_HOST) == host:
                return True
        return False

    async def _sync_hub_device_host(
        self,
        unique_id: str | None,
        host: str | None,
        port: int | None,
    ) -> bool:
        """Update persisted host/port for an already configured device."""
        hub_entry = self._get_devices_entry()
        if hub_entry is None:
            return False

        devices = list(hub_entry.data.get(CONF_DEVICES, []))
        changed = False
        matched = False

        for idx, existing in enumerate(devices):
            if not isinstance(existing, dict):
                continue
            if unique_id and existing.get("unique_id") == unique_id:
                matched = True
            elif host and existing.get(CONF_HOST) == host:
                matched = True
            else:
                continue

            updated = dict(existing)
            if host and updated.get(CONF_HOST) != host:
                updated[CONF_HOST] = host
                changed = True
            if port and updated.get(CONF_PORT) != port:
                updated[CONF_PORT] = port
                changed = True
            if changed:
                devices[idx] = updated
            break

        if not matched:
            return False

        if changed:
            self.hass.config_entries.async_update_entry(
                hub_entry,
                data={**hub_entry.data, CONF_DEVICES: devices},
            )
            await self.hass.config_entries.async_reload(hub_entry.entry_id)
        return True

    async def _upsert_device_into_hub(self, payload: dict[str, Any]) -> FlowResult:
        hub_entry = self._get_devices_entry()
        unique_id = payload.get("unique_id") or payload.get(CONF_HOST)

        if hub_entry is None:
            await self.async_set_unique_id(SINGLE_HUB_ID)
            self._abort_if_unique_id_configured()
            payload[CONF_ENTRY_TYPE] = ENTRY_TYPE_DEVICE
            return self.async_create_entry(
                title=SINGLE_HUB_NAME,
                data={
                    CONF_ENTRY_TYPE: ENTRY_TYPE_DEVICE,
                    CONF_DEVICES: [payload],
                },
            )

        devices = list(hub_entry.data.get(CONF_DEVICES, []))
        for existing in devices:
            if existing.get("unique_id") == unique_id or existing.get(CONF_HOST) == payload.get(CONF_HOST):
                return self.async_abort(reason="already_configured")

        devices.append(payload)
        self.hass.config_entries.async_update_entry(
            hub_entry,
            data={**hub_entry.data, CONF_DEVICES: devices},
        )
        await self.hass.config_entries.async_reload(hub_entry.entry_id)
        return self.async_abort(reason="added_to_hub")

    async def _ensure_devices_hub(self) -> config_entries.ConfigEntry | None:
        """Create the Genelec Devices entry if it does not exist."""
        hub_entry = self._get_devices_entry()
        if hub_entry is not None:
            return hub_entry

        result = await self.hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "user"},
            data={
                CONF_ENTRY_TYPE: ENTRY_TYPE_DEVICE,
                CONF_DEVICES: [],
            },
        )

        if result.get("type") == "create_entry":
            return self._get_devices_entry()
        return self._get_devices_entry()

    async def _resolve_device_name(
        self,
        device: GenelecSmartIPDevice,
        fallback_name: str,
        host: str,
    ) -> str:
        """Resolve preferred display name for a single device."""
        try:
            aoip_identity = await device.get_aoip_identity()
            receiver_name = aoip_identity.get("fname") if isinstance(aoip_identity, dict) else None
            if isinstance(receiver_name, str) and receiver_name.strip():
                return receiver_name.strip()
        except Exception:
            pass

        try:
            network = await device.get_network_config()
            hostname = network.get("hostname") if isinstance(network, dict) else None
            if isinstance(hostname, str) and hostname.strip():
                return hostname.strip()
        except Exception:
            pass

        if isinstance(fallback_name, str) and fallback_name.strip():
            return fallback_name.strip()
        return host

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["device", "group"],
        )

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure a single device entry."""
        errors: dict[str, str] = {}

        if user_input is None:
            await self._ensure_devices_hub()

        if user_input is not None:
            try:
                # Create temporary session for connection test
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    lock = asyncio.Lock()
                    device = GenelecSmartIPDevice(
                        host=user_input[CONF_HOST],
                        username=user_input.get(CONF_USERNAME, DEFAULT_USERNAME),
                        password=user_input.get(CONF_PASSWORD, DEFAULT_PASSWORD),
                        port=user_input.get(CONF_PORT, DEFAULT_PORT),
                        api_version=user_input.get(CONF_API_VERSION, DEFAULT_API_VERSION),
                        session=session,
                        lock=lock,
                    )

                    if await device.test_connection():
                        device_name = await self._resolve_device_name(
                            device,
                            fallback_name=user_input[CONF_HOST],
                            host=user_input[CONF_HOST],
                        )

                        try:
                            await device.get_device_id()
                        except Exception:
                            pass

                        payload = dict(user_input)
                        payload[CONF_DEVICE_NAME] = f"{device_name} [{user_input[CONF_HOST]}]"
                        payload["unique_id"] = device.unique_id
                        return await self._upsert_device_into_hub(payload)
                    else:
                        errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="device",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_group(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure a zone-group hub entry."""
        await self.async_set_unique_id("genelec_group_hub")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="Genelec Zone",
            data={
                CONF_ENTRY_TYPE: ENTRY_TYPE_GROUP,
            },
        )

    async def async_step_import(self, user_input: dict[str, Any]) -> FlowResult:
        """Handle import-style creation for group entries."""
        entry_type = user_input.get(CONF_ENTRY_TYPE)
        if entry_type == ENTRY_TYPE_DEVICE:
            hub_entry = self._get_devices_entry()
            if hub_entry is not None:
                return self.async_abort(reason="already_configured")

            await self.async_set_unique_id(SINGLE_HUB_ID)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=SINGLE_HUB_NAME,
                data={
                    CONF_ENTRY_TYPE: ENTRY_TYPE_DEVICE,
                    CONF_DEVICES: list(user_input.get(CONF_DEVICES, [])),
                },
            )

        if entry_type != ENTRY_TYPE_GROUP:
            return self.async_abort(reason="discovery_failed")

        await self.async_set_unique_id("genelec_group_hub")
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title="Genelec Zone",
            data={
                CONF_ENTRY_TYPE: ENTRY_TYPE_GROUP,
            },
        )

    async def async_step_zeroconf(
        self, discovery_info: ServiceInfo
    ) -> FlowResult:
        """Handle zeroconf discovery."""
        _LOGGER.info("Zeroconf discovery triggered: %s", discovery_info)

        if discovery_info is None:
            _LOGGER.error("Discovery info is None")
            return self.async_abort(reason="discovery_failed")

        name = discovery_info.name.replace(f".{MDNS_SERVICE}", "")
        # Get hostname from service info
        hostname = discovery_info.name if hasattr(discovery_info, 'name') else None
        # Use addresses attribute instead of parsed_addresses
        addresses = discovery_info.addresses if hasattr(
            discovery_info, 'addresses') else []
        host = None
        if addresses:
            raw_addr = addresses[0]
            try:
                if isinstance(raw_addr, (bytes, bytearray)):
                    host = str(ipaddress.ip_address(raw_addr))
                else:
                    host = str(raw_addr)
            except ValueError:
                _LOGGER.warning("Failed to parse zeroconf address: %s", raw_addr)
        port = discovery_info.port
        properties = discovery_info.properties or {}

        mac_bytes = properties.get(b"mac", b"")
        mac = mac_bytes.decode() if mac_bytes else None

        _LOGGER.info("Discovered device: name=%s, host=%s, port=%s, mac=%s",
                     name, host, port, mac)

        # Only check MAC if it's provided, otherwise proceed anyway
        if mac and not mac.startswith(GENELEC_OUI):
            _LOGGER.warning(
                "Discovered device MAC %s doesn't match Genelec OUI, but proceeding anyway", mac)

        if host is None:
            _LOGGER.error("No IP address found in discovery info")
            return self.async_abort(reason="no_ip_address")

        unique_id = f"genelec_{mac.replace(':', '_')}" if mac else f"genelec_{host}"
        if await self._sync_hub_device_host(unique_id, host, port):
            return self.async_abort(reason="already_configured")
        if self._hub_has_device(unique_id, host):
            return self.async_abort(reason="already_configured")

        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()

        self.context["title_placeholders"] = {
            "name": name,
            "host": host,
        }

        self._discovered_devices = [
            {
                CONF_HOST: host,
                CONF_PORT: port,
                "name": name,
                "mac": mac,
            }
        ]

        _LOGGER.info("Proceeding to confirmation step")
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle user-confirmation of discovered device."""
        errors: dict[str, str] = {}

        if user_input is not None:
            if not self._discovered_devices:
                return self.async_abort(reason="no_devices_found")

            device_info = self._discovered_devices[0]

            try:
                # Create temporary session for connection test
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    lock = asyncio.Lock()
                    device = GenelecSmartIPDevice(
                        host=device_info[CONF_HOST],
                        port=device_info[CONF_PORT],
                        username=user_input.get(CONF_USERNAME, DEFAULT_USERNAME),
                        password=user_input.get(CONF_PASSWORD, DEFAULT_PASSWORD),
                        session=session,
                        lock=lock,
                    )

                    if await device.test_connection():
                        device_name = await self._resolve_device_name(
                            device,
                            fallback_name=device_info.get("name") or "Genelec Smart IP",
                            host=device_info[CONF_HOST],
                        )
                        try:
                            await device.get_device_id()
                        except Exception:
                            pass

                        data = {
                            CONF_HOST: device_info[CONF_HOST],
                            CONF_PORT: device_info[CONF_PORT],
                            CONF_USERNAME: user_input.get(CONF_USERNAME, DEFAULT_USERNAME),
                            CONF_PASSWORD: user_input.get(CONF_PASSWORD, DEFAULT_PASSWORD),
                            CONF_API_VERSION: DEFAULT_API_VERSION,
                            CONF_DEVICE_NAME: f"{device_name} [{device_info[CONF_HOST]}]",
                            "unique_id": device.unique_id,
                        }
                        return await self._upsert_device_into_hub(data)
                    else:
                        errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="confirm",
            description_placeholders={
                "name": self._discovered_devices[0]["name"],
                "host": self._discovered_devices[0][CONF_HOST],
            },
            data_schema=vol.Schema({
                vol.Optional(CONF_USERNAME, default=DEFAULT_USERNAME): str,
                vol.Optional(CONF_PASSWORD, default=DEFAULT_PASSWORD): str,
            }),
            errors=errors,
        )
