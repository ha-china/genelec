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
                        device_name = user_input[CONF_HOST]
                        try:
                            network = await device.get_network_config()
                            hostname = network.get("hostname") if isinstance(network, dict) else None
                            if isinstance(hostname, str) and hostname.strip():
                                device_name = hostname.strip()
                        except Exception:
                            pass

                        await self.async_set_unique_id(device.unique_id)
                        self._abort_if_unique_id_configured()
                        payload = dict(user_input)
                        payload[CONF_DEVICE_NAME] = f"{device_name} [{user_input[CONF_HOST]}]"
                        payload[CONF_ENTRY_TYPE] = ENTRY_TYPE_DEVICE
                        return self.async_create_entry(title=payload[CONF_DEVICE_NAME], data=payload)
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

        # Use MAC for unique_id if available, otherwise use host
        unique_id = f"genelec_{mac.replace(':', '_')}" if mac else f"genelec_{host}"
        _LOGGER.info("Setting unique_id: %s", unique_id)

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
                        data = {
                            CONF_HOST: device_info[CONF_HOST],
                            CONF_PORT: device_info[CONF_PORT],
                            CONF_USERNAME: user_input.get(CONF_USERNAME, DEFAULT_USERNAME),
                            CONF_PASSWORD: user_input.get(CONF_PASSWORD, DEFAULT_PASSWORD),
                            CONF_API_VERSION: DEFAULT_API_VERSION,
                            CONF_DEVICE_NAME: f"{(device_info.get('name') or 'Genelec Smart IP')} [{device_info[CONF_HOST]}]",
                            CONF_ENTRY_TYPE: ENTRY_TYPE_DEVICE,
                        }
                        return self.async_create_entry(title=data[CONF_DEVICE_NAME], data=data)
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
