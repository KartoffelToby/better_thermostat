---
title: Troubleshooting & common questions
description: Symptom-by-symptom troubleshooting guide and answers to the most frequent Better Thermostat questions.
---

## Quick diagnostic tip: Check `heating_blocked_by` first!

Whenever a room does not heat the way you expect, open **Developer Tools → States** in Home Assistant, type `climate.<your_better_thermostat>`, and check two attributes ([Entities & attributes](/setup/entities/)):

- **`heating_blocked_by`**: Immediately tells you *why* Better Thermostat is holding the radiator off right now:
  - `null` / empty → Heating is allowed (if the radiator is still cold, check the table below!).
  - `no_call_for_heat` → **[Summer mode](/deep-explanations/summer-mode/)** turned heating off because the 24h-smoothed outdoor temperature is above your threshold (or within its `1 °C` switch-back hysteresis).
  - `window` or `door` → A configured **[Window](/faq/window-sensor/)** or **[Door](/faq/door-sensor/)** sensor currently reports open (or is still counting down its close delay).
  - `hvac_off` → The Better Thermostat entity itself is set to `off`.
  - `devices_released` → The **`Release devices`** switch (or every TRV's **`Release`** switch) is turned `on` ([Releasing devices](/setup/releasing-devices/)).
  - `valve_maintenance` or `startup` → A temporary valve-pin exercise run or initial startup sync is finishing.
- **`control_mode`**: Shows `optimal` during normal control, `sensor_fallback` if your room sensor failed (falling back to TRV sensors), or `hold` if all temperature readings are unavailable ([Degraded mode](/faq/degraded-mode/)).

---

## Symptom → Cause → Solution matrix

| What you see | Most likely cause | How to fix it |
|---|---|---|
| **Better Thermostat shows `unavailable` right after setup** | 1. Newly paired Zigbee2MQTT TRV still reports `local_temperature_calibration: null` / `unknown`.<br />2. Underlying physical `climate` TRV is offline. | 1. Open the TRV in Zigbee2MQTT / HA Device page and set its calibration slider to **`0`** once so it publishes a number.<br />2. Check [Missing entity](/faq/missing-entity/). |
| **Room is `19 °C` (target `21 °C`), `heating_blocked_by` is empty, yet radiator stays cold or lukewarm** | 1. **`Offset Based` hit the TRV's firmware offset cap** (e.g. `±2.5 °C` or `±5 °C`) behind a curtain, sofa, or cover.<br />2. TRV has an internal schedule / eco limit (`max_heat_setpoint_limit` in Z2M) rejecting setpoints.<br />3. Central boiler or wall thermostat is off. | 1. Open **Configure → Screen 2** and switch `Calibration type` from `Offset Based` to **`Target Temperature Based`** (or `Direct Valve Based`).<br />2. Check [Target temperature not taken](/faq/setpoint-not-applied/) and disable internal TRV schedules.<br />3. Connect your boiler via [`hvac_action`](/deep-explanations/central-heating-thermostat/). |
| **Room overshoots target by `1–2 °C`** | 1. `Aggressive` mode or oversized radiator thermal momentum.<br />2. Room sensor placed too far in a cold corner or draft.<br />3. Central boiler cut water flow earlier, causing valve windup. | 1. Switch `Calibration mode` to **`TPI Controller`** (or **`(AI) MPC v2`** on Direct Valve TRVs) and set `Tolerance` to **`0.2–0.3 °C`**.<br />2. Cap oversized Direct-Valve radiators with **`Valve Max Opening`** (e.g. `65 %`).<br />3. Use the **`Pause Learning`** switch when boiler flow stops ([Pausing learning](/setup/pause-learning/)). |
| **Physical TRV display shows `27 °C` or `30 °C` even though Better Thermostat is set to `21 °C`** | **Normal behavior under `Target Temperature Based`!** Because the pipe reads `24 °C`, BT raises the TRV's setpoint temporarily to pull the valve open. | Nothing is broken! As your room approaches `21.0 °C`, Better Thermostat lowers the TRV setpoint smoothly so the room stops right on target. (Prefer seeing `21 °C` on the TRV screen? Use `Offset Based` if your TRV has a wide enough offset range.) |
| **Valve motor adjusts constantly / batteries drain fast** | 1. `Tolerance` was left at the initial `0.0 °C`.<br />2. Using `MPC Predictive (Beta)` or `PID Controller` with a noisy sensor. | 1. Raise **`Tolerance`** to **`0.3 °C`** (`0.5 °C` for underfloor).<br />2. Switch `Calibration mode` to **`TPI Controller`** or **`(AI) Time Based (Default)`**. |
| **Thermostats stay off in spring/autumn even when the room feels chilly** | **Summer Mode** (`call_for_heat: false`): Either your outdoor sensor sits in afternoon sun, or the 24h day/night average hasn't dropped `1.0 °C` below your threshold yet. | Move the outdoor sensor to permanent north-wall shade, or raise **`Outdoor Temperature Threshold`** on the device page ([Summer mode explained](/deep-explanations/summer-mode/)). |
| **Dashboard shows `Configuration error` or `Custom element doesn't exist` on Better Thermostat UI Card** | Companion **Better Thermostat UI Card v3.0+** split the old card tag into two separate cards. | Update your dashboard card YAML `type:` from `custom:better-thermostat-ui-card` to **`custom:better-thermostat-normal-climate-card`** or **`custom:better-thermostat-mini-climate-card`**. |
| **Need to open all radiator valves 100 % to bleed air or fill the heating circuit** | Turning the physical TRV knob up directly gets reverted by Better Thermostat's 5-minute reconciler. | Turn **On** `switch.<room>_release_devices` first, open the physical TRVs manually to bleed the radiators, then turn **`Release devices` Off** when finished ([Releasing devices](/setup/releasing-devices/)). |

---

## Frequently asked questions

### What devices are supported?
Every thermostat exposed as a Home Assistant `climate` entity (with a writable target temperature) works via **`Target Temperature Based`** control. **`Offset Based`** and **`Direct Valve Based`** (`0–100 %`) unlock automatically whenever your integration (such as Zigbee2MQTT, Shelly, HomematicIP/OpenCCU, Z-Wave JS, deCONZ, or tado°) exposes a compatible offset or valve-position entity. See **[Supported devices & compatibility](/working-devices/compatibility/)** and **[Recommended TRVs](/working-devices/recommended-devices/)**.

### `Offset Based` vs. `Target Temperature Based` vs. `Direct Valve Based` — which is best?
1. **`Direct Valve Based`** *(best when offered)*: Drives the `0–100 %` valve motor directly without internal TRV firmware interference.
2. **`Offset Based`**: Shifts the TRV's thermometer offset so the radiator display still shows `21.0 °C`—great for exposed radiators with wide offset ranges.
3. **`Target Temperature Based`**: Shifts the TRV's setpoint dynamically—the most reliable choice whenever a radiator sits behind curtains/furniture, has a small `±2.5–5 °C` firmware offset limit, or runs on ZHA.
See side-by-side numerical examples in **[Calibration types & algorithms](/calibration_algorithms/)**.

### Which algorithm should I start with?
Start with **`(AI) Time Based (Default)`** or **`TPI Controller`**.
- `(AI) Time Based` learns your room's warming speed automatically over 2–3 heating cycles.
- `TPI Controller` needs zero learning wait, moves the valve motor far less than `PID` or `MPC Predictive`, and eliminates overshoot in bedrooms, well-insulated rooms, and multi-radiator rooms.
- On **`Direct Valve Based`** TRVs, **`(AI) MPC v2 (QP + Kalman, experimental)`** adds physics-based predictive braking.

### Why does my TRV pick up a change with a 30-second (or 10-minute) delay?
Better Thermostat spaces routine radio writes to each TRV at least **30 seconds apart** (tracked independently for setpoint, offset, and valve commands). Battery-powered Zigbee/Z-Wave/BLE valves sit on shared low-bandwidth radios; sending write bursts drains batteries rapidly and drops packets.

- Any change made inside that 30-second window isn't lost—it is queued and sent automatically the moment the 30-second timer expires.
- With the **HomematicIP** option enabled, background writes are spaced **10 minutes apart** to protect the strict 868 MHz 1 % airtime budget, while your *first* manual target/mode change still goes out at the 30-second pace.
- **Safety writes** (turning `off`, closing a valve to `0 %`, or frost-floor protection) **bypass the timer and go out immediately**.
- If a radio packet is dropped anyway, Better Thermostat's 5-minute reconciler spots the mismatch and re-sends it automatically ([Data updates & radio limits](/deep-explanations/data-updates/)).

### Can Better Thermostat use my boiler or heat-pump flow temperature?
No—and intentionally so. Better Thermostat neither asks for a fixed flow temperature nor reads a supply-pipe sensor, because in thermal benchmarks across 40 boiler and heat-pump scenarios, feeding flow temperature without knowing exact radiator wattage degraded heat-pump accuracy and quadrupled valve wear. Read the full benchmark breakdown in **[Why MPC v2 does not use the flow temperature](/calibration_algorithms/#why-mpc-v2-does-not-use-the-flow-temperature)**.

### How do I collect logs or diagnostics for a bug report?
Better Thermostat includes an internal **50-cycle deterministic flight recorder** inside Home Assistant's one-click Diagnostics file—so developers can replay your exact room decisions offline! Follow **[Debugging & diagnostics](/faq/debugging/)**.
