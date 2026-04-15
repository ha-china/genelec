"""Helpers for stable Genelec zone membership resolution."""
from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .const import DOMAIN


def iter_zone_sources(hass: HomeAssistant) -> list[Any]:
    """Return all real device data objects for zone aggregation."""
    sources: list[Any] = []
    for key, value in hass.data.get(DOMAIN, {}).items():
        if key.startswith("_"):
            continue
        if hasattr(value, "devices"):
            sources.extend(
                dev_data for dev_data in value.devices.values() if getattr(dev_data, "device", None)
            )
            continue
        if getattr(value, "device", None):
            sources.append(value)
    return sources


def get_zone_info(value: Any) -> dict[str, Any]:
    """Return live zone info from data object/coordinator if available."""
    zone_info = getattr(value, "zone_info", {}) or {}
    if not zone_info:
        coordinator = getattr(value, "coordinator", None)
        if coordinator and coordinator.data:
            zone_info = coordinator.data.get("zone_info", {}) or {}
    return zone_info if isinstance(zone_info, dict) else {}


def get_device_identifier(value: Any) -> str:
    """Return stable device identifier for a device data object."""
    device_info = getattr(value, "device_info", {}) or {}
    identifier = device_info.get("_device_identifier")
    if isinstance(identifier, str) and identifier:
        return identifier
    device = getattr(value, "device", None)
    unique_id = getattr(device, "unique_id", None)
    return unique_id if isinstance(unique_id, str) else ""


def _zone_fields_match(zone_info: dict[str, Any], zone_id: int, zone_name: str) -> bool:
    expected_name = zone_name.strip().lower()
    try:
        zone_value = int(zone_info.get("zone"))
    except (TypeError, ValueError):
        zone_value = None
    live_name = str(zone_info.get("name", "")).strip().lower()
    return zone_value == zone_id or (bool(expected_name) and live_name == expected_name)


def resolve_zone_targets(hass: HomeAssistant, zone_id: int, zone_name: str) -> list[Any]:
    """Resolve stable zone members with live zone correction.

    Persisted members keep the group stable when live zone_info is temporarily
    missing. When live zone_info clearly points to another zone, the member is
    excluded so real zone changes still propagate automatically.
    """
    zone_index = hass.data.get(DOMAIN, {}).get("_zone_index", {})
    record = zone_index.get(zone_id, {}) if isinstance(zone_index, dict) else {}
    stable_ids = set(record.get("members", [])) if isinstance(record, dict) else set()

    targets: list[Any] = []
    seen_ids: set[str] = set()
    for value in iter_zone_sources(hass):
        device_id = get_device_identifier(value)
        live_zone = get_zone_info(value)
        live_known = bool(live_zone)
        live_matches = _zone_fields_match(live_zone, zone_id, zone_name) if live_known else False
        is_stable_member = bool(device_id) and device_id in stable_ids

        include = False
        if is_stable_member:
            include = True if not live_known else live_matches
        elif live_matches:
            include = True

        if not include:
            continue
        if device_id and device_id in seen_ids:
            continue
        targets.append(value)
        if device_id:
            seen_ids.add(device_id)
    return targets
