[![Active installations](https://badge.t-haber.de/badge/better_thermostat?kill_cache=1)](https://github.com/KartoffelToby/better_thermostat/)
[![GitHub issues](https://img.shields.io/github/issues/KartoffelToby/better_thermostat?style=for-the-badge)](https://github.com/KartoffelToby/better_thermostat/issues)
[![Version - 1.8.0](https://img.shields.io/badge/Version-1.8.0-009688?style=for-the-badge)](https://github.com/KartoffelToby/better_thermostat/releases)
[![Discord](https://img.shields.io/discord/925725316540923914.svg?style=for-the-badge)](https://discord.gg/9BUegWTG3K)
[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)

# Better Thermostat

**For more info visit: https://better-thermostat.org/**

### Requirements

- Minimum required Home Assistant version: `2024.12`
  (_Latest tested version: `2025.11.3`_)

### Companion UI

We've created a companion UI element which can display more information than the default thermostat element in Home Assistant. Check it out via HACS: [better-thermostat-ui-card](https://github.com/KartoffelToby/better-thermostat-ui-card)

- If you have a question or need help please create a new [discussion](https://github.com/KartoffelToby/better_thermostat/discussions) or check if your question is already answered
- If you have a suggestion, found a bug, or want to add a new device or function create a new [issue](https://github.com/KartoffelToby/better_thermostat/issues)
- If you want to contribute to this project create a new [pull request](https://github.com/KartoffelToby/better_thermostat/pulls)

### Features

This integration brings some smartness to your connected radiator thermostats setup:

- Uses a temperature sensor far away from the radiators to measure the real room temperature
- Makes your TRVs fully compatible with Google Home
- Let your windows disable your heating (avoid programming this via an automation)
- Your weather forecast provider will turn your heat on/off
- Or an outside air temperature sensor can do this as well
- Does some valve maintenance automatically, to avoid them getting stuck closed over the summer
- Group multiple TRVs to one (e.g. for a room with multiple radiators)
- Enhance the default TRV Algorithm with some smartness to reduce energy consumption
- Dynamic preset temperature learning & persistence (each preset, incl. baseline/"no preset", remembers the last temperature you set and survives restarts)

### Preset Temperature Configuration

Preset temperatures are now fully configurable via dedicated `number` entities.

How it works:

1. For each available preset mode (e.g. Eco, Comfort, Sleep), a corresponding `number` entity is created (e.g., `number.better_thermostat_preset_eco`).
2. These entities are located in the **Configuration** category of the device.
3. You can adjust the temperature for each preset directly using these number sliders.
4. The values are automatically persisted across Home Assistant restarts.
5. Changing a preset temperature via the number entity immediately updates the thermostat if that preset is currently active.

Default starting values:

```
Away:            16.0 °C
Boost:           24.0 °C
Comfort:         21.0 °C
Eco:             19.0 °C
Home:            20.0 °C
Sleep:           18.0 °C
Activity:        22.0 °C
```

### Which hardware do we support?

**We support all thermostats which are compatible with Home Assistant as long as they are shown up as a climate entity**

**_Integrations that are tested_**

- Zigbee2Mqtt
- Deconz
- Tado
- generic_thermostat

### How to setup

Install this integration via HACS or copy the files from the [latest release](https://github.com/KartoffelToby/better_thermostat/releases/latest)

Configuration details can be found in the [documentation](docs/Configuration/configuration.md) or on our website: [better-thermostat.org](https://better-thermostat.org/configuration)

Some nice-to-know config tips for the configuration.yaml

#### Example Window/Door - Sensor config

```yaml
group:
  livingroom_windows:
    name: Livingroom Windows
    icon: mdi:window-open-variant
    all: false
    entities:
      - binary_sensor.openclose_1
      - binary_sensor.openclose_2
      - binary_sensor.openclose_3
```

#### Combine multiple TRV to one (Group)

No worry, Better Thermostat supports grouping out of the box

---

# Contributing?

checkout the [CONTRIBUTING.md](CONTRIBUTING.md) file

# ☕ Support

If you want to support this project, you can ☕ [**buy a coffee here**](https://www.buymeacoffee.com/kartoffeltoby).

<a href="https://www.buymeacoffee.com/kartoffeltoby"><img src="https://img.buymeacoffee.com/button-api/?text=Buy me a coffee&emoji=&slug=kartoffeltoby&button_colour=0ac982&font_colour=000000&font_family=Cookie&outline_colour=000000&coffee_colour=ffffff"></a>
