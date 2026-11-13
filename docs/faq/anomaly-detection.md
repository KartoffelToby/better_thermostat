---
title: Heating fault & anomaly detection
description: How sensorless heating fault and anomaly detection works and what its repair issues mean.
slug: faq/anomaly-detection
---

Better Thermostat can monitor the room temperature response against the
expected heating and cooling dynamics to detect heating faults and
environmental anomalies without additional hardware sensors.

## Configuration switches

- **Anomaly Detection** — enables sensorless fault and anomaly detection for
  the thermostat. When active, confirmed faults appear in the
  `active_anomalies` attribute on the climate entity, pause thermal model
  learning so fault data does not corrupt learned parameters, and raise Home
  Assistant repair issues for hardware faults.
- **Suspected Window Suppression** — when both **Anomaly Detection** and
  **Suspected Window Suppression** are enabled, a confirmed rapid heat loss
  (`rapid_heat_loss`) temporarily pauses heating as `suspected_window` for up
  to 60 minutes or until room temperature stops falling.

## Repair issues

### Heating supply failure (`heating_supply_failure`)

All radiator thermostats in the room have commanded significant heating for an
extended period while the room remains well below target temperature and fails
to warm.

**What to check:**
- Verify that the central boiler, heat pump, or circulation pump is switched on
  and supplying hot water.
- Check for system-wide fault codes or low water pressure on the heat source.

### Radiator thermostat not producing heat (`trv_no_heat_response`)

A single radiator thermostat in a multi-TRV room has commanded heating for an
extended period without delivering heat, while another thermostat in the room
responds normally.

**What to check:**
- Bleed air from the affected radiator.
- Inspect the mechanical valve pin beneath the thermostat head to ensure it is
  not seized closed.
- Verify that the thermostat head is firmly mounted on the valve body.

### Leaking or stuck-open radiator valve (`valve_leak_detected`)

The room continues to warm significantly above the target temperature even
though the radiator thermostat has been commanded closed (`0 %`) and residual
radiator heat has already dissipated.

When **Valve maintenance** is enabled for the affected thermostat, Better
Thermostat automatically triggers a one-shot valve exercise cycle (at most once
every 24 hours) to help dislodge scale or debris on the valve seat.

**What to check:**
- Inspect the thermostat head mounting and adapter ring to make sure the pin is
  pushed all the way closed.
- Remove the thermostat head and check whether the valve pin moves freely and
  seals when depressed.

All anomaly repair issues clear automatically once the room temperature
response returns to normal.
