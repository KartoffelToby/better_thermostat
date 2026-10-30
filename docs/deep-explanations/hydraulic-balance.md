---
title: Virtual hydraulic balancing
description: How Better Thermostat's 0–100 % valve-opening algorithms and Valve Max Opening cap distribute heat evenly across rooms—and how they differ from mechanical radiator balancing.
---

In heating engineering, **mechanical hydraulic balancing** means a plumber sets a presetting dial inside each radiator's valve body so rooms closest to the boiler pump cannot steal all the hot water from distant bedrooms.

Better Thermostat adds a complementary **software-driven virtual balancing layer** in all **5 valve-computing algorithms**:

- **`(AI) Time Based (Default)`**
- **`TPI Controller`**
- **`(AI) MPC v2 (QP + Kalman, experimental)`**
- **`PID Controller`**
- **`MPC Predictive (Beta)`**

*(The remaining 3 modes—`Aggressive`, `External Sensor Offset Only`, and `No Calibration`—do not compute a `0–100 %` valve opening.)*

---

## How `0–100 %` demand reaches your radiator

Every control cycle, the active algorithm computes a desired valve opening between **`0 %` (closed)** and **`100 %` (full flow)** for each room based on how far the room is from target and how fast it warms or loses heat.

Better Thermostat then applies that percentage through your TRV's configured **[`Calibration type`](/calibration_algorithms/)**:

1. **`Direct Valve Based` *(True motor positioning)*:**
   - Writes the exact percentage directly to the TRV's valve motor (for example `45 %` opening; on Sonoff `TRVZB` where a closing-degree entity exists, it sets closing degree to `100 − 45 = 55 %` as well).
   - **Capping an oversized radiator (`Valve Max Opening`):** Every direct-valve TRV gets its own `number.<trv>_valve_max_opening` slider (default `100 %`). Lowering a fast guest-bathroom radiator to **`60 %`** restricts its peak water flow so slower rooms down the hall receive hot water faster and the small room stops overshooting!
2. **`Offset Based` & `Target Temperature Based` *(Mapped proportional setpoint)*:**
   - Even when a TRV has no direct valve entity, Better Thermostat maps `0–100 %` smoothly between the **TRV's current internal reading (`0 %` = close valve)** and the **TRV's maximum setpoint (`100 %` = open wide)**.
   - Example: If the TRV currently reads `23.0 °C` and its maximum setpoint is `30.0 °C`, a **`50 %` demand** places the effective TRV target midway (~`26.5 °C`) rather than slamming it to `30.0 °C`, nudging the TRV's internal stepper motor to a partial opening.

---

## What virtual balancing solves—and where mechanical balancing still matters

| What virtual balancing helps with | Where mechanical lockshield/presetting still matters |
|---|---|
| Throttles fast rooms down early as they approach `21.0 °C`, freeing pump pressure for slower rooms still at `19.5 °C`. | Cannot fix shrill water-flow whistling caused by excessive pump differential pressure across a tiny valve gap. |
| Lets you fine-tune an overpowered radiator from the sofa via `Valve Max Opening` (`Direct Valve Based`). | On cold mornings when *every* room demands `100 %` simultaneously, mechanical valve presetting still governs initial water split. |
| Reduces room overshoot and boiler return-water temperatures (helping condensing boilers & heat pumps run efficiently). | Relies on well-placed room sensors and responsive TRV integrations ([Recommended settings](/optimal-settings/recommended-settings/)). |
