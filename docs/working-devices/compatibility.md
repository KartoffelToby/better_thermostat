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
