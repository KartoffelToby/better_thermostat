---
title: Schedule and night mode
description: Use service calls and automations to implement temporary target schedules.
---

Better Thermostat exposes service calls for temporary target scheduling.

## Available services

### `better_thermostat.set_temp_target_temperature`

Sets a temporary target for selected Better Thermostat entities.

<a href="https://my.home-assistant.io/redirect/developer_call_service/?service=better_thermostat.set_temp_target_temperature" target="_blank"><img src="https://my.home-assistant.io/badges/developer_call_service.svg" alt="Open set_temp_target_temperature service" /></a>

### `better_thermostat.restore_saved_target_temperature`

Restores the previously saved target temperature.

<a href="https://my.home-assistant.io/redirect/developer_call_service/?service=better_thermostat.restore_saved_target_temperature" target="_blank"><img src="https://my.home-assistant.io/badges/developer_call_service.svg" alt="Open restore_saved_target_temperature service" /></a>

## Typical night mode pattern

1. Use a Home Assistant `schedule` helper.
2. When schedule turns on, call `set_temp_target_temperature` with night temperature.
3. When schedule turns off, call `restore_saved_target_temperature`.

Ready-to-import blueprint:

<a href="https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https://github.com/KartoffelToby/better_thermostat/blob/master/blueprints/night_mode.yaml" target="_blank"><img src="https://my.home-assistant.io/badges/blueprint_import.svg" alt="Import night mode blueprint" /></a>
