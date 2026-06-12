---
title: Schedule/Planner
description: Actions to set a temporary target temperature and how to build a night mode schedule.
slug: schedule
---

# Schedule/Planner

Actions you can perform from Home Assistant to set a temporary target temperature for a thermostat.

## Action: `better_thermostat.set_temp_target_temperature`

<a href="https://my.home-assistant.io/redirect/developer_call_service/?service=better_thermostat.set_temp_target_temperature" target="_blank"><img src="https://my.home-assistant.io/badges/developer_call_service.svg" alt="Open your Home Assistant instance and show your service developer tools with a specific service selected." /></a>

## Action: `better_thermostat.restore_saved_target_temperature`

<a href="https://my.home-assistant.io/redirect/developer_call_service/?service=better_thermostat.restore_saved_target_temperature" target="_blank"><img src="https://my.home-assistant.io/badges/developer_call_service.svg" alt="Open your Home Assistant instance and show your service developer tools with a specific service selected." /></a>

# How can I set up a night mode schedule?

You can set up an automation that performs an action for every climate entity.
As an example, you can use this blueprint:

<a href="https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https://github.com/KartoffelToby/better_thermostat/blob/master/blueprints/night_mode.yaml" target="_blank"><img src="https://my.home-assistant.io/badges/blueprint_import.svg" alt="Open your Home Assistant instance and show the blueprint import dialog with a specific blueprint pre-filled." /></a>

```yaml
blueprint:
  name: Better Thermostat Night mode
  description: >
    Set BT Thermostats to the Sleep preset when a Schedule event is active,
    and restore the None (normal) preset when the schedule ends.
    Requires the Sleep preset to be enabled in your Better Thermostat configuration.
  domain: automation
  source_url: https://github.com/KartoffelToby/better_thermostat/blob/master/blueprints/night_mode.yaml
  input:
    night_times_schedule:
      name: Schedule helper
      selector:
        entity:
          domain: schedule

    thermostat_target:
      name: Thermostats
      selector:
        target:
          device:
            integration: better_thermostat
          entity:
            integration: better_thermostat
            domain: climate

mode: queued
max_exceeded: silent

trigger:
  - platform: state
    entity_id: !input night_times_schedule
    from: "on"
    to: "off"
  - platform: state
    entity_id: !input night_times_schedule
    from: "off"
    to: "on"
condition: []
action:
  - if:
      - condition: state
        entity_id: !input night_times_schedule
        state: "on"
    then:
      - service: climate.set_preset_mode
        data:
          preset_mode: sleep
        target: !input thermostat_target
    else:
      - service: climate.set_preset_mode
        data:
          preset_mode: none
        target: !input thermostat_target
```
