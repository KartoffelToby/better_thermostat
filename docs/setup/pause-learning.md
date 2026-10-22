---
title: Pausing learning
sidebar:
    order: 4
description: The Pause Learning switch keeps Better Thermostat from learning while the room behaves in a way it should not learn from, with automation recipes for a boiler heating hot water, a fireplace and an away preset.
---

Better Thermostat learns how your room heats and cools from what it observes: how fast the room warms while the valve is open, how fast it cools while the valve is closed, and how the calibration controllers have to be set to hit the target. That works as long as an open valve heats the room. Sometimes it does not, for reasons Better Thermostat cannot see:

- A combi boiler heats domestic hot water and sends no heat to the radiators for a quarter of an hour.
- The boiler or heat pump is locked out by a schedule, its own weather logic or a defrost cycle.
- A fireplace or another heat source warms the room while the valve is closed.
- The room sits at a frost-protection temperature for weeks while you are away.

Learning from such a stretch teaches the controllers something that stops being true when it ends. A PID controller raises its gains because the room does not warm up, and overshoots once the heat comes back; the learned heating power drifts down during weeks of short frost-protection cycles.

The **Pause Learning** switch stops that. You decide in Home Assistant when the room is not worth learning from, and an automation turns the switch on and off.

## What the switch does

**Pause Learning** exists for every Better Thermostat device and sits in the *Configuration* section of the device page. While it is on:

- Regulation carries on. The TRVs keep getting their target temperature, offset or valve opening, computed with what was learned before the pause. Unlike an open window, the switch does not close the valves, so heat flows as soon as it is there again.
- Nothing learned changes. The learned heating power and heat loss stay as they are, the PID controller neither changes its gains nor lets its integral grow, and MPC Predictive and MPC v2 hold what they learned about the room.
- A heating cycle or an idle period that is running when the switch turns on is discarded, not evaluated, the same way an open window discards it.

When the switch turns off, Better Thermostat runs one more control cycle right away, still without learning, and every learner starts its next interval from that cycle. Learning then resumes from the values held. Nothing that happened during the pause is caught up, and no learning interval spans the pause.

The thermostat shows the pause too. Its `learning_paused` attribute is `true` while the switch is on, which you can read in **Developer Tools → States** or use in a template. The switch keeps its state across a restart of Home Assistant.

The switch pauses every calibration mode the device uses. In detail:

| Learns | While paused |
| --- | --- |
| Heating power and heat loss (shown by the *Heating Power* and *Heat Loss* sensors) | Held. A cycle in progress is dropped, and no new one starts until the pause ends. |
| PID Controller | The gains and the integral are held, and auto-tuning starts over after the pause. The P and D terms keep acting on the room. |
| MPC Predictive | Gain, loss, insulation and the minimum valve opening are held; no forced closure to measure the heat loss starts, and one in progress ends. |
| MPC v2 | The disturbance estimate and the error integral are held, and no new room model is fitted or adopted. Its estimate of the room temperature keeps following the sensor. |
| AI Time Based | Learns through the heating power above, which is held. |
| TPI Controller, Aggressive, External Sensor Offset Only | Learn nothing, so there is nothing to pause. |

The smoothed room temperature and the temperature slope keep following the sensor. They describe the room as it is, not what Better Thermostat has learned about it.

## Recipes

Each recipe is an automation you add under **Settings → Automations & Scenes**, in YAML mode. Replace the entity IDs with your own. One automation can drive the switches of several rooms: list them under `entity_id`.

### The boiler heats hot water

A combi boiler with hot water priority sends no heat to the radiators while it heats water. Many boilers report this through their integration, for example over eBUS or OpenTherm. Build a `binary_sensor` that is `on` only while the boiler delivers heat to the radiators, then pause learning whenever it is off:

```yaml
alias: Pause learning while the boiler heats no radiators
triggers:
  - trigger: state
    entity_id: binary_sensor.boiler_heats_radiators
    to: "off"
    id: pause
  - trigger: state
    entity_id: binary_sensor.boiler_heats_radiators
    to: "on"
    id: resume
actions:
  - action: "switch.turn_{{ 'on' if trigger.id == 'pause' else 'off' }}"
    target:
      entity_id:
        - switch.living_room_pause_learning
        - switch.bedroom_pause_learning
```

The two triggers name the states they react to, so a sensor that turns `unavailable` changes nothing. The same automation covers a boiler lockout or a heat pump in a defrost cycle, as long as a sensor reports it. The *Pause learning while the heat source delivers no heat* [blueprint](/setup/automation-blueprints/) sets up the same automation from the UI, also for a sensor that is `on` while the radiators get no heat, such as a hot water indicator.

