"""Constants for the Genelec Smart IP integration."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME, Platform

LOGGER = logging.getLogger(__package__)

DOMAIN = "genelec"

DEFAULT_PORT = 9000
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "admin"
DEFAULT_API_VERSION = "v1"

MIN_VOLUME_DB = -130.0
MAX_VOLUME_DB = 0.0

CONF_API_VERSION = "api_version"
CONF_ENTRY_TYPE = "entry_type"
CONF_ZONE_ID = "zone_id"
CONF_ZONE_NAME = "zone_name"
CONF_DEVICE_NAME = "device_name"

ENTRY_TYPE_DEVICE = "device"
ENTRY_TYPE_GROUP = "group"

SINGLE_HUB_ID = "genelec_devices_hub"
GROUP_HUB_ID = "genelec_group_hub"
SINGLE_HUB_NAME = "Genelec Devices"

# Platforms
PLATFORMS = [
    Platform.MEDIA_PLAYER,
    Platform.SENSOR,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.NUMBER,
]

# Scan interval for polling
SCAN_INTERVAL = timedelta(seconds=120)

# mDNS
MDNS_SERVICE = "_smart_ip._tcp.local."
GENELEC_OUI = "AC:47:23"

# Power states
POWER_STATE_ACTIVE = "ACTIVE"
POWER_STATE_STANDBY = "STANDBY"
POWER_STATE_BOOT = "BOOT"
POWER_STATE_AOIPBOOT = "AOIPBOOT"
POWER_STATE_ISS_SLEEP = "ISS_SLEEP"
POWER_STATE_PWR_FAIL = "PWR_FAIL"

# Inputs (API values)
INPUT_ANALOG_API = "A"
INPUT_AOIP_01_API = "AoIP01"
INPUT_AOIP_02_API = "AoIP02"

# Inputs (display names)
INPUT_ANALOG = "Analog"
INPUT_AOIP_01 = "AoIP 01"
INPUT_AOIP_02 = "AoIP 02"
INPUT_MIX = "Mix"
INPUT_NONE = "No Input"

# Input mapping
INPUT_API_TO_DISPLAY = {
    INPUT_ANALOG_API: INPUT_ANALOG,
    INPUT_AOIP_01_API: INPUT_AOIP_01,
    INPUT_AOIP_02_API: INPUT_AOIP_02,
}
INPUT_DISPLAY_TO_API = {v: k for k, v in INPUT_API_TO_DISPLAY.items()}

# Endpoints
API_BASE = "/public/{version}"
ENDPOINT_API_ROOT = "/"
ENDPOINT_DEVICE_ID = "/device/id"
ENDPOINT_DEVICE_INFO = "/device/info"
ENDPOINT_DEVICE_PWR = "/device/pwr"
ENDPOINT_DEVICE_LED = "/device/led"
ENDPOINT_AUDIO_VOLUME = "/audio/volume"
ENDPOINT_AUDIO_INPUTS = "/audio/inputs"
ENDPOINT_EVENTS = "/events"
ENDPOINT_PROFILE_LIST = "/profile/list"
ENDPOINT_PROFILE_RESTORE = "/profile/restore"
ENDPOINT_NETWORK_IPV4 = "/network/ipv4"
ENDPOINT_NETWORK_ZONE = "/network/zone"
ENDPOINT_AOIP_DANTE_IDENTITY = "/aoip/dante/identity"
ENDPOINT_AOIP_IPV4 = "/aoip/ipv4"

# Attributes
ATTR_BARCODE = "barcode"
ATTR_MAC = "mac"
ATTR_HW_ID = "hwId"
ATTR_MODEL = "model"
ATTR_FW_ID = "fwId"
ATTR_API_VER = "apiVer"
ATTR_CATEGORY = "category"
ATTR_CPU_TEMP = "cpu_temp"
ATTR_CPU_LOAD = "cpu_load"
ATTR_NETWORK_TRAFFIC = "network_traffic"
ATTR_UPTIME = "uptime"
ATTR_BASS_LEVEL = "bass_level"
ATTR_TWEETER_LEVEL = "tweeter_level"
ATTR_INPUT_LEVEL = "inLevel"

# Device categories
CATEGORY_SAM_1W = "SAM_1W"
CATEGORY_SAM_2W = "SAM_2W"
CATEGORY_SAM_3W = "SAM_3W"
CATEGORY_MICR = "MICR"

# Sensor data keys for coordinator
SENSOR_KEYS_EVENTS = "events"
SENSOR_KEYS_NETWORK_IPV4 = "network_ipv4"
SENSOR_KEYS_AOIP_IPV4 = "aoip_ipv4"
SENSOR_KEYS_AOIP_IDENTITY = "aoip_identity"
SENSOR_KEYS_ZONE = "zone_info"
SENSOR_KEYS_PROFILE = "profile_list"
