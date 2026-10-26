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
- Z-Wave JS, when the TRV exposes a calibration entity. A configuration parameter is not used as one: its value counts in the parameter's own unit (the Eurotronic Spirit's temperature offset, parameter 8, counts tenths of a degree), so there is no need to enable it for Better Thermostat
- Any other integration whose TRV exposes a calibration entity (a `number` or `select`) that Better Thermostat can find

## Direct valve control (recommended for MPC/PID)

Direct valve control requires both:

1. Adapter support in Better Thermostat
2. A controllable valve position entity exposed by your TRV/integration

Devices that expose a controllable valve position work best with the advanced control modes (MPC/PID), because BT can set the valve opening directly. Select the **Direct Valve Based** calibration type to use it.

### Shelly BLU TRV (direct valve control)

Use Home Assistant's native Shelly integration with a compatible BLU gateway. The BLU TRV's own automatic temperature control must be disabled in Shelly's settings. Home Assistant then exposes its writable **Valve position** `number` instead of the read-only position sensor. Wait for the number to become available before configuring Better Thermostat. It must belong to the same TRV device as the `climate` entity; a gateway can serve several TRVs.

In Better Thermostat, select the physical TRV and the room temperature sensor, then set the **Calibration type** to **Direct Valve Based** and the **Calibration mode** to **AI Time Based**, **MPC**, **MPC v2**, **PID** or **TPI**. The calibration modes External Sensor Offset Only (stored as Normal), Aggressive and No Calibration do not produce a valve opening and are rejected for this combination in both setup and options. A stored configuration with an incompatible or unknown calibration mode raises a Repairs issue; BT requests 0% and blocks opening that valve, including Boost, until its options are corrected. The entry stays loaded to retry closure if the device is temporarily unreachable. BT does not replace the saved calibration mode automatically.

During regulation, the valve percentage is the only actuator: Better Thermostat does not send a setpoint, HVAC mode or calibration offset to the BLU TRV. A device that still reports a target temperature follows the same rule. Change the room's target and mode on Better Thermostat; setpoint or mode reports from the physical TRV do not change them. Temperature and availability reports are still read. Periodic reconciliation checks the valve position, leaving differences in thermostat settings alone. An OFF or open-contact decision closes the valve rather than switching the device's thermostat off. Standard startup tweaks and the common valve-maintenance routine are unchanged.

This policy is specific to an adopted BLU TRV valve. The Shelly TRV Gen1 also exposes a valve number, but its thermostat keeps running; that number does not enable this direct-control path. Ordinary target-temperature and offset control retain their existing behaviour.

#### Switching back to Shelly's temperature control

First set Better Thermostat to OFF and wait until the valve reports 0%. Stop any other automation that writes the valve. Re-enable automatic temperature control in Shelly, wait for Home Assistant to update the device's entities, then set the **Calibration type** in Better Thermostat to **Target Temperature Based** (or stop using Better Thermostat for that device). For direct control again, disable Shelly's automatic control, wait for the writable number, and set the calibration type to Direct Valve Based with a supported calibration mode. Do not switch Shelly modes while Better Thermostat is actively controlling the valve.

#### Unavailable devices and outages

An unavailable valve number does not cause Better Thermostat to fall back to setpoint or HVAC commands. The normal write budget and retry handling apply; there is no extra sleep inside the control lock for BLU TRVs. Restore the gateway connection or reload the Shelly integration after a firmware update if its entities do not recover. If an entity was disabled, re-enable it; if the device's mode changed, restore the intended mode and reconfigure BT.

Direct control depends on Home Assistant, the gateway and the Bluetooth link. If these stop working, Better Thermostat cannot send commands and the last valve position may remain in effect. If neither the room sensor nor any TRV temperature is usable and BT enters HOLD, it requests a fixed emergency opening of 25% for a valve-only device because its thermostat cannot take over. This preserves limited heating without retaining a potentially fully open valve, and Boost does not override it. Normal regulation resumes after the sensor-recovery stability period. The command uses the normal write and retry path and requires a reachable device. OFF, open windows or doors, and an invalid stored calibration mode still request closure; the invalid configuration also raises a repair issue. A fixed opening cannot guarantee frost protection or provide a local automatic shutoff or boiler timeout. Any zone-relay timeout or independent temperature limit belongs outside Better Thermostat.

## Room temperature written to the TRV

Some TRVs can regulate on a room temperature sent to them instead of their own sensor. For these models Better Thermostat writes its room temperature to the device:

- **Sonoff TRVZB and TRV-ZBT** (Zigbee2MQTT): to `external_temperature_input`, and switches the device's temperature sensor selection to it.
- **Danfoss Ally, Popp 701721, Hive UK7004240** (Zigbee2MQTT): to `external_measured_room_sensor`, in hundredths of a degree Celsius as Zigbee2MQTT expects, but only with the calibration mode **No Calibration**. The device corrects for its own sensor itself once it has the room temperature, so any other calibration mode would correct the same bias a second time. A change of at least 0.1 °C is written at once. An unchanged value goes out again with Better Thermostat's 30-minute refresh, which stays within the time after which the device drops the value and returns to its own sensor: 3 hours, or 35 minutes with `radiator_covered` on. Better Thermostat does not change `radiator_covered`; set it to match how the radiator is mounted. Open windows are not reported through `window_open_external`, and `heat_available` is not used.

No value is written while the room sensor gives no reading, or when the room sensor belongs to the TRV itself.

## Devices that are known to work

Better Thermostat includes specific fixes and optimizations for the following devices to ensure they work correctly:

- 014G2461 (Danfoss Ally, also Danfoss 014G2463, Popp 701721 and Hive UK7004240)
- BHT-002-GCLZB
- BTH-RM
- BTH-RM230Z
- COZB0001 (Eurotronic Comet Zigbee)
- Heater (TermoWeb electric radiator, through the ha-termoweb integration)
- ME167 (Avatto)
- SEA801-Zigbee / SEA802-Zigbee
- SHTRV-01 (Shelly TRV, Gen1)
- SPZB0001 (Eurotronic Spirit Zigbee)
- TRV-ZBT (Sonoff TRV-ZBT)
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

Many TRVs work in Celsius. On a system set to Fahrenheit, Home Assistant shows their setpoint in °F, rounded to a whole degree unless the integration states a finer precision. Zigbee2MQTT does not state one. A setpoint of 71.5 °F would come back from such a TRV as 71 °F or 72 °F, never as written, so Better Thermostat sends it whole degrees only: a target of 70.5 °F reaches the TRV as 70 °F or 71 °F. The TRV's own half-degree Celsius step is 0.9 °F, so little precision is lost. Your target stays 70.5 °F, and a turn of one step at the TRV still shows up as a new target. A turn small enough that Home Assistant shows the same whole degree does not show up, so Better Thermostat cannot see that turn either. The same applies to a cooler that Home Assistant shows in whole degrees.

### Learning modes need time, and some are still in testing

AI Time Based, MPC Predictive and PID Controller learn from the heating cycles they observe, so they get better over days, not minutes. MPC Predictive and PID Controller are in beta and MPC v2 is experimental; in the project's benchmark MPC Predictive does not overshoot less than the other controllers and can over-react with several radiators in one room. See [Calibration Algorithms](/calibration_algorithms/).