### A fireplace is lit

A fireplace warms the room while the valves stay closed. Pause learning while it burns, here based on a temperature sensor at the fireplace:

```yaml
alias: Pause learning while the fireplace burns
triggers:
  - trigger: numeric_state
    entity_id: sensor.fireplace_temperature
    above: 40
    id: pause
  - trigger: numeric_state
    entity_id: sensor.fireplace_temperature
    below: 35
    for:
      minutes: 30
    id: resume
actions:
  - action: "switch.turn_{{ 'on' if trigger.id == 'pause' else 'off' }}"
    target:
      entity_id:
        - switch.living_room_pause_learning
        - switch.dining_room_pause_learning
```

The thresholds are in the unit of the sensor's state in Home Assistant, because `numeric_state` compares that state and converts nothing. When the state is in °F, use 104 instead of 40 and 95 instead of 35. The *Pause learning while the fireplace burns* [blueprint](/setup/automation-blueprints/) converts a sensor in °F or K to °C itself.

The second room is the one next door that the fireplace warms as well. The half hour before learning resumes lets the room cool off from the fire first. Better Thermostat keeps regulating meanwhile and closes the valves while the room is warm enough. The *Pause learning while the fireplace burns* [blueprint](/setup/automation-blueprints/) sets up the same automation from the UI and can also set a preset, such as Eco, while the fire burns.

### Away or frost protection for weeks

In a holiday home that sits at a low temperature for weeks, learning from the short frost-protection cycles drags the learned heating power down. Pause learning while the Away preset is active:

```yaml
alias: Pause learning while away
triggers:
  - trigger: state
    entity_id: climate.holiday_home
    attribute: preset_mode
conditions:
  - condition: template
    value_template: >-
      {{ trigger.to_state is not none
         and trigger.to_state.state not in ['unavailable', 'unknown'] }}
actions:
  - action: >-
      switch.turn_{{ 'on' if trigger.to_state.attributes.preset_mode == 'away' else 'off' }}
    target:
      entity_id: switch.holiday_home_pause_learning
```

When you arrive and switch the preset back, learning resumes from the heating power the room had before you left.

Without an Away preset, pause learning while the target is at frost protection, here below 15 °C. The target is in your system's temperature unit; with Fahrenheit, use 59 instead of 15:

```yaml
alias: Pause learning at frost protection
triggers:
  - trigger: state
    entity_id: climate.holiday_home
    attribute: temperature
conditions:
  - condition: template
    value_template: "{{ trigger.to_state.attributes.temperature is number }}"
actions:
  - action: >-
      switch.turn_{{ 'on' if trigger.to_state.attributes.temperature < 15 else 'off' }}
    target:
      entity_id: switch.holiday_home_pause_learning
```

### Several reasons for one room

Each recipe above turns the switch off when its own reason ends, even if another reason still holds, and so do the two pause-learning blueprints. When a room has more than one reason to pause, combine them in a template binary sensor and drive the switch from that sensor alone. The fireplace first becomes a sensor of its own, with the same thresholds and delay as its recipe above:

```yaml
template:
  - triggers:
      - trigger: numeric_state
        entity_id: sensor.fireplace_temperature
        above: 40
        id: burning
      - trigger: numeric_state
        entity_id: sensor.fireplace_temperature
        below: 35
        for:
          minutes: 30
        id: out
    binary_sensor:
      - name: Fireplace burning
        state: "{{ trigger.id == 'burning' }}"
  - binary_sensor:
      - name: Living room learning paused
        availability: "{{ has_value('binary_sensor.boiler_heats_radiators') }}"
        state: >-
          {{ is_state('binary_sensor.boiler_heats_radiators', 'off')
             or is_state('binary_sensor.fireplace_burning', 'on') }}
```

The fireplace sensor only changes when one of its triggers fires, so it stays on until the fireplace has been below 35 °C for 30 minutes, and it keeps its state across a restart. As in the recipe, use 104 and 95 when the sensor's state is in °F. The combined sensor is unavailable while the boiler sensor is, and this automation then leaves the switch alone:

```yaml
alias: Pause learning in the living room
triggers:
  - trigger: state
    entity_id: binary_sensor.living_room_learning_paused
    to: "on"
    id: pause
  - trigger: state
    entity_id: binary_sensor.living_room_learning_paused
    to: "off"
    id: resume
actions:
  - action: "switch.turn_{{ 'on' if trigger.id == 'pause' else 'off' }}"
    target:
      entity_id: switch.living_room_pause_learning
```
