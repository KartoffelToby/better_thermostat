---
title: Degraded mode
description: What the degraded-mode warning means and how Better Thermostat keeps controlling when sensors fail.
slug: qanda/degraded_mode
---

Better Thermostat raises a **degraded mode** repair issue when one of the
sensors it was configured with becomes unavailable — the room temperature
sensor, the window sensor, the humidity sensor, the outdoor sensor, or the
weather entity. The thermostat keeps running; the issue tells you that it
is working with less information than you configured.

During the first five minutes after startup the warning is suppressed so
that slow integrations (for example cloud-based weather providers) get
time to come online before you see it.

## What still works

Better Thermostat degrades step by step instead of stopping. The control
quality steps down a ladder, one rung at a time:

1. **Optimal** — the room sensor delivers; everything works as configured.
2. **Sensor fallback** — the room temperature sensor is unavailable, but
   at least one TRV reports its internal temperature. Better Thermostat
   then controls on the average of the TRV-internal temperatures. TRVs
   measure next to the hot valve, so expect less accuracy — but it is
   strictly better than controlling on a frozen last reading.
3. **Hold** — neither the room sensor nor any TRV temperature is usable
   (for example during a Zigbee outage). The controller stops adjusting
   and the TRVs keep their last commanded state. Frost protection stays
   enforced on every write.

A rung steps down after the loss has persisted for about two minutes, and
climbs back up after the sensors have been stable again for about five
minutes — short sensor flaps do not flip the behavior back and forth.

## How to see the current rung

The Better Thermostat climate entity exposes the rung as the
`control_mode` attribute (`optimal`, `sensor_fallback`, or `hold`),
along with `degraded_for_s` (how long the degradation has lasted) and
`unavailable_sensors`. The `calibrator_health` attribute reports per
TRV whether its calibration controller is healthy or has self-healed
(for example after a poisoned learning state) or shows oscillating
output. Check them under **Developer tools → States**.

## What you should do

- Check the listed sensors: battery, power, and whether the entity shows
  `unavailable` or `unknown` in Home Assistant.
- For cloud-based weather or outdoor entities, check the integration that
  provides them.

The repair issue disappears on its own once all configured sensors are
available again.
