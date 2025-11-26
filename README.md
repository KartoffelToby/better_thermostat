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

### Dynamic Preset Temperature Persistence

Since version (next release) static preset temperature fields have been removed from the configuration flow. Preset temperatures are now fully dynamic and automatically persisted.

How it works:

1. Select a preset (e.g. Boost, Eco, Comfort, Sleep, Activity, Home, Away) or stay in "No preset".
2. Change the target temperature in the standard UI while that preset is active.
3. The new value is immediately stored as that preset's temperature (or as the baseline temperature when in "No preset").
4. Switching away and back to the preset re-applies your custom value.
5. All customized preset (and baseline) temperatures are persisted across Home Assistant restarts (using a combination of entity state restore and config entry options for durability even in ephemeral test containers).

What is stored:

- Every preset listed in the climate entity plus the baseline (shown as no active preset) maintains its own temperature.
- A flag `bt_preset_customized` (boolean) indicates if at least one preset deviates from the original defaults.
- The mapping itself is exposed in the entity attributes as `bt_preset_temperatures` (JSON serialized) and is also mirrored into the integration's config entry options.

Resetting presets:

- To revert a single preset: activate it and set the temperature back to the original default (see defaults below). That becomes the new stored value.
- To revert everything quickly you can remove and re-add the integration (this clears stored options) or manually delete the `bt_preset_temperatures` key from the config entry options (advanced users via `.storage` editing—only do this while HA is stopped).

Default starting values (if no customization yet):

```
None (baseline): 20.0 °C
Away:            16.0 °C
Boost:           24.0 °C
Comfort:         21.0 °C
Eco:             19.0 °C
Home:            20.0 °C
Sleep:           18.0 °C
Activity:        22.0 °C
```

FAQ:

- Q: Are values lost if I restore a backup?
  A: They are stored in the config entry options and in the last entity state, so a normal HA backup/restore keeps them.
- Q: Can I automate per-preset changes?
  A: Yes—call `climate.set_temperature` while the preset is active; the stored preset temperature updates automatically.

If you rely on automation logic that previously referenced static config values, update it to read the entity attribute `bt_preset_temperatures` instead.

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
