---
title: Recommended settings
description: Practical Day-1 profiles for standard radiators, direct-valve TRVs, multi-radiator rooms, and underfloor heating—plus why three setup defaults should be adjusted.
---

If you want reliable comfort without spending hours tweaking parameters, start with the profile below that matches your room, or generate a custom setup in the **[Interactive Configurator](/setup/configurator/)**.

## Three setup defaults you should change on Day 1

Home Assistant's config form starts three sliders at conservative neutral values. Adjusting them during setup immediately improves comfort and battery life:

| Setting | Form initial value | Recommended value | What happens if you leave the initial value vs. change it |
|---|---|---|---|
| **`Tolerance`** | `0.0 °C` | **`0.3 °C`** *(radiators)*<br />**`0.5 °C`** *(underfloor)* | At **`0.0 °C`**, every tiny `0.11 °C` room dip triggers a new heating cycle, wearing out valve gears and batteries. At **`0.3 °C`** (e.g. target `21.0 °C`), Better Thermostat heats to `21.0 °C` and rests quietly until the room cools to `20.7 °C`. |
| **`Outdoor temperature when the thermostat should turn off`** *(Summer Mode)* | `20 °C` | **`15–16 °C`** *(modern home)*<br />**`17–18 °C`** *(older home)* | Outdoor sensor readings pass through **24-hour exponential damping** ([Summer mode](/deep-explanations/summer-mode/)), weighting recent history across day and night rather than reacting to brief afternoon peaks *(unlike a weather entity's daily forecast mean, a `24 °C` peak and `8 °C` low do not necessarily produce a `16 °C` damped reading, though cool nights still pull the damped value well below daytime highs)*. Leaving the threshold at `20 °C` keeps radiators active well into warm spring weather! |
| **Window & Door open / close delays** | `0 s` | **Window:** `120 s` open / `180 s` close<br />**Door:** `240–300 s` open / `60 s` close | At **`0 s`**, stepping onto the balcony for 15 seconds cycles your TRVs off and on. A **2–3 min close delay** also lets cold window air mix with warm room air after closing before Better Thermostat demands full heat. |

---

## Proven room profiles

### Profile A — Standard radiator room *(Works for 90 % of homes)*

- **`Calibration type`:** `Offset Based` *(or `Target Temperature Based` if behind curtains/furniture or on ZHA/tado°)*
- **`Calibration mode`:** **`(AI) Time Based (Default)`** *(or **`TPI Controller`** if you prefer zero learning wait)*
- **`Tolerance`:** **`0.3 °C`**
- **`Overheating protection?`:** **On**
- **Window delays (if sensor attached):** **`120 s` open / `180 s` close**

### Profile B — Direct-valve TRV *(Sonoff TRVZB, Shelly BLU TRV, Eurotronic Spirit)*

- **`Calibration type`:** **`Direct Valve Based`**
- **`Calibration mode`:** **`(AI) MPC v2 (QP + Kalman, experimental)`** *(or **`TPI Controller`**)*
- **`MPC v2 room size`:** **`Auto`** *(or pick `Small` / `Medium` / `Large` to match your room on Hour 1)*
- **`Tolerance`:** **`0.2–0.3 °C`**
- **Tip:** If the radiator is oversized for a small room (like a guest WC or small study), set **`number.<trv>_valve_max_opening`** to **`60–75 %`** on the device page so it warms gently instead of blasting full flow.

### Profile C — Large room with 2 or more radiators

- **Setup:** Select **all** TRVs of that room inside **one** Better Thermostat entry (never create two Better Thermostats for one open room).
- **`Calibration type`:** `Direct Valve Based` (if supported) or `Target Temperature Based` / `Offset Based`
- **`Calibration mode`:** **`TPI Controller`** *(steadiest multi-radiator behavior; avoids two valves chasing each other)*
- **`Tolerance`:** **`0.3 °C`**
- **Tip:** If one radiator sits right next to the dining table and gets uncomfortably hot faster than the second radiator across the room, cap that radiator's **`Valve Max Opening`** slider (e.g. `70 %`) ([Virtual hydraulic balancing](/deep-explanations/hydraulic-balance/)).

### Profile D — Underfloor heating or heavy cast-iron radiators

- **`Calibration mode`:** **`(AI) Time Based (Default)`** or **`TPI Controller`**
- **`Tolerance`:** **`0.5 °C`** *(prevents short-cycling a high-mass slab that takes 1–2 hours to respond)*
- **Schedules:** Keep night setbacks gentle (**`1.0–1.5 °C` maximum** difference between `Comfort` and `Sleep`/`Eco`), because reheating a cooled concrete slab by `4 °C` burns extra energy and causes morning overshoot.

---

## Golden habits for clean control

1. **Sensor placement beats every algorithm tweak:** Mount your room thermometer `1.2–1.5 m` high on an interior wall or shelf—away from direct sun, drafts, TVs, and radiators ([5-minute quickstart](/setup/getting-started/)).
2. **Give changes 24–48 hours:** Room thermodynamics move slowly, and `(AI) Time Based`, `PID Auto Tune`, and `MPC v2` refine behavior across full heating and cooling cycles. Although learned parameters remain stored when you switch modes, changing algorithms interrupts the active control loop while the newly selected controller still needs a 24–48 hour settling period.
3. **Protect learned values from unusual heat events:** If you light a wood stove/fireplace or your heat pump pauses radiator heating for hot tap water, flip on the **`Pause Learning`** switch automatically via our ready-made [Automation Blueprints](/setup/automation-blueprints/).
