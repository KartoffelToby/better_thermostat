---
title: Recommended TRVs, sensors & Zigbee
description: Buying and setup guide for the best radiator valves, external room sensors, contact sensors, and Zigbee integrations for Better Thermostat 2.0.
---

Hardware quality and how your Home Assistant integration exposes a device make a noticeable difference in comfort, motor noise, and battery life. See **[Supported devices & compatibility](/working-devices/compatibility/)** for the complete quirk matrix.

## What makes a radiator valve (TRV) shine with Better Thermostat?

1. **Direct Valve Control (`0–100 %`)**: The gold standard. Lets Better Thermostat position the valve pin directly (`Direct Valve Based`) without having to reverse-engineer the TRV's own thermostat firmware.
2. **Wide Local Offset or External Sensor Input**: Lets Better Thermostat feed your true room temperature into the TRV (`Offset Based`) while keeping the physical radiator screen showing your real target temperature.
3. **Quiet Motor & Reliable Wake-Up**: Smooth stepping without loud gear whining in bedrooms, and dependable Zigbee/Bluetooth reporting.

---

## Recommended TRVs

### 1. Sonoff `TRVZB` *(Best overall Zigbee value — supports Direct Valve!)*
- **Why we recommend it:** Fast Zigbee2MQTT support, quiet motor, exposes **Direct Valve Control (`0–100 %`)**, wide **`Offset Based`** calibration (down to `-12.7 °C`), *and* a dedicated external temperature register that Better Thermostat automatically mirrors your room sensor into.
- **Recommended Screen 2 setup:** `Direct Valve Based` + **`(AI) MPC v2 (QP + Kalman, experimental)`** or **`TPI Controller`** (or `(AI) Time Based (Default)`).
- *(Note on the newer **Sonoff `TRV-ZBT` Gen2**: Better Thermostat automatically mirrors your external room sensor into it too, so `Offset Based` / external-sensor regulation works great, though Gen2 does not expose direct `0–100 %` motor positioning.)*

