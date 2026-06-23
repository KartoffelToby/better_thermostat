---
title: Window sensor states
description: What the "invalid window sensor state" repair issue means and how to fix it.
slug: faq/window-sensor
---

Better Thermostat expects the configured window sensor to behave like a
binary sensor:

- `on`, `true` or `open` — window is open, heating pauses
- `off`, `false` or `closed` — window is closed, heating resumes
- `unknown` or `unavailable` — Better Thermostat assumes the window is
  closed so heating continues (a lost sensor must not stop heating); the
  unavailability is still reported

If the sensor reports anything else, Better Thermostat raises an
**invalid window sensor state** repair issue and ignores the state
change.

## Common causes

- The configured entity is not a binary sensor — for example a numeric
  sensor, an input helper with custom values, or a template that returns
  something other than `on`/`off`.
- A group helper that aggregates non-binary entities.

## How to fix it

1. Check the sensor's actual state under **Developer tools → States**.
2. Use a `binary_sensor` (device class `window`/`door`/`opening`), or a
   group of binary sensors:

   ```yaml
   group:
     livingroom_windows:
       name: Livingroom Windows
       icon: mdi:window-open-variant
       all: false
       entities:
         - binary_sensor.openclose_1
         - binary_sensor.openclose_2
   ```

3. If you template your own sensor, make sure it only ever renders
   `on` or `off`.
