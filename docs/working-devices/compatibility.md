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

## Direct valve control (recommended for MPC/PID)

Direct valve control requires both:

1. Adapter support in Better Thermostat
2. A controllable valve position entity exposed by your TRV/integration

Devices that expose a controllable valve position work best with the advanced control modes (MPC/PID), because BT can set the valve opening directly.

### Shelly BLU TRV

Better Thermostat supports direct valve control through Home Assistant's official
Shelly integration and a writable valve-position `number` belonging to the TRV.
Discovery uses device and entity metadata, so renaming the entity is supported,
including when several TRVs share one BLU Gateway Gen3.

To prepare a device:

1. Pair the BLU TRV with its gateway and add the gateway to the Shelly integration.
2. Manually disable the TRV's automatic temperature control in Shelly's settings.
   [Shelly requires this before accepting position commands](https://shelly-api-docs.shelly.cloud/docs-ble/Devices/BLU_ZB/trv/#trvsetposition).
   Home Assistant exposes the valve-position number in this mode. Ensure it is
   enabled, available and writable; a read-only position sensor is insufficient.
3. Configure Better Thermostat with the Shelly climate entity and an external
   Home Assistant room temperature sensor. The sensor can use any integration,
   including Matter-over-Thread; a Shelly BLU H&T is not required.
4. Select **Direct Valve Based** and **Heating Power** (the default), **MPC**,
   **MPC v2**, **PID**, or **TPI**. MPC v2 retains its existing `daqp` solver
   requirement. Normal, Aggressive and No calibration are rejected because they
   do not produce the valve output required by this mode.

Use the **Better Thermostat UI** for temperature, OFF and Boost. During regulation,
BT owns the valve and sends no physical climate setpoint, HVAC mode or local offset
commands. Standard BT startup adjustments still apply: where matching entities
exist, BT resets the local calibration offset, synchronizes the configured child
lock and disables internal window detection and away mode.
Physical climate setpoint/mode reports do not change BT's target or mode; internal
temperature and telemetry remain available. OFF, an open window/door contact or
disabled heat demand closes the valve. Boost opens it to the configured maximum
only when heating is permitted. Valve maintenance opens and closes the valve,
then ordinary control recalculates demand without restoring a climate setpoint.
The Shelly adapter serializes writes per TRV. Maintenance uses BT's existing
cycle and interruption handling; this support adds no recovery close command.
An exercise interrupted after opening may leave the valve open until a subsequent
command reaches it.

If the number disappears or becomes unavailable, BT retains exclusive valve
ownership and retries discovery on subsequent control cycles. It does not fall
back to climate control. Home Assistant and communication with the gateway are
part of the active control loop: during an outage the valve may retain its last
position, and the Shelly internal thermostat does not automatically take over.
This support adds no offline watchdog.

To return to the internal thermostat, turn BT OFF and confirm the valve closes,
then manually re-enable Shelly automatic temperature control. Change BT to a
normal mode such as Target Temperature Based (or disable BT if Shelly alone will
control the room). The configuration reload restores ordinary climate commands.
Neither a position write nor a target-temperature write is treated as an
automatic handover between Shelly modes.

Software tests use simulated devices. Validate one real TRV first: intermediate
positions, OFF, contacts, Boost, maintenance, restart and both manual handovers.

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

If your preferred integration is missing, please open an issue:

- https://github.com/KartoffelToby/better_thermostat/issues