### 2. Shelly BLU TRV *(Best non-Zigbee Direct-Valve choice)*
- **Why we recommend it:** Uses Bluetooth Low Energy via a **Shelly BLU Gateway Gen3** with Home Assistant's native Shelly integration. Once Shelly's internal auto-temperature mode is disabled, Better Thermostat drives the `0–100 %` valve motor directly with dedicated safety handling ([Shelly BLU setup rules](/working-devices/compatibility/#shelly-blu-trv-direct-valve-control--repair-warning)).
- **Recommended Screen 2 setup:** `Direct Valve Based` + **`(AI) MPC v2 (QP + Kalman, experimental)`** or **`TPI Controller`**.

### 3. Eurotronic Spirit Zigbee (`SPZB0001`) / Spirit Z-Wave (`ZWA021`)
- **Why we recommend it:** Strong mechanical motor and native **`Direct Valve Based`** support on both Zigbee2MQTT (`SPZB0001`) and Z-Wave JS (`ZWA021`). *(Motor noise is slightly louder than Sonoff/tado°, making it better suited to living rooms than light-sleeper bedrooms.)*
- **Recommended Screen 2 setup:** `Direct Valve Based` + **`TPI Controller`**, **`(AI) MPC v2`**, or **`PID Controller`**.

### 4. HomematicIP (`HmIP-eTRV` series) & tado°
- **Why we recommend them:** Rock-solid build quality and very quiet motors.
  - On **tado°**, use `Offset Based` or `Target Temperature Based` with **`(AI) Time Based (Default)`**, **`TPI Controller`**, or **`External Sensor Offset Only`**.
  - On **HomematicIP / OpenCCU**, un-ignore `TEMPERATURE_OFFSET` ([setup steps](/configuration/#homematicip--openccu-enabling-offset-based-calibration)) and keep the **HomematicIP** 10-minute radio write-spacing checkbox enabled!

### 5. Budget Tuya / Saswell (`SEA801` / `SEA802` / `TS0601` / Avatto `ME167`)
- **Why people pick them:** Inexpensive and quiet, with built-in rounding and quirk fixes in Better Thermostat.
- **Crucial setup tip:** Many budget OEM valves restrict local offsets to a tiny window (e.g. `±2.5 °C` on `SEA801`/`SEA802`). If the gap between the hot pipe and your room exceeds `2.5 °C`, switch `Calibration type` to **`Target Temperature Based`** + **`(AI) Time Based (Default)`** or **`TPI Controller`**.

---

## External room temperature sensors *(Mandatory!)*

Better Thermostat **requires** a separate room temperature sensor—setup cannot complete without one.

### What to look for
- **`0.1 °C`–`0.2 °C` reporting resolution** with prompt updates (every 1–5 minutes on change). Avoid cheap sensors that wait for a `0.5 °C` or `1.0 °C` jump before transmitting, especially if you want to run `PID Controller`.
- **Smart placement:** `1.2–1.5 m` high on an interior wall or shelf—never above a radiator, in direct sun, on a cold outer window frame, or on top of a warm TV/router.

### Proven sensors
- **Aqara Temperature & Humidity Sensor (`WSDCGQ11LM`)**: Compact, long battery life, reliable Zigbee reporting.
- **Sonoff `SNZB-02D` / `SNZB-02P` or Xiaomi Mijia (BLE / Zigbee firmware)**: Clear e-ink/LCD room display plus crisp `0.1 °C` updates.
- **Shelly H&T Gen3 / Shelly BLU H&T**: Great Wi-Fi or BLE options if you don't run Zigbee.

---

## Window & door contact sensors *(Optional, big energy saver)*

- **Aqara Door & Window Sensor (`MCCGQ11LM`)**: Tiny, instant Zigbee open/close events.
- **Sonoff `SNZB-04` / `SNZB-04P`**: Inexpensive standard Zigbee 3.0 contact sensor.
- **Shelly BLU Door/Window**: Fast Bluetooth contact sensor with long coin-cell battery life.

---

## Zigbee2MQTT vs. ZHA — Which Zigbee integration should I use?

Both work with Better Thermostat, **but Zigbee2MQTT (Z2M) is strongly recommended for Zigbee TRVs**:
- **Zigbee2MQTT** exposes `number.<trv>_local_temperature_calibration` (`Offset Based`), `number.<trv>_valve_opening_degree` (`Direct Valve Based` on TRVZB/Spirit), child locks, and external sensor registers across dozens of TRV models. *(Tip: enable **Availability** in Z2M's settings so offline room sensors trigger Better Thermostat's [Degraded mode](/faq/degraded-mode/) fallback!)*
- **ZHA** often exposes only the base `climate` entity without offset or valve-position numbers. Better Thermostat still controls ZHA TRVs accurately by preselecting **`Target Temperature Based`**.

Need a reliable Zigbee USB coordinator?
- **[Home Assistant Connect ZBT-2](https://www.home-assistant.io/connect/zbt-2/)**: Official first-party coordinator maintained by the Home Assistant team.
- **[SONOFF Zigbee 3.0 USB Dongle Plus](https://amzn.to/4rt9aWt)**: Proven, widely used community coordinator for Zigbee2MQTT.

---

## Complete tested starter setups

### Tested Setup 1: Budget-friendly & silent (`SEA801` / `SEA802`)
- **TRV:** Saswell `SEA801-Zigbee` / `SEA802-Zigbee` ([Amazon link](https://amzn.to/4aDFMW3))
- **Room Sensor:** Aqara Temperature & Humidity Sensor `WSDCGQ11LM` ([Amazon link](https://amzn.to/40jCClw))
- **Window Sensor:** Aqara Door & Window Sensor `MCCGQ11LM` ([Amazon link](https://amzn.to/4cB4zwz))
- **Recommended Better Thermostat settings:**
  - **`Calibration type`:** `Offset Based` *(switch to **`Target Temperature Based`** if the radiator sits behind a sofa/curtain or has > `2.5 °C` heat-pocket delta!)*
  - **`Calibration mode`:** **`(AI) Time Based (Default)`** or **`TPI Controller`**
  - **`Tolerance`:** **`0.3 °C`**

### Tested Setup 2: Direct-valve control (`Eurotronic Spirit Zigbee`)
- **TRV:** Eurotronic Spirit Zigbee `SPZB0001` ([Amazon link](https://amzn.to/40mKByg)) *(or Sonoff `TRVZB`)*
- **Room Sensor:** Aqara Temperature & Humidity Sensor `WSDCGQ11LM` ([Amazon link](https://amzn.to/40jCClw))
- **Window Sensor:** Aqara Door & Window Sensor `MCCGQ11LM` ([Amazon link](https://amzn.to/4cB4zwz))
- **Recommended Better Thermostat settings:**
  - **`Calibration type`:** **`Direct Valve Based`**
  - **`Calibration mode`:** **`(AI) MPC v2 (QP + Kalman, experimental)`** or **`TPI Controller`** *(or `(AI) Time Based (Default)`)*
  - **`Tolerance`:** **`0.2–0.3 °C`**
  - *(Note: When using `Direct Valve Based`, always change target temperatures on the Better Thermostat entity in Home Assistant rather than on the physical TRV buttons.)*
