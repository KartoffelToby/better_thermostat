---
title: Recommended Devices
description: The best TRVs and sensors to use with Better Thermostat.
---

When choosing a Smart Radiator Valve (TRV) or temperature sensor for Better Thermostat, some devices perform significantly better than others due to their hardware capabilities and how they integrate with Home Assistant.

## What makes a TRV "Good" for Better Thermostat?

The best TRVs for Better Thermostat have the following features:
1. **Local Temperature Calibration**: Allows Better Thermostat to send an offset to the TRV, making the TRV's internal logic work with your external room sensor.
2. **Direct Valve Control**: Allows Better Thermostat to directly set the valve opening percentage (e.g., 0-100%). This is **highly recommended** for advanced algorithms like MPC Predictive and PID Controller to work optimally.
3. **Fast Reporting**: Sends temperature and state updates frequently without aggressive battery-saving sleep modes that delay commands.

## Top Recommended TRVs

Based on community feedback and integration capabilities, here are some of the most recommended TRVs:

### 1. Sonoff TRVZB
- **Why it's great**: Excellent Zigbee2MQTT support, fast response times, and reliable local temperature calibration. It also has specific model fixes built into Better Thermostat to ensure smooth operation.
- **Best Algorithm**: AI Time Based or Aggressive or MPC.

### 2. Eurotronic Spirit Zigbee (SPZB0001)
- **Why it's great**: One of the few TRVs that reliably supports **Direct Valve Control**. This makes it the absolute best choice if you want to use the advanced **MPC Predictive** or **PID Controller** algorithms.
- **Best Algorithm**: MPC Predictive or PID Controller.

### 3. Moes / Tuya TS0601 Series
- **Why it's great**: Very affordable and widely available. Better Thermostat includes extensive model fixes for these devices to correct their often-quirky default behaviors and make them reliable.
- **Best Algorithm**: AI Time Based or MPC.

### 4. Tado Smart Radiator Thermostats
- **Why it's great**: Premium build quality and excellent native integration support for local calibration.
- **Best Algorithm**: AI Time Based.

## Recommended External Sensors

Better Thermostat relies heavily on accurate room temperature readings. The internal sensor of a TRV is too close to the radiator to be accurate. **You must use an external room sensor for the best experience.**

### What makes a good external sensor?
- **Frequent Updates**: The sensor should report temperature changes of 0.1°C or 0.2°C immediately.
- **Placement**: Place it at eye level, away from direct sunlight, drafts, and the radiator itself.

### Top Picks:
- **Aqara Temperature and Humidity Sensor**: Affordable, reliable, and updates frequently enough for most rooms.
- **Xiaomi Mijia Bluetooth/Zigbee Sensors**: Very accurate and easy to place anywhere.
- **Shelly Temperature Sensor**: Excellent battery life and precision.

## Recommended Window/Door Sensors

Window sensors are crucial for Better Thermostat to quickly turn off the heating when a window is opened, saving energy and preventing the TRV from trying to heat the outside.

### What makes a good window sensor?
- **Instant Reporting**: The sensor must report its state change (open/closed) immediately without delay.
- **Reliability**: It shouldn't drop off the network or miss state changes.

### Top Picks:
- **Aqara Door and Window Sensor**: Extremely reliable, instant reporting, and very small form factor.
- **Sonoff SNZB-04**: Affordable, uses standard Zigbee, and reports instantly.
- **Shelly Door/Window 2**: Great Wi-Fi option if you don't have a Zigbee network, includes tilt and temperature sensors.

## Recommended Integrations

To get the most out of these devices, we highly recommend using **Zigbee2MQTT (Z2M)**. It provides the most granular control over TRV entities, exposing calibration and valve control entities that other integrations (like ZHA or deCONZ) sometimes hide or don't support fully.

if you looking for a good Zigbee coordinator, the [SONOFF Zigbee 3.0 USB Dongle Plus](https://amzn.to/4rt9aWt) is a popular choice that works well with Zigbee2MQTT and supports a wide range of devices. But, if you want to support even HomeAssistant itself, you better get the [ZBT-2](https://www.home-assistant.io/connect/zbt-2/) its a bit more expensive but it has better performance and reliability, and it's also supported by HomeAssistant itself as a native Zigbee coordinator.

## Tested and Good Setups

Here are some examples of complete setups that have been tested and proven to work very well together.

### Setup 1: The "SEA801/SEA802" Setup
This is a highly reliable and affordable setup using popular Zigbee devices.

- **TRV**: SEA801-Zigbee / SEA802-Zigbee (TS0601) [Buying Link](https://amzn.to/4aDFMW3)
- **Room Sensor**: Aqara Temperature and Humidity Sensor (WSDCGQ11LM / lumi.weather) [Buying Link](https://amzn.to/40jCClw)
- **Window Sensor**: Aqara Door and Window Sensor (MCCGQ11LM / lumi.sensor_magnet.aq2) [Buying Link](https://amzn.to/4cB4zwz)
- **Weather Integration**: Meteorologisk institutt (Met.no)

**Recommended Configuration:**
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

**Recommended Configuration:**
- **Algorithm**: AI Time Based or Aggressive
- **Calibration Type**: Local Calibration or Valve Control (both work well)

**Pros:**
- Can handle both valve-based and local-based calibration
- Strong motor
- Reacts quickly

**Cons:**
- Loud operation
- When using valve calibration mode, it's not possible to set the target temperature on the TRV itself
