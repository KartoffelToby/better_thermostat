---
title: Invalid external temperature
description: What the "invalid external temperature" repair issue means and how to fix it.
slug: faq/invalid-external-temperature
---

Better Thermostat raises this repair issue when the room temperature sensor of a Better Thermostat reports a value it cannot use. The issue names the Better Thermostat and the value the sensor reported.

A value is rejected when it is not a number, or when it lies outside −50 °C to 60 °C. A sensor that reports in Fahrenheit is converted first, so the range is −58 °F to 140 °F. `unknown` and `unavailable` do not raise this issue; they count as a missing sensor and lead to [degraded mode](/faq/degraded-mode) instead.

## What Better Thermostat does

It ignores the reading and keeps regulating on the last room temperature it accepted. The next valid reading clears the issue on its own.

If the sensor keeps sending invalid values, the room has no usable temperature. After about two minutes Better Thermostat falls back to the temperatures the TRVs measure themselves, as described in [Degraded mode](/faq/degraded-mode).

## Common causes

- The entity selected as room temperature sensor is not a temperature sensor, for example a battery level, a humidity sensor or a text sensor.
- The sensor's integration publishes a placeholder value while it has no reading. Some devices report fixed values such as 127 °C to signal a state instead of a temperature.
- The sensor reports in a unit other than °C or °F, such as kelvin. Better Thermostat reads such a number as °C.
- The sensor is faulty or its battery is too low to measure correctly.

## How to fix it

1. Open the sensor named in the issue under **Settings → Devices & services → Entities** and look at its history. Check that the values are room temperatures and that its unit is °C or °F.
2. If the selected entity is the wrong one, open the Better Thermostat entry, choose **Configure** and pick the right sensor under **Temperature Sensor**.
3. If the sensor sometimes publishes placeholder values, filter them out with a [template sensor](https://www.home-assistant.io/integrations/template/) or the [filter integration](https://www.home-assistant.io/integrations/filter/) and select that sensor instead.
4. If the sensor is faulty, replace its battery or the sensor.
