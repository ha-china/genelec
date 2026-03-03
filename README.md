# Genelec Smart IP

[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/release/ha-china/genele.svg)](https://github.com/ha-china/genele/releases)
[![License](https://img.shields.io/github/license/ha-china/genele.svg)](LICENSE)

A custom integration for Home Assistant to control Genelec Smart IP series studio monitors.

## Installation

### Via HACS (Recommended)

1. Open HACS
2. Go to "Integrations"
3. Click the "+" button in the top right corner
4. Search for "Genelec Smart IP"
5. Click "Download"

### Manual Installation

1. Download this repository
2. Copy the `custom_components/genelec` folder to your Home Assistant configuration directory under `custom_components`
3. Restart Home Assistant

## Configuration

1. In Home Assistant, go to "Settings" -> "Devices & Services"
2. Click the "+" button in the bottom right corner
3. Search for "Genelec Smart IP"
4. Follow the setup instructions

## Features

- Media player control (volume, mute, input source switching)
- Power state monitoring and remote control
- Device information sensors (temperature, CPU load, uptime, etc.)
- LED brightness control
- Profile management
- Dante/AoIP settings
- Zone group entities (group media/profile/LED intensity)
- Multicast control services (volume/mute/profile/power)

## API Coverage (Smart IP API v1 rev 0.8.4)

This integration is implemented against `Smart IP API Documentation v1 rev 0.8.4`.

- `4.6 API version`: `GET /public/{version}/` (with `/device/info` fallback)
- `4.7 /aoip`: `GET /aoip/dante/identity`, `GET /aoip/ipv4`
- `4.8 /audio`: `GET/PUT /audio/inputs`, `GET/PUT /audio/volume`
- `4.9 /device`: `GET /device/id`, `GET /device/info`, `GET/PUT /device/pwr`
- `4.10 /events`: `GET /events`
- `4.11 /led`: `GET/PUT /device/led` (including LED intensity number entity)
- `4.12 /network`: `GET/PUT /network/ipv4`, `GET /network/zone`
- `4.13 /profile`: `GET /profile/list`, `PUT /profile/restore`
- `6 multicast`: `mcast.level`, `mcast.mute`, `mcast.profile`, `mcast.state`

Notes:
- Discovery is supported via mDNS (`_smart_ip._tcp`).
- Use Smart IP Manager and API control carefully; concurrent control can desync state.

## Supported Devices

- Genelec Smart IP series studio monitors (requires firmware with API support)

## Services

### wake_up
Wake up the device from standby/sleep mode.

### set_standby
Put the device in standby mode.

### boot_device
Boot the device.

### set_volume_level
Set volume level in dB.

### set_led_intensity
Set the front panel LED intensity.

### restore_profile
Restore a saved profile from device memory.

### set_network_ipv4
Write network IPv4 configuration (`/network/ipv4`).

### multicast_set_volume
Send multicast volume command (`mcast.level`).

### multicast_set_mute
Send multicast mute command (`mcast.mute`).

### multicast_set_profile
Send multicast profile command (`mcast.profile`).

### multicast_power
Send multicast power command (`mcast.state`).

### get_api_root
Query API root payload from `/public/<version>/`.

## License

MIT License
