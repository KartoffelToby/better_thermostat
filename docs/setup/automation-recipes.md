---
title: Automation recipes
sidebar:
    order: 6
description: Home Assistant automations that combine Better Thermostat's switches, numbers, presets and HVAC modes for situations it has no setting of its own for.
---

Better Thermostat gives each room a few settings that an automation can change: the **Pause Learning**, **Window Detection** and **Release devices** switches, the **Outdoor Temperature Threshold** and **Tolerance** numbers, the presets, the HVAC mode and the `better_thermostat.run_valve_maintenance` action. When to change them is up to you. A boiler that heats hot water, a lit fireplace or a sunny afternoon with surplus solar power are things Home Assistant knows about and Better Thermostat does not, so the decision belongs in an automation.

This page collects those automations by what you want to achieve. Where another page already has the recipe, it links there.

Add an automation under **Settings → Automations & Scenes**, in YAML mode. The entity IDs below are examples: Better Thermostat names its entities after the device, so a device called *Living room* has `climate.living_room`, `switch.living_room_pause_learning`, `switch.living_room_window_detection`, `switch.living_room_release_devices` and `number.living_room_outdoor_temperature_threshold`. Look yours up on the device page. A device with a cooler has the HVAC modes `heat_cool` and `off` instead of `heat` and `off`; use `heat_cool` in the recipes below for such a room.

## Keep the learned values clean

When the room warms or cools for a reason Better Thermostat cannot see, turn **Pause Learning** on. Regulation carries on, but nothing is learned until the switch is off again. [Pausing learning](/setup/pause-learning/) explains the switch and has the recipes:

