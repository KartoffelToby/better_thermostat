---
layout: default
title: Schedule/Planer
nav_order: 3
description: "BT."
permalink: schedule
---

# Schedule/Planer

Services you can call from Home Assistant to set a temporary target temperature for a thermostat.

## Service: `better_thermostat.set_temp_target_temperature`
<a href="https://my.home-assistant.io/redirect/developer_call_service/?service=better_thermostat.set_temp_target_temperature" target="_blank"><img src="https://my.home-assistant.io/badges/developer_call_service.svg" alt="Open your Home Assistant instance and show your service developer tools with a specific service selected." /></a>

## Service: `better_thermostat.restore_saved_target_temperature`
<a href="https://my.home-assistant.io/redirect/developer_call_service/?service=better_thermostat.restore_saved_target_temperature" target="_blank"><img src="https://my.home-assistant.io/badges/developer_call_service.svg" alt="Open your Home Assistant instance and show your service developer tools with a specific service selected." /></a>

# How can i setup a night mode schedule?

Basically you can setup a automation that triggers a service call for every climate entity.
As a example you can use this blueprint:

<a href="https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FKartoffelToby%2Fbetter_thermostat%2Ftree%2Fmaster%2Fblueprints%2Fnight_mode.yaml" target="_blank"><img src="https://my.home-assistant.io/badges/blueprint_import.svg" alt="Open your Home Assistant instance and show the blueprint import dialog with a specific blueprint pre-filled." /></a>

```yaml
blueprint:
  name: BT Night mode
  description: Set BT Thermostats to night mode if Schedule event is active.
  domain: automation
  source_url: https://github.com/KartoffelToby/better_thermostat/tree/master/blueprints/night_mode.yaml
  input:
    night_times_schedule:
      name: Schedule helper
      selector:
        target:
          entity:
            domain: schedule
          device:
            integration: schedule

    thermostat_target:
      name: Thermostats
      selector:
        target:
          device:
            integration: better_thermostat
          entity:
            integration: better_thermostat
            domain: climate

    night_temp:
      name: Night Temperature
      description: The target temperature at night
      default: 18
      selector:
        number:
          min: 5
          max: 35
          unit_of_measurement: °C


mode: queued
max_exceeded: silent

trigger:
  - platform: state
    entity_id:
      - !input night_times_schedule
    from: "on"
    to: "off"
  - platform: state
    entity_id:
      - !input night_times_schedule
    from: "off"
    to: "on"
condition: []
action:
  - if:
      - condition: state
        entity_id: !input night_times_schedule
        state: "on"
    then:
      - service: better_thermostat.set_temp_target_temperature
        data:
          temperature: !input night_temp
        target: !input thermostat_target
    else:
      - service: better_thermostat.restore_saved_target_temperature
        data: {}
        target: !input thermostat_target

```