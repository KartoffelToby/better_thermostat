---
title: Common questions
description: Quick answers to the most common Better Thermostat questions.
---

## What devices are supported?

In general, all thermostats available as Home Assistant `climate` entities are supported for core control.

Local calibration support depends on adapter support and TRV capabilities. Current integrations with local calibration support include:

- Tado
- Zigbee2MQTT
- deCONZ

For device-level details, see [Working devices](/working-devices/compatibility/).

## Local calibration vs target temperature calibration — what is the difference?

- **Local calibration (offset based)**: BT adjusts TRV offset so TRV reading matches room sensor.
- **Target temperature based**: BT adjusts requested target temperature to compensate internally.

Choose offset-based where available; use target-based for broad compatibility.

## Which algorithm should I start with?

Start with **AI Time Based**. It is the best default for most homes.

If you want tighter overshoot control and have stable sensors, try **MPC Predictive**.

## Where do I find advanced tuning info?

See:

- [Recommended settings](/optimal-settings/recommended-settings/)
- [Algorithm selection](/optimal-settings/algorithm-selection/)
- [Hydraulic balance explanation](/deep-explanations/hydraulic-balance/)