- **When the boiler heats hot water or the heat source is locked out**, pause learning for that time: [The boiler heats hot water](/setup/pause-learning/#the-boiler-heats-hot-water), or the *Pause learning while the heat source delivers no heat* [blueprint](/setup/automation-blueprints/).
- **When a fireplace or stove warms the room**, pause learning in that room and the ones next to it: [A fireplace is lit](/setup/pause-learning/#a-fireplace-is-lit), or the *Pause learning while the fireplace burns* [blueprint](/setup/automation-blueprints/), which can also set a preset while the fire burns.
- **When a holiday home sits at frost protection for weeks**, pause learning while it does: [Away or frost protection for weeks](/setup/pause-learning/#away-or-frost-protection-for-weeks).

## Heat less while nobody is in the room

When the house or a single room is empty, set the Away preset and go back to your normal preset when someone is there again. The *Away preset when nobody is home* [blueprint](/setup/automation-blueprints/) does this. It takes `person` and `device_tracker` entities for the whole house, or a room's occupancy `binary_sensor` for one room, and waits a delay you choose before it lowers the temperature. Set the Away temperature on the device; see [Presets](/setup/presets/). Window and door sensors keep working alongside it.

## Decide yourself what an open window does

The **Window Detection** switch decides whether the window and door sensors turn the heating off. [Turning window detection off](/faq/window-sensor/#turning-window-detection-off) describes it and has the recipes:

- **When you want the Eco temperature instead of switching off**, for TRVs that drain their battery when switched off: [Lower the temperature instead of switching off](/faq/window-sensor/#lower-the-temperature-instead-of-switching-off).
- **When you have closed the window and want heat now**, without waiting for the close delay: [A button that resumes heating now](/faq/window-sensor/#a-button-that-resumes-heating-now).
- **When you want to heat with the window ajar, or a window sensor is stuck on open**, turn **Window Detection** off from a dashboard and let an automation turn it back on later: [Ignore a faulty window sensor](/faq/window-sensor/#ignore-a-faulty-window-sensor).

### Never let the room get colder than a minimum

When the room must not drop below a minimum, whatever a window sensor or the outdoor temperature says, override both while the room is too cold. This automation turns **Window Detection** off and raises the **Outdoor Temperature Threshold** to 40 °C, above the damped outdoor temperature you can expect, once the room has been below 15 °C for ten minutes. Once it is back above 17 °C, it turns detection on again and puts the threshold back to 18 °C, the value you normally use:

```yaml
alias: Living room never below 15 °C
triggers:
  - trigger: numeric_state
    entity_id: climate.living_room
    attribute: current_temperature
    below: 15
    for:
      minutes: 10
    id: too_cold
  - trigger: numeric_state
    entity_id: climate.living_room
    attribute: current_temperature
    above: 17
    for:
      minutes: 10
    id: warm_again
actions:
  - choose:
      - conditions:
          - condition: trigger
            id: too_cold
        sequence:
          - action: switch.turn_off
            target:
              entity_id: switch.living_room_window_detection
          - action: number.set_value
            target:
              entity_id: number.living_room_outdoor_temperature_threshold
            data:
              value: 40
      - conditions:
          - condition: trigger
            id: warm_again
        sequence:
          - action: switch.turn_on
            target:
              entity_id: switch.living_room_window_detection
          - action: number.set_value
            target:
              entity_id: number.living_room_outdoor_temperature_threshold
            data:
              value: 18
```

The room heats to its target temperature, so the target has to be above the minimum, and the thermostat has to be in `heat`. The **Window Detection** switch exists only for a device with a window or door sensor, and the threshold number only for one with an outdoor sensor or a weather entity; leave out the steps for the entity your device does not have. All temperatures in the automation are in your system's temperature unit; with Fahrenheit, use 59, 63, 64 and 104 instead of 15, 17, 18 and 40. A damped outdoor temperature at or above the raised threshold still switches the heating off, so pick a value above anything your climate reaches. The threshold steps act at once, because Better Thermostat checks the outdoor temperature again as soon as the threshold changes. A weather entity that stops giving a forecast needs no recipe: after three hours without one, the room heats until the forecast is back. See [Summer mode](/deep-explanations/summer-mode/).

## Switch the heating off for the summer

When the heating stays off from spring to autumn, whatever the weather on a single day, switch the thermostats off with a helper. Create a toggle helper called *Heating season* and switch it from a dashboard:

```yaml
alias: Heating season
triggers:
  - trigger: state
    entity_id: input_boolean.heating_season
    to:
      - "on"
      - "off"
actions:
  - action: climate.set_hvac_mode
    target:
      entity_id:
        - climate.living_room
        - climate.bedroom
    data:
      hvac_mode: "{{ 'heat' if trigger.to_state.state == 'on' else 'off' }}"
```

To switch on fixed dates, let a second automation set the helper, here on 1 May and 1 October. It also sets the helper when Home Assistant starts on one of those days, and leaves it to the dashboard on every other day:

```yaml
alias: Heating season by date
triggers:
  - trigger: template
    value_template: "{{ now().day == 1 and now().month in [5, 10] }}"
  - trigger: homeassistant
    event: start
conditions:
  - condition: template
    value_template: "{{ now().day == 1 and now().month in [5, 10] }}"
actions:
  - action: "input_boolean.turn_{{ 'off' if now().month == 5 else 'on' }}"
    target:
      entity_id: input_boolean.heating_season
```

Keeping the valves moving does not need the heating on. A TRV with **If your thermostat has no own maintenance mode, you can use this one** enabled is opened and closed about once a week, more often for some TRV models, while its thermostat is off, too. To exercise the valves at a time of your choosing instead, call the action from an automation of your own:

```yaml
action: better_thermostat.run_valve_maintenance
target:
  entity_id:
    - climate.living_room
    - climate.bedroom
```

It fails for a thermostat with no TRV that has maintenance enabled; see [Actions and conditions](/setup/actions-and-conditions/#better_thermostatrun_valve_maintenance).

## Run the devices yourself for a while

When you want to hold radiators open for maintenance or over the summer, run an air conditioner by hand while Better Thermostat is off, or let it run a self-cleaning sequence, turn **Release devices** on for that time. [Releasing the devices](/setup/releasing-devices/#examples) has a recipe for each.

## Use the outdoor temperature

The **Outdoor Temperature Threshold** stops the heating while a damped outdoor temperature, averaged over about a day, is above it. To change it from an automation or use one threshold for several rooms, see [Changing the threshold](/deep-explanations/summer-mode/#changing-the-threshold).

### Stop heating while it is nearly as warm outside as the target

When you want the heating off on a warm afternoon, compare the current outdoor temperature with the room's target. This automation switches the thermostat off once it is less than 2 °C colder outside than the target, and back on once the gap is more than 3 °C:

```yaml
alias: Living room off while it is warm outside
triggers:
  - trigger: template
    value_template: >-
      {{ states('sensor.outdoor_temperature') | float(-100)
         > state_attr('climate.living_room', 'temperature') | float(100) - 2 }}
    for:
      minutes: 15
    id: warm
  - trigger: template
    value_template: >-
      {{ states('sensor.outdoor_temperature') | float(100)
         < state_attr('climate.living_room', 'temperature') | float(-100) - 3 }}
    for:
      minutes: 15
    id: cold
conditions:
  - condition: template
    value_template: "{{ trigger.id == 'warm' or is_state('climate.living_room', 'off') }}"
actions:
  - action: climate.set_hvac_mode
    target:
      entity_id: climate.living_room
    data:
      hvac_mode: "{{ 'off' if trigger.id == 'warm' else 'heat' }}"
```

The thermostat keeps its target while it is off, so the second trigger can still compare with it. The automation switches the thermostat back on even when you switched it off yourself; turn the automation off for as long as the room should stay off. The `float` defaults make a missing outdoor reading count as neither warm nor cold. The gaps of 2 and 3 are in your system's temperature unit; with Fahrenheit, use 4 and 5. For a room with a cooler, use `target_temp_low` instead of `temperature` and `heat_cool` instead of `heat`.

### Heat with the air conditioner while the sun provides the power

When an air conditioner can heat the room more cheaply than the radiators while there is surplus solar power, set up two Better Thermostat devices for the room: one with the radiators' TRVs, one with the air conditioner as its thermostat. This automation keeps exactly one of them in `heat`. The air conditioner takes over once the surplus has stayed above 1.5 kW for ten minutes while it is warmer than 3 °C outside; the radiators take over again when the surplus has dropped below 0.8 kW for ten minutes or it gets colder than 3 °C outside. When Home Assistant starts, it picks the device from the current readings, and changes nothing while the readings are missing or between the thresholds:

```yaml
alias: Living room heats with surplus solar power
mode: queued
triggers:
  - trigger: template
    value_template: >-
      {{ states('sensor.solar_surplus_power') | float(0) > 1500
         and states('sensor.outdoor_temperature') | float(-100) > 3 }}
    for:
      minutes: 10
    id: surplus
  - trigger: numeric_state
    entity_id: sensor.solar_surplus_power
    below: 800
    for:
      minutes: 10
    id: no_surplus
  - trigger: numeric_state
    entity_id: sensor.outdoor_temperature
    below: 3
    id: no_surplus
  - trigger: homeassistant
    event: start
    id: start
variables:
  surplus: "{{ states('sensor.solar_surplus_power') | float(none) }}"
  outdoor: "{{ states('sensor.outdoor_temperature') | float(none) }}"
  heating_device: >-
    {% if trigger.id == 'surplus' %}climate.living_room_ac
    {% elif trigger.id == 'no_surplus' %}climate.living_room_radiators
    {% elif surplus is not none and outdoor is not none
         and surplus > 1500 and outdoor > 3 %}climate.living_room_ac
    {% elif (surplus is not none and surplus < 800)
         or (outdoor is not none and outdoor < 3) %}climate.living_room_radiators
    {% endif %}
conditions:
  - condition: template
    value_template: "{{ heating_device != '' }}"
actions:
  - action: climate.set_hvac_mode
    target:
      entity_id: "{{ heating_device }}"
    data:
      hvac_mode: heat
  - action: climate.set_hvac_mode
    target:
      entity_id: >-
        {{ 'climate.living_room_radiators' if heating_device == 'climate.living_room_ac'
           else 'climate.living_room_ac' }}
    data:
      hvac_mode: "off"
```

The outdoor limit of 3 is in the unit of the outdoor sensor's state in Home Assistant. Home Assistant converts temperature sensors to the system unit unless you set another unit on the sensor. When that state is in °F, use 37 instead. Give both devices the same room temperature sensor and the same target, and the air conditioner's device the calibration type *Target Temperature Based*. The device that is off switches its TRVs or its air conditioner off.

## Keep the air conditioner from cooling

When the air conditioner has a Better Thermostat device of its own, switching that device off keeps the air conditioner off, and the radiators of the room carry on; see [Releasing the devices](/setup/releasing-devices/#run-an-air-conditioner-by-hand-at-night).

When the air conditioner is the cooler of the device that also drives the radiators, switching the device off would stop the radiators too. Raise the cooling target instead: Better Thermostat keeps the air conditioner off as long as the room is below that target, and heats to the heating target as before. This automation raises the cooling target to the highest value the device accepts at night, keeping the heating target, and puts back the evening's targets and preset in the morning:

```yaml
alias: Living room air conditioner quiet at night
triggers:
  - trigger: time
    at: "22:00:00"
    id: night
  - trigger: time
    at: "07:00:00"
    id: morning
actions:
  - choose:
      - conditions:
          - condition: trigger
            id: night
        sequence:
          - action: scene.create
            data:
              scene_id: living_room_before_night
              snapshot_entities:
                - climate.living_room
          - action: climate.set_temperature
            target:
              entity_id: climate.living_room
            data:
              target_temp_low: "{{ state_attr('climate.living_room', 'target_temp_low') }}"
              target_temp_high: "{{ state_attr('climate.living_room', 'max_temp') }}"
      - conditions:
          - condition: trigger
            id: morning
        sequence:
          - action: scene.turn_on
            target:
              entity_id: scene.living_room_before_night
```

Better Thermostat limits the cooling target to the air conditioner's own range, so a room that gets warmer than the air conditioner's highest setting is still cooled. Setting a preset during the night brings back that preset's cooling target. A scene made with `scene.create` is lost when Home Assistant restarts, so after a restart at night, set the targets in the morning yourself.

The same automation keeps the air conditioner from cooling in winter: trigger the first branch when the outdoor temperature drops below a value of your choice, and the second when it rises above it again.
