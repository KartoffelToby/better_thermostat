---
title: Door sensor states
description: What the "invalid door sensor state" repair issue means and how to fix it.
slug: faq/door-sensor
---

Better Thermostat expects the configured door sensor to behave like a
binary sensor:

- `on`, `true` or `open` — door is open, heating pauses
- `off`, `false` or `closed` — door is closed, heating resumes
- `unknown` or `unavailable` — Better Thermostat assumes the door is
  open as a precaution

If the sensor reports anything else, Better Thermostat raises an
**invalid door sensor state** repair issue and ignores the state change.

## Common causes

- The configured entity is not a binary sensor — for example a numeric
  sensor, an input helper with custom values, or a template that returns
  something other than `on`/`off`.
- A group helper that aggregates non-binary entities.

## How to fix it

1. Check the sensor's actual state under **Developer tools → States**.
2. Use a `binary_sensor` (device class `door`/`opening`), or a group of
   binary sensors:

   ```yaml
   group:
     hallway_doors:
       name: Hallway Doors
       icon: mdi:door-open
       all: false
       entities:
         - binary_sensor.frontdoor_contact
         - binary_sensor.balcony_contact
   ```

3. If you template your own sensor, make sure it only ever renders
   `on` or `off`.

## Door and window sensors are independent

Doors are configured separately from windows and carry their own delays,
**Delay before the thermostat should turn off when the door is opened**
and its counterpart for closing. A hallway door that is opened briefly and
often is usually given a longer opening delay than a window, so short
passages do not interrupt heating.

Both contacts suppress heating on the same terms, and they combine: heating
resumes only once *every* configured contact reports closed. See
[Window sensor states](/faq/window-sensor) for the window side.
