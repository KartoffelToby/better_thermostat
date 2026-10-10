---
title: Actions and conditions
sidebar:
    order: 6
description: The actions Better Thermostat adds to Home Assistant, their fields, and the device conditions it offers in automations.
---

Better Thermostat adds three actions of its own and two device conditions. Everything else, such as setting a target or a preset, goes through Home Assistant's `climate` actions, which every Better Thermostat accepts.

## Actions

Each action targets one or more Better Thermostat climate entities. You find them under **Developer tools → Actions** and in the automation editor under **Add action → Better Thermostat**.

### `better_thermostat.reset_heating_power`

Sets the heating power the thermostat has learned back to its starting value. The thermostat then learns it again from the next heating cycles; the current value is published in the `heating_power` attribute and the heating power sensor.

Use it after a change that makes the learned value wrong, such as a new radiator or a room sensor in a new place.

This action takes no fields.

```yaml
action: better_thermostat.reset_heating_power
target:
  entity_id: climate.living_room
```

### `better_thermostat.run_valve_maintenance`

Runs the valve maintenance cycle now instead of at its next scheduled time: each valve is opened and closed so it does not get stuck. Only valves with valve maintenance enabled in the thermostat's settings take part.

The action fails with a message when no valve of the thermostat has maintenance enabled, or when a maintenance run is already in progress.

This action takes no fields.

```yaml
action: better_thermostat.run_valve_maintenance
target:
  entity_id: climate.living_room
```

### `better_thermostat.reset_pid_learnings`

Clears everything the PID controller has learned for this thermostat, for every TRV and every target temperature. Without further fields the controller starts again from its built-in gains.

| Field | Required | Description |
| --- | --- | --- |
| `apply_pid_defaults` | no | When `true`, writes starting gains for the current target and the targets 0.5 °C above and below it, on every TRV. Default `false`. |
| `defaults_kp` | no | Proportional gain to write, 0 to 1000. Without it the built-in 60 is used. |
| `defaults_ki` | no | Integral gain to write, 0 to 2. Without it the built-in 0.01 is used. |
| `defaults_kd` | no | Derivative gain to write, 0 to 10000. Without it the built-in 2000 is used. |

The three gain fields only take effect together with `apply_pid_defaults`. With `apply_pid_defaults` the thermostat needs a target temperature; without one the action fails and nothing is reset. The PID numbers on the device show the new gains as soon as the action has run.

```yaml
action: better_thermostat.reset_pid_learnings
target:
  entity_id: climate.living_room
data:
  apply_pid_defaults: true
  defaults_kp: 40
```

## Device conditions

In the automation editor, **Add condition → Device**, a Better Thermostat device offers two conditions.

| Condition type | True while | Values |
| --- | --- | --- |
| `is_hvac_mode` | The thermostat is in the chosen HVAC mode. | `off`, `heat`, `heat_cool` |
| `is_hvac_action` | The thermostat's current action is the chosen one. | `off`, `heating`, `cooling`, `idle` |

The mode is what the thermostat is set to; the action is what it is doing right now. A thermostat set to `heat` reports `idle` while the room is warm enough. A thermostat reports `cooling` only when a cooler is configured.

```yaml
condition: device
device_id: 0123456789abcdef0123456789abcdef
domain: better_thermostat
entity_id: climate.living_room
type: is_hvac_action
hvac_action: heating
```

The device triggers are described with the blueprints that use them, in [How device triggers work](/setup/automation-blueprints/#how-device-triggers-work).
