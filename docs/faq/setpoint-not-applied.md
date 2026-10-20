---
title: Target temperature not taken
description: What the "does not take the target temperature" repair issue means and how to fix it.
slug: faq/setpoint-not-applied
---

Better Thermostat raises this repair issue when one of its TRVs keeps
reporting a different target temperature from the one Better Thermostat
writes to it. The issue names the TRV, the target temperature Better
Thermostat wants and the one the TRV reports, as of the latest control cycle.
The TRV is also listed in the thermostat's `errors` attribute, so the
`device_error` device trigger and the [device error blueprint](/setup/automation-blueprints/) fire for it.

## When it is raised

A write that gets lost on the radio is not an error. Better Thermostat
compares the target temperature the TRV reports with the one it wants on
every control cycle and writes it again when they differ. A difference of up
to one step of the TRV is treated as taken, because some devices round a
written value onto their own grid.

When the TRV does not take the value, the second write follows after the
normal 30 seconds between writes (ten minutes for a HomematicIP head), and
every further one waits twice as long as the one before, up to one hour.
The issue is raised when the TRV still does not hold the value after six
writes in a row. At the 30 second interval the waits are 30 seconds, 1, 2, 4
and 8 minutes, so the issue appears about a quarter of an hour after the
first write. A HomematicIP head waits 10, 20 and 40 minutes and then one hour
twice, so its issue appears a little over three hours after the first write.
The waits keep doubling after that until they reach one hour, and from then
on Better Thermostat writes the target temperature once an hour. When you
change the room's target temperature, the next write goes out at the normal
pace.

## Common causes

- The TRV has a setpoint limit of its own that is lower than the maximum it
  publishes to Home Assistant. Some calibration modes ask a TRV for a target
  temperature well above the room's target, up to its published maximum, and
  the TRV refuses or caps it. In Zigbee2MQTT, check settings such as
  `max_heat_setpoint_limit` and `min_heat_setpoint_limit`.
- The TRV is in a mode or preset in which it ignores target temperatures,
  for example a schedule or programme mode.
- The TRV lost its radio connection, or its battery is low.

## How to fix it

1. Open the TRV's device page in Home Assistant and compare its target
   temperature with the one named in the issue.
2. If the TRV holds a lower value than asked for, raise its own setpoint
   limit to at least the maximum it publishes.
3. Make sure the TRV is in a manual or heating mode in which it follows the
   target temperature it is sent.
4. Check the battery and the radio connection.

The issue clears on its own, and the TRV leaves the `errors` attribute, as
soon as the TRV reports the target temperature Better Thermostat wants or the
last one it wrote.
