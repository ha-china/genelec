# ESPHome

This directory contains standalone ESPHome configs for devices used with this project.

## M5Stack Dial media controller

File: `esphome/m5stack_dial_media_controller.yaml`

Companion blueprint:
[![Open your Home Assistant instance and import this blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https://github.com/ha-china/genelec/blob/main/blueprint/m5stack-dial-media-event-controller.yaml)

What it does:
- emit ESPHome event actions for volume up/down, mute toggle, and power toggle
- keep the display, touch input, RTC, buzzer, and diagnostics on the device side
- let Home Assistant handle actual media player control through the companion blueprint

Before flashing, update:
- Wi-Fi secrets in your ESPHome secrets file

Notes:
- import the companion blueprint in Home Assistant and bind the event entity to your target media player
- The current config does not add temperature or humidity because the stock Dial pin map and official docs do not show a built-in temp/humidity sensor
