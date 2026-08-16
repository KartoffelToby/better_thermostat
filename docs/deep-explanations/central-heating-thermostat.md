---
title: A central thermostat alongside TRVs
description: How Better Thermostat fits a system that has both a boiler thermostat and radiator valves.
---

Better Thermostat has no concept of a boiler. It controls TRVs, one instance per room, and it never decides whether the heat source runs. If your system has a central thermostat as well, that thermostat stays outside Better Thermostat and you drive it yourself.

This page exists because the question keeps coming back and the answer was never written down.

## The shape that works

Give each room its own Better Thermostat instance with that room's TRVs and its temperature sensor, and leave the central thermostat out of every one of them. Keep the central thermostat as its own climate entity, and drive it from an automation that watches the rooms.

## The signal to drive it with

Use `hvac_action` on the Better Thermostat entities. It reports `heating` while a room wants heat and `idle` once the room is satisfied, which is exactly the aggregate a boiler needs.

```yaml
triggers:
  - trigger: state
    entity_id:
      - climate.living_room
      - climate.bedroom
    attribute: hvac_action
actions:
  - action: climate.set_temperature
    target:
      entity_id: climate.central_thermostat
    data:
      temperature: >
        {{ 21 if expand('climate.living_room', 'climate.bedroom')
             | selectattr('attributes.hvac_action', 'eq', 'heating')
             | list | count > 0
           else 15 }}
```

Swap the two temperatures for whatever your central thermostat treats as "run" and "stand down".

### One attribute to stay away from

`call_for_heat` looks like the right signal and is not. It is the weather-based shutdown flag: it says the outdoor temperature is below the cut-off, not that this room wants heat. It is identical across every room served by the same weather entity.

## Why the central thermostat cannot be left alone

Leaving the boiler thermostat on its own schedule produces a failure that is easy to misread as a Better Thermostat problem.

While the central thermostat is satisfied, no flow reaches the radiators. The rooms keep missing their targets, so Better Thermostat opens their valves further, which is the correct response to a room that will not warm up. When the central thermostat calls for heat again, every valve is wide open and the rooms overshoot together. The symptom shows up as overshoot; the cause is the ceiling above the valves.

Wiring the central thermostat to the rooms removes the mismatch, because the boiler runs when a room asks and stands down when none does.

## A note on flow temperature

Better Thermostat does not modulate flow temperature either, and a flow-temperature loop is a second controller with its own time constant. Keep it much slower than the room loop: every change in flow temperature is a disturbance to every room at once, and a fast outer loop will fight the inner ones into an oscillation. Weather compensation from outdoor temperature, corrected slowly, is the usual well-behaved version.

For what Better Thermostat does contribute to distribution across rooms, see [Hydraulic balance](/deep-explanations/hydraulic-balance/).
