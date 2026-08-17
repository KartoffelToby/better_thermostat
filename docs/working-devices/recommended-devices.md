---
title: Recommended Devices
description: The best TRVs and sensors to use with Better Thermostat.
---

When choosing a Smart Radiator Valve (TRV) or temperature sensor for Better Thermostat, hardware capabilities and integration quality differ enough to change how well Better Thermostat can control them.

## What makes a TRV a good fit

The best TRVs for Better Thermostat have the following features:
1. **Local Temperature Calibration**: Allows Better Thermostat to send an offset to the TRV, making the TRV's internal logic work with your external room sensor.
2. **Direct Valve Control**: Allows Better Thermostat to directly set the valve opening percentage (e.g., 0-100%). Every algorithm runs without it, but MPC Predictive and PID Controller gain the most from it.
3. **Fast Reporting**: Sends temperature and state updates frequently without aggressive battery-saving sleep modes that delay commands.

## Recommended TRVs

Based on community feedback and what the integrations expose:

### 1. Sonoff TRVZB

- Good Zigbee2MQTT support, fast response times and reliable local temperature calibration. It also has specific model fixes built into Better Thermostat to ensure smooth operation.
- Suggested algorithms: AI Time Based, Aggressive or MPC Predictive.

### 2. Eurotronic Spirit Zigbee (SPZB0001)

- One of the few TRVs that reliably supports **Direct Valve Control**, which is the capability MPC Predictive and PID Controller gain the most from.
- Suggested algorithms: MPC Predictive or PID Controller.

### 3. Moes / Tuya TS0601 Series

- Very affordable and widely available. Better Thermostat includes extensive model fixes for these devices to correct their often-quirky default behaviors and make them reliable.
- Suggested algorithms: AI Time Based or MPC Predictive.

### 4. Tado Smart Radiator Thermostats

- Solid build quality and native integration support for local calibration.
- Suggested algorithm: AI Time Based.

## External sensors

Better Thermostat relies heavily on accurate room temperature readings. The internal sensor of a TRV is too close to the radiator to be accurate. **You must use an external room sensor for the best experience.**

### What makes a good external sensor

- **Frequent Updates**: The sensor should report temperature changes of 0.1°C or 0.2°C immediately.
- **Placement**: Place it at eye level, away from direct sunlight, drafts, and the radiator itself.

### Suggestions

- **Aqara Temperature and Humidity Sensor**: Affordable, reliable, and updates frequently enough for most rooms.
- **Xiaomi Mijia Bluetooth/Zigbee Sensors**: Very accurate and easy to place anywhere.
- **Shelly Temperature Sensor**: long battery life and good precision.

## Window and door sensors

A window sensor lets Better Thermostat pause the heating as soon as a window opens, instead of letting the TRV heat the outside.

### What makes a good window sensor

- **Instant Reporting**: The sensor must report its state change (open/closed) immediately without delay.
- **Reliability**: It shouldn't drop off the network or miss state changes.

### Suggestions

- **Aqara Door and Window Sensor**: reliable, reports instantly, small.
- **Sonoff SNZB-04**: Affordable, uses standard Zigbee, and reports instantly.
- **Shelly Door/Window 2**: a Wi-Fi option if you have no Zigbee network; includes tilt and temperature sensors.

## Integrations

For the Zigbee TRVs above we recommend **Zigbee2MQTT (Z2M)**: it exposes the local-calibration and valve-position entities Better Thermostat writes to, where ZHA and deCONZ do not always expose them. Which of the two exists depends on the TRV as much as on the integration. Where neither does, the setup form preselects the **Target Temperature Based** calibration type and Better Thermostat steers the device through its setpoint instead. Tado is the exception in the list above: it runs on its own native integration.

If you are looking for a good Zigbee coordinator, the [SONOFF Zigbee 3.0 USB Dongle Plus](https://amzn.to/4rt9aWt) is a popular choice that works well with Zigbee2MQTT and supports a wide range of devices. If you also want to support Home Assistant itself, consider the [ZBT-2](https://www.home-assistant.io/connect/zbt-2/): it costs a little more and Home Assistant maintains it as a first-party Zigbee coordinator.

## Tested setups

Complete setups that people run successfully.

### Setup 1: The "SEA801/SEA802" Setup

An affordable setup built from widely available Zigbee devices.

- **TRV**: SEA801-Zigbee / SEA802-Zigbee (TS0601) [Buying Link](https://amzn.to/4aDFMW3)
- **Room Sensor**: Aqara Temperature and Humidity Sensor (WSDCGQ11LM / lumi.weather) [Buying Link](https://amzn.to/40jCClw)
- **Window Sensor**: Aqara Door and Window Sensor (MCCGQ11LM / lumi.sensor_magnet.aq2) [Buying Link](https://amzn.to/4cB4zwz)
- **Weather Integration**: Meteorologisk institutt (Met.no)

**Configuration:**
- **Algorithm**: MPC Predictive or AI Time Based
- **Calibration Type**: Local Calibration (Default)
- **Important Note for Large Temperature Gaps**: The SEA801/SEA802 TRVs only allow a small offset calibration via Zigbee2MQTT. If you have a room with a **large temperature difference** between the radiator and the room sensor, the standard local offset calibration won't be enough. In this specific case, you need to switch the Calibration Type to **Target Temperature Based**. This bypasses the TRV's offset limit and directly manipulates the target temperature to achieve the desired room temperature.

**Pros:**
- Silent operation
- Can handle local calibration
- Good if you want to change the target temperature on the TRV itself

**Cons:**
- Does not support direct valve control

### Setup 2: The "Eurotronic Spirit" Setup

This setup uses the powerful Eurotronic Spirit TRV, which supports both local offset and direct valve control.

- **TRV**: Eurotronic Spirit Zigbee (SPZB0001) [Buying Link](https://amzn.to/40mKByg)
- **Room Sensor**: Aqara Temperature and Humidity Sensor (WSDCGQ11LM / lumi.weather) [Buying Link](https://amzn.to/40jCClw)
- **Window Sensor**: Aqara Door and Window Sensor (MCCGQ11LM / lumi.sensor_magnet.aq2) [Buying Link](https://amzn.to/4cB4zwz)
- **Weather Integration**: Meteorologisk institutt (Met.no)

**Configuration:**
- **Algorithm**: AI Time Based or Aggressive
- **Calibration Type**: Local Calibration or Valve Control (both work well)

**Pros:**
- Can handle both valve-based and local-based calibration
- Strong motor
- Reacts quickly

**Cons:**
- Loud operation
- When using valve calibration mode, it's not possible to set the target temperature on the TRV itself
