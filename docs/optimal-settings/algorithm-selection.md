---
title: Quick algorithm selector
description: Fast cheat sheet to pick the right Calibration type and Calibration mode by room type or heating symptom.
---

Not sure which options to pick on **Screen 2** of Better Thermostat's setup? Use the two quick tables below, try the **[Interactive Configurator](/setup/configurator/)**, or read the full numerical breakdown in **[Calibration types & algorithms](/calibration_algorithms/)**.

## Step 1: Pick the right `Calibration type` (hardware channel)

| What your TRV & room look like | Pick this `Calibration type` | Why |
|---|---|---|
| TRV exposes a writable valve opening (`0–100 %`) — e.g. **Sonoff TRVZB**, **Shelly BLU TRV**, **Eurotronic Spirit** | **`Direct Valve Based`** | Highest precision; Better Thermostat positions the valve motor directly without fighting internal TRV firmware. |
| Radiator stands in the open **and** TRV exposes a wide local calibration offset (e.g. via Zigbee2MQTT) | **`Offset Based`** | Keeps the TRV's physical screen showing your real room target (e.g. `21.0 °C`) while shifting its thermometer reading. |
| Radiator sits **behind curtains, a sofa, or a cover**, **or** TRV limits offsets to `±2.5–5 °C` (e.g. Saswell `SEA801`/`802`), **or** you use **ZHA / tado°** | **`Target Temperature Based`** | Never hits narrow firmware offset walls. *(Seeing `26–30 °C` temporarily on the TRV screen while heating is completely normal!)* |

---

## Step 2: Pick the right `Calibration mode` (control algorithm)

### By room type

| Room / Heating system | Recommended `Calibration mode` | Recommended `Tolerance` | Notes |
|---|---|---|---|
| **Everyday living room or office** (1 radiator) | **`(AI) Time Based (Default)`** or **`TPI Controller`** | **`0.3 °C`** | Set-and-forget comfort; learns room speed in 2–3 cycles (or works immediately with `TPI`). |
| **Bedroom or nursery** (quiet, zero overshoot) | **`TPI Controller`** | **`0.3 °C`** | Holds a calm proportional opening without motor hunting; zero learning wait. |
| **Large room with 2 or more radiators** | **`TPI Controller`** | **`0.3 °C`** | Keeps grouped TRVs steady without over-reacting against each other. *(Cap any oversized radiator with `Valve Max Opening`.)* |
| **Direct-valve TRV room** (Sonoff TRVZB, Shelly BLU, Spirit) | **`(AI) MPC v2 (QP + Kalman, experimental)`** or **`TPI Controller`** | **`0.2–0.3 °C`** | MPC v2 predicts thermal momentum to land softly on target without overshoot. |
| **Kitchen, sunny south room, or drafty hallway** | **`PID Controller`** *(with `0.1 °C` room sensor)* or **`TPI Controller`** | **`0.2–0.3 °C`** | PID's derivative brake reacts rapidly to cooking heat, afternoon sun, or door drafts. |
| **Cold, poorly insulated room** that warms up too slowly | **`Aggressive`** *(non-direct-valve)* | **`0.3 °C`** | Adds `+2.5 °C` extra push immediately on temperature dips. Keep `Overheating protection?` **On**. |
| **Underfloor heating / heavy cast-iron radiators** | **`(AI) Time Based (Default)`** or **`TPI Controller`** | **`0.5 °C`** | Wider tolerance prevents fighting multi-hour thermal inertia. |
| **Smart TRV whose internal loop you want to keep** (e.g. tado°) | **`External Sensor Offset Only`** | *(not used)* | Pure 1:1 sensor bias correction; TRV's own firmware handles modulation. |

---

### By symptom you want to fix

| Problem you notice | What to change |
|---|---|
| **Room overshoots target by `1–2 °C`** | Switch `Calibration mode` to **`TPI Controller`** (or **`(AI) MPC v2`** on Direct Valve TRVs), keep `Tolerance` at **`0.2–0.3 °C`**, lower **`Valve Max Opening`** (e.g. to `60–75 %`) if the radiator is oversized, and check that your central boiler isn't cutting flow on a separate timer ([Central heating](/deep-explanations/central-heating-thermostat/)). |
| **Radiator stops heating `1–3 °C` below target** | If using `Offset Based`, your TRV likely hit its firmware offset limit (`±2.5 °C` or `±5 °C`)—switch `Calibration type` to **`Target Temperature Based`**. Also check `max_heat_setpoint_limit` in Zigbee2MQTT ([Target temperature not taken](/faq/setpoint-not-applied/)). |
| **Room warms up far too slowly** | If using `(AI) Time Based`, run `better_thermostat.reset_heating_power` in case it learned during a low-flow period, or switch to **`Aggressive`** (with `Overheating protection?` **On**). |
| **Valve motor adjusts too often / batteries drain quickly** | Raise `Tolerance` from `0.0` to **`0.3–0.4 °C`** and switch from `PID` / `MPC Predictive` to **`TPI Controller`** or **`(AI) Time Based (Default)`**. |
