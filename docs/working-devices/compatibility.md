---
title: Compatibility
description: Understand what works out of the box and where advanced support depends on your TRV.
---

## Core compatibility

If your thermostat is available as a Home Assistant `climate` entity, Better Thermostat can usually control it.

## Local calibration compatibility

Local calibration requires both:

1. Adapter support in Better Thermostat
2. A compatible offset capability on your TRV/integration

Currently, integrations with local calibration support include:

- Tado
- Zigbee2MQTT
- deCONZ
- Z-Wave JS, when the TRV exposes a calibration entity
- Any other integration whose TRV exposes a calibration entity (a `number` or `select`) that Better Thermostat can find

## Direct valve control (recommended for MPC/PID)

Direct valve control requires both:

1. Adapter support in Better Thermostat
2. A controllable valve position entity exposed by your TRV/integration

Devices that expose a controllable valve position work best with the advanced control modes (MPC/PID), because BT can set the valve opening directly. Select the **Direct Valve Based** calibration type to use it.

## Devices that are known to work

Better Thermostat includes specific fixes and optimizations for the following devices to ensure they work correctly:

- BHT-002-GCLZB
- BTH-RM
- BTH-RM230Z
- COZB0001 (Eurotronic Comet Zigbee)
- ME167 (Avatto)
- SEA801-Zigbee / SEA802-Zigbee
- SPZB0001 (Eurotronic Spirit Zigbee)
- TRVZB (Sonoff TRVZB)
- TS0601
- TS0601_thermostat
- TV02-Zigbee
- ZWA021 (Eurotronic Spirit Z / Aeotec ZWA021, Z-Wave)

If your preferred integration is missing, please open an issue:

- https://github.com/KartoffelToby/better_thermostat/issues

## Known limitations

### Better Thermostat can only use what your TRV exposes

Better Thermostat has no hardware and no radio of its own. It works through the entities the TRV's integration creates in Home Assistant, so the integration decides what is possible:

- **Offset Based** calibration needs a calibration offset entity (a `number` or a `select`) that Better Thermostat can find on the TRV's device. Some integrations hide that entity by default, HomematicIP among them; see [Configuration](/configuration/).
- **Direct Valve Based** calibration needs a valve position the TRV lets Better Thermostat write. Without it, the MPC, TPI and PID controllers reach the valve only through the setpoint the TRV sees, and the TRV's own regulation still decides how far the valve opens.
- **Target Temperature Based** works with every TRV. It is what the setup preselects when the TRV offers neither an offset nor a writable valve.
- A TRV reports only as often as its integration and firmware let it. Better Thermostat cannot make a TRV report more often, and it spaces its own writes to each TRV at least 30 seconds apart, or ten minutes apart with the HomematicIP option.

### The room sensor decides the result

Every calibration mode regulates on the room temperature sensor. A sensor next to the radiator, in direct sunlight, in a draught or on an outside wall gives every algorithm a wrong picture of the room, and no setting makes up for that. Place it at a spot that represents the room, away from heat sources.

Better Thermostat notices a sensor that becomes unavailable or reports values it cannot use, and then falls back to the TRVs' own temperatures (see [Degraded mode](/faq/degraded-mode)). It does not notice a sensor that stops sending new values while Home Assistant still shows the last one as current. Whether such a sensor turns unavailable is up to its integration: ZHA marks a device unavailable after a while without messages, Zigbee2MQTT does so only with its *availability* option turned on, and an MQTT sensor only with `expire_after` set. With that in place, Better Thermostat falls back as described above.

### One TRV, one Better Thermostat

A TRV belongs to exactly one Better Thermostat. The setup and the settings refuse a TRV that another Better Thermostat already controls, and two that still share one from an older setup get a [repair issue](/faq/shared-trv). To control several TRVs as one thermostat, put them into the same Better Thermostat; they then share one target temperature.

### No boiler, no schedule

Better Thermostat controls TRVs and an optional cooler. It does not switch a boiler or set a flow temperature; see [A central thermostat alongside TRVs](/deep-explanations/central-heating-thermostat/). It has no built-in schedule either: presets are temperatures, and switching between them on a timetable is a job for an automation or the [schedule blueprint](/setup/automation-blueprints/).

### Humidity is shown, not used

The humidity sensor is shown on the thermostat and can trigger automations. The control does not use it.

### The weather entity needs a forecast

The outdoor temperature check reads the weather entity's forecast. When the weather entity offers no forecast or does not answer, Better Thermostat keeps its last decision for three hours and then heats until a forecast comes back. If your weather integration has no forecasts, use an outdoor temperature sensor instead.

### Temperature differences stay in kelvin

On a system set to Fahrenheit, temperatures are shown in °F, but rates and differences are not converted: *Temperature Slope*, *Heating Power*, *Heat Loss*, *MPC Gain*, *MPC Loss* and *MPC v2 Disturbance* are in kelvin per minute and *PID Error* is in kelvin. One kelvin is a difference of 1.8 °F. See [Entities](/setup/entities/).

### A Celsius TRV on a Fahrenheit system reports whole degrees

Many TRVs work in Celsius. On a system set to Fahrenheit, Home Assistant shows their setpoint in °F, rounded to a whole degree unless the integration states a finer precision. Zigbee2MQTT does not state one. When Better Thermostat writes 71.5 °F, such a TRV shows 71 °F or 72 °F. Better Thermostat counts that as its own write coming back, so it does not take the rounded value as your new target. The flip side: turning such a TRV by less than one whole degree Fahrenheit does not show up in Home Assistant, so Better Thermostat cannot see that turn either.

### Learning modes need time, and some are still in testing

AI Time Based, MPC Predictive and PID Controller learn from the heating cycles they observe, so they get better over days, not minutes. MPC Predictive and PID Controller are in beta and MPC v2 is experimental; in the project's benchmark MPC Predictive does not overshoot less than the other controllers and can over-react with several radiators in one room. See [Calibration Algorithms](/calibration_algorithms/).
