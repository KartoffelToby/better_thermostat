---
title: Calibration types & algorithms
description: Understand the difference between Calibration type (how Better Thermostat talks to your TRV) and Calibration mode (the 8 control algorithms), with concrete numerical examples.
slug: calibration_algorithms
---

When configuring a radiator valve in Better Thermostat (Screen 2), you make **two separate choices** whose names sound similar:

1. **`Calibration mode` (The Control Brain):** *How much heat does the room need right now?* (8 algorithms to choose from)
2. **`Calibration type` (The Hardware Channel):** *How should Better Thermostat transmit that heat demand to your specific TRV?* (3 hardware methods)

```mermaid
flowchart LR
    S["Room: 19.0 °C | Target: 21.0 °C"] --> M["1. Calibration mode (Brain): Calculates heat / valve %"]
    M --> T["2. Calibration type (Channel): Commands your TRV"]
    T --> V1["Direct Valve Based: Sets valve motor to e.g. 65 %"]
    T --> V2["Offset Based: Shifts TRV offset to e.g. -4.0 °C"]
    T --> V3["Target Temperature Based: Raises TRV setpoint to e.g. 25.0 °C"]
```

Start here if you are unsure:

| Your situation | Algorithm |
| --- | --- |
| Setting up for the first time | AI Time Based (default) |
| Room heats too slowly | Aggressive |
| Temperature often overshoots | TPI Controller |
| You want fine control and know PID tuning | PID Controller |
| You want something simple | External Sensor Offset Only or TPI Controller |
| You want to try an experimental predictive controller | MPC v2 |
| You want the TRV to regulate on its own | No Calibration |

## The algorithms

### External Sensor Offset Only

This mode uses your external temperature sensor to correct the TRV's internal one. Better Thermostat compares the two readings and sends the TRV an offset, or a setpoint that already carries the difference; the TRV keeps running on its own sensor and never sees yours.

It is simple, reliable, works with most TRVs and costs almost nothing to run. In exchange it does not optimise for efficiency and does not adapt to the room.

Use it as a starting point when you want reliable control and no complexity.

---

## Part 1: `Calibration type` — How Better Thermostat talks to your TRV

Better Thermostat always offers **`Target Temperature Based`**, and automatically adds **`Direct Valve Based`** and **`Offset Based`** whenever your TRV hardware and integration expose those writable channels.

To see what each option actually sends over the radio, imagine this everyday snapshot:

- **Your desired target:** `21.0 °C`
- **External room sensor (on the table):** `19.0 °C` *(the room is `2.0 °C` too cold)*
- **TRV's internal sensor (next to the hot pipe):** `23.0 °C` *(reads `4.0 °C` too warm, so the TRV would close its valve on its own!)*

### 1. `Direct Valve Based` *(Highest precision — use when offered)*

- **What it sends:** Bypasses the TRV's internal thermostat loop and sets the valve motor position directly between **`0 %` and `100 %`** (capped by your TRV's `Valve Max Opening` slider), for example **`65 %`**.
- **What it brings:** Zero guesswork about how the TRV's own firmware reacts to offsets or setpoints. Enables fine proportional modulation without slamming the valve fully open and shut.
- **Supported hardware:** Offered automatically when your integration exposes a writable valve-opening entity or quirk—such as **Sonoff `TRVZB`** (Zigbee2MQTT), **Shelly BLU TRV** (Shelly integration via BLU Gateway Gen3), **Eurotronic Spirit Zigbee (`SPZB0001`)** & **Z-Wave (`ZWA021`)**, and several Danfoss/Popp/Hive TRVs in Zigbee2MQTT.
- **Compatible algorithms:** Works with the **5 valve-computing modes**: `(AI) Time Based (Default)`, `TPI Controller`, `(AI) MPC v2 (QP + Kalman, experimental)`, `PID Controller`, and `MPC Predictive (Beta)`.
  - If you pair `Direct Valve Based` with `Aggressive`, `External Sensor Offset Only`, or `No Calibration`, standard TRVs automatically fall back to setpoint control (`Target Temperature Based`). On valve-only TRVs like the **Shelly BLU TRV**, setup rejects incompatible modes—and if a stored configuration carries one, Better Thermostat returns a **`0 %` valve command** (raising a Home Assistant Repair warning and blocking heating, including `Boost`) until you select a compatible controller.
  - Activating the **`Boost`** preset opens direct-valve TRVs straight to `100 %` (or your `Valve Max Opening` limit).

### 2. `Offset Based` *(Local temperature calibration)*

- **What it sends:** Writes a calibration offset (for example **`-4.0 °C`**, plus any boost/throttling from the active algorithm) to the TRV's `number.<trv>_local_temperature_calibration` entity while keeping the TRV's target display at **`21.0 °C`**. The TRV now believes its `23.0 °C` pipe reading is `19.0 °C` and opens its valve.
- **What it brings:** The physical display on the radiator continues to show your real `21.0 °C` room target instead of shifted numbers.
- **Automatic external sensor mirroring:** On TRVs with a dedicated external-sensor register—such as **Sonoff `TRVZB`** and **Sonoff `TRV-ZBT` (Gen2)**—Better Thermostat also mirrors your room sensor's live temperature straight into the TRV automatically.

:::caution[Watch out for narrow hardware offset limits!]
Many TRVs clamp their local offset in firmware (for example `±2.5 °C` on Saswell `SEA801`/`SEA802`, `±5.0 °C` on Eurotronic Spirit, `-6.0 °C` on several Tuya models, or `-12.7 °C` on Sonoff TRVZB).

**What goes wrong:** If your radiator sits behind a curtain, sofa, or cover where trapped air reaches `27 °C` while the room is `19 °C` (`8 °C` difference), a TRV capped at `-5.0 °C` can only correct `27 °C` down to `22 °C`. It still thinks the room is warmer than `21 °C` and **stops heating while your room stays cold!**

**The fix:** Switch `Calibration type` from `Offset Based` to **`Target Temperature Based`** (or **`Direct Valve Based`** if available).
:::

### 3. `Target Temperature Based` *(Always available for setpoint-capable TRVs)*

- **What it sends:** Leaves the TRV's internal offset untouched and instead shifts the **target temperature** sent to the TRV—for example, sending **`25.0 °C`** (or higher depending on the algorithm's demand) so the TRV opens its valve despite reading `23.0 °C` at the pipe. As the room approaches `21.0 °C`, Better Thermostat lowers the TRV setpoint smoothly until the valve closes right on target.
- **What it brings:** Works reliably with Home Assistant `climate` entities that expose either a single target temperature or a target temperature range (including ZHA, tado°, HomematicIP without offset entities, and radiators hidden behind curtains or furniture because it is not restricted by narrow `±2.5–5 °C` offset caps).
- **What you'll notice on the TRV screen:** Seeing **`26 °C` or `30 °C`** on the radiator's physical display while heating is **completely normal and expected**. Better Thermostat is temporarily pulling the valve open and will throttle it back down before your room exceeds `21.0 °C`.
MPC is still in testing. In the project's benchmark it does not overshoot less than the other controller modes, it works the valve harder than PID or TPI, and with several radiators in one room it can over-react. For a room that overshoots, start with TPI instead.

:::tip[How 0–100 % algorithms work on Offset Based and Target Temperature Based TRVs]
Even without `Direct Valve Based`, algorithms like `(AI) Time Based`, `TPI`, and `PID` still compute a `0–100 %` valve demand. Better Thermostat translates `0 %` into a setpoint/offset at the TRV's current temperature (closing the valve) and `100 %` toward the TRV's maximum setpoint (opening the valve), giving you smooth proportional modulation on ordinary TRVs ([Virtual hydraulic balancing](/deep-explanations/hydraulic-balance/)).
:::

---

## Part 2: Quick comparison of all 8 `Calibration mode` algorithms

Qualitative ratings below reflect the single-TRV `balanced` profile of Better Thermostat's thermal benchmark (`uv run python -m tests.benchmark.runner`); actual behavior depends on your room insulation, radiator sizing, hardware channel (`Direct Valve Based` vs. offset/setpoint), and room-sensor resolution (`0.1 °C` vs. `0.5 °C`).

| Exact UI name (`Calibration mode`) | Needs learning? | Overshoot protection | Warm-up speed | Valve & battery wear | Works with `Direct Valve`? | Uses `Overheating protection?` toggle? | Best for |
|---|---|---|---|---|---|---|---|
| **`(AI) Time Based (Default)`** | Yes (~2–3 cycles) | Good | Medium–Fast | Low | **Yes** | **Yes** (non-valve) | Everyday rooms; set-and-forget default |
| **`TPI Controller`** | **No** (instant) | **Very high** *(in steady rooms)* | Steady / Gentle | **Very low** *(steady proportional)* | **Yes** | No (built-in) | Bedrooms, well-insulated rooms, multi-TRV rooms, overshoot issues |
| **`(AI) MPC v2 (QP + Kalman, experimental)`** | Optional (`Auto` adapts) | **Very high** *(on direct valve)* | Fast & smooth *(direct valve)* | Low–Medium *(direct valve)* | **Yes (designed for it)** | No (built-in) | **Direct-valve TRVs** (Sonoff TRVZB, Shelly BLU, Spirit) |
| **`PID Controller`** *(Beta)* | Auto-Tune or manual | High *(once tuned, `≤0.2 °C` sensor)* | Fast *(direct valve)* | Medium–High *(higher on `0.5 °C` sensors)* | **Yes (best)** | No (built-in) | Sunny rooms, kitchens, hallways with sudden heat/cold swings (`0.1 °C` sensor) |
| **`Aggressive`** | No | Low (can overshoot) | **Fast** *(`+2.5 °C` boost, non-valve)* | Low–Medium | No (falls back) | **Yes** | Drafty, poorly insulated, or sluggish rooms where fast heat-up beats precision |
| **`External Sensor Offset Only`** | No | TRV's own loop | TRV's own loop | Very low | No (falls back) | No | Smart TRVs (e.g. tado°) whose internal controller you like—just fixes sensor bias |
| **`MPC Predictive (Beta)`** | Yes (continuous) | Medium | Fast | **High** | Yes | No | Earlier 30-min predictive beta; prefer **TPI** or **MPC v2** for most rooms |
| **`No Calibration`** | No | TRV's own loop | TRV's own loop | Lowest | No (falls back) | No | Using BT only for window/door pause, Summer Mode, presets, or grouping |
MPC v2 is an experimental predictive controller. It estimates the room's state with a Kalman observer and computes the valve opening by solving a small optimisation problem (a quadratic program) over a receding horizon. With the Direct Valve Based calibration type that opening is written to the TRV as it stands (see [Direct valve control](#direct-valve-control)). With any other calibration type, MPC v2 turns the opening into a setpoint between the TRV's current reading and its maximum: the wider the opening, the higher the setpoint. Target Temperature Based sends that setpoint to the TRV as its target temperature; Offset Based leaves the target temperature as it is and moves the calibration offset by the difference instead, rounded to the TRV's offset step and limited to its offset range.

### Which algorithm should I choose in 15 seconds?

- **Just starting out?** Keep **`(AI) Time Based (Default)`** or pick **`TPI Controller`**. Both are rock-solid across standard radiators.
- **Room overshoots target, has multiple radiators, or drains batteries?** Pick **`TPI Controller`**.
- **Using a `Direct Valve Based` TRV (Sonoff TRVZB, Shelly BLU TRV, Spirit) and want cutting-edge predictive control?** Try **`(AI) MPC v2 (QP + Kalman, experimental)`** (or **`PID Controller`** if your room gets sudden sun/cooking heat and has a fast `0.1 °C` sensor).
- **Cold, leaky room takes forever to warm up?** Pick **`Aggressive`** (with `Overheating protection?` turned **On**).

Still unsure? Run the **[Interactive Configurator](/setup/configurator/)** for a tailored room profile.

---

## Part 3: What every algorithm does, brings, and causes
### PID Controller

⚠️ **Beta Status:** The PID Controller is currently in beta and may require further fine-tuning in the algorithm. While it's functional and includes auto-tuning capabilities, you may experience some edge cases that need optimization. Feedback and real-world testing are appreciated.

PID (Proportional-Integral-Derivative) is the classic industrial control method. It sets the valve position from three terms:

- P (Proportional): how far you are from the target temperature
- I (Integral): how long you have been away from it
- D (Derivative): how fast the temperature is moving, read from the room temperature smoothed over time, so two control cycles a few seconds apart do not jolt the valve

It tunes those three itself over time.

PID reacts fast and handles disturbances well, which is what makes it a good fit for a room with sun through the windows, draughts or a heat source whose output varies. Early on it can be aggressive and oscillate a little while it tunes, and getting the most out of it means understanding roughly what the three parameters do.

Pick it when your heating power varies, when outside influences keep moving the room temperature, and when you want a responsive controller and are comfortable with the parameters.

#### Auto-tuning and manual tuning

Auto-tuning is on by default.

**How tuning proceeds:**

- **At the start:** The controller keeps its gains per TRV and per target temperature, rounded to 0.5 °C. It starts from the gains kept for the current target: values you set on the PID numbers, or values it tuned earlier at that target. A target with neither starts from the defaults (Kp=60, Ki=0.01, Kd=2000), so tuning starts over at a target it has not seen. You may notice slight temperature oscillations as it adjusts.

- **While tuning:** The algorithm adjusts the gains at most every 5 minutes, and only when one of these conditions holds (the "target band" is 0.1 °C either side of the target):
  - **Overshoot:** The room was more than 0.2 °C on one side of the target and is now more than 0.2 °C on the other side, for example it heated from below to 0.3 °C above the target. It decreases Kp (less aggressive), increases Kd (more damping) and decreases Ki. A room that comes up to the target and stays below it, or ends up less than 0.2 °C above it, is not an overshoot. Changing the target starts the comparison over, so the room being on the other side of the new target does not count
  - **Sluggish response:** The room is more than 0.1 °C below the target, its temperature changes by less than 0.005 °C per minute (0.3 °C per hour) and the valve output is below 95 %. It increases Ki and Kp
  - **Steady state:** The room is inside the target band and the valve output is below 20 %. It decreases Ki
  - **Holding the target:** The room is inside the target band and Kd is above its default of 2000. Kd falls by 1 % toward 2000, and no further. Damping added by overshoots therefore wears off again while the room holds the target

- **Settled:** Kp stops changing once the room no longer overshoots and no longer heats up too slowly, and Kd returns to 2000 while the room holds the target. While the room holds the target with a small valve output, Ki keeps shrinking every 5 minutes until it reaches its lower limit of 0.001. How many heating cycles that takes depends on the room; the code sets no fixed period.

**What to expect:**

- Adjustments happen at least 5 minutes apart (300 seconds) to avoid over-tuning
- Parameters are constrained to safe ranges:
  - Kp: 10-500
  - Ki: 0.001-2.0
  - Kd: 100-10,000
- Auto-tuning is conservative - it makes small changes and learns gradually
- Gains are learned separately for each target temperature, in steps of 0.5 °C. A target the controller has not run at yet starts from the defaults, or from the values you set by hand
- The integral term belongs to the TRV, not to a target, so changing the target does not restart it: a higher target opens the valve further at once, a lower one closes it further

**Manual tuning:**

Each TRV's *PID Kp (Proportional)*, *PID Ki (Integral)* and *PID Kd (Derivative)* numbers accept Kp from 0 to 1000, Ki from 0 to 100 and Kd from 0 to 10,000, and Better Thermostat keeps a value set there, including 0 (Kd 0 makes a PI controller). With auto-tuning on, a value you set is the starting point it adjusts from. Auto-tuning keeps to the ranges listed above, so it can move a value set outside them into the range in a single step: Ki 0 becomes 0.001 at its first adjustment, and Kp 800 becomes 500 the first time the room heats up too slowly. Kd 0 stays 0, and a Kd above 2000 falls back toward 2000 while the room holds the target. Turn off the *PID Auto Tune* switch to keep fixed values.

The *PID Auto Tune* switch and the values you set apply to the TRV at every target temperature. Turning auto-tuning off keeps the gains the numbers show at that moment, which are the ones in use at the current target, and the controller then uses them at every target; a value you set afterwards replaces them at every target too. With auto-tuning on, a target that has learned its own value uses it, and a target not tuned yet starts from the value you set, or from the values kept when auto-tuning was last turned off.

If you want to tune PID parameters manually or understand what the auto-tuning is doing:

1. **Kp (Proportional gain):** Controls immediate response to temperature error
   - Too high: Oscillations and overshoot
   - Too low: Slow response, takes long to reach target
   - Default: 60

2. **Ki (Integral gain):** Eliminates steady-state error over time
   - Too high: Oscillations, instability
   - Too low: Never quite reaches target (offset)
   - Default: 0.01

3. **Kd (Derivative gain):** Predicts future error based on rate of change
   - Too high: Sensitive to noise, erratic behavior
   - Too low: Overshoot, slow damping
   - Default: 2000

**Monitoring the learned values:**

You can monitor the learned PID values in Home Assistant:

1. Go to Developer Tools → States
2. Find your Better Thermostat entity
3. Look for attributes containing PID debug info showing current Kp, Ki, Kd values

The device also has *PID Kp (Proportional)*, *PID Ki (Integral)* and *PID Kd (Derivative)* number entities that show the current values and let you set them.

### 1. `(AI) Time Based (Default)`

- **In plain English:** Measures how fast *your specific room* actually warms up and cools down, then scales how wide it opens the radiator based on your room's thermal speed. *(Despite `(AI)` in the name, everything runs locally using transparent math—no cloud and no LLM.)*
- **How it works (concrete numbers):**
  Over normal heating and cooling phases, Better Thermostat tracks two diagnostic sensors on your device:
  - **`Heating Power`** (`0.005` to `0.2` °C/min, starting at `0.01`): how quickly the room warms when the radiator is hot.
  - **`Heat Loss`**: how quickly the room cools when heating is idle.

  Look at how differently `(AI) Time Based` opens the valve when your room is **`0.3 °C` below target**:

  | Room behavior | Learned `Heating Power` | Resulting valve demand at `0.3 °C` below target | Why? |
  |---|---|---|---|
  | Fast room / big radiator | `0.02 °C/min` | **~25 %** | Prevents overshooting a quick-heating room |
  | Average room (initial default) | `0.01 °C/min` | **~47 %** | Balanced mid-opening |
  | Slow, heavy, or cold room | `0.005 °C/min` | **~91 %** | Pushes hard so the room doesn't stall |

- **What `Overheating protection?` adds here (non-valve channels only):** On `Offset Based` and `Target Temperature Based` channels, once the room reaches target and goes `idle`, if residual radiator heat pushes the room past `target + tolerance`, Better Thermostat multiplies that excess by **`8×`** in the closing direction (e.g. `0.2 °C` past the band pushes the offset/setpoint by `1.6 °C` toward closed) so stubborn valves seal completely. *(When `(AI) Time Based` runs on `Direct Valve Based`, or when using direct controller modes like `TPI`, `PID`, or `MPC v2`, the `8×` post-adjustment does not apply because the valve closes to `0 %` directly.)*
- **Tips & maintenance:**
  - Turn on the **`Pause Learning`** switch while a wood stove/fireplace burns or while your boiler stops radiator flow for domestic hot water ([Pausing learning](/setup/pause-learning/)).
  - If you relocate your room sensor, bleed a previously air-locked radiator, or change your boiler's flow temperature, reset the learned speed via **Developer Tools → Actions → `better_thermostat.reset_heating_power`**.

---

### 2. `TPI Controller` *(Time-Proportional-Integral)*

- **In plain English:** Calculates a smooth `0–100 %` heating demand purely from two numbers—how far your room is from target right now, plus how cold it is outdoors—with **zero learning phase** and **minimal valve movement**.
- **Important clarification (no noisy on/off clicking!):** Classic electric TPI thermostats click a relay 100 % ON and 0 % OFF every few minutes. **Better Thermostat does NOT pulse your radiator valve open and shut.** Instead, it holds TPI's calculated percentage as a **steady proportional valve opening**, saving motor gears and batteries.
- **How it works (concrete numbers):**
  TPI combines `60 %` per `°C` of indoor temperature gap (`coef_int = 0.6`) with `1 %` per `°C` of indoor-to-outdoor temperature difference (`coef_ext = 0.01`).
  Suppose your **Target is `21.0 °C`** and your **Outdoor temperature is `5.0 °C`** (`16 °C` colder outside → `+16 %` steady heat-loss compensation):

  | Current room temperature | Gap to `21.0 °C` | TPI valve demand | What happens in the room |
  |---|---|---|---|
  | **`19.5 °C`** | `1.5 °C` too cold | **`100 %`** (`90 % + 16 %`, capped) | Full heat-up power |
  | **`20.5 °C`** | `0.5 °C` below target | **`46 %`** (`30 % + 16 %`) | Gently throttles down *before* hitting `21.0 °C` |
  | **`21.0 °C`** | `0.0 °C` (right on target!) | **`16 %`** (`0 % + 16 %`) | Holds a tiny steady trickle matching heat escaping outdoors—no sawtooth cycling! |
  | **`21.3 °C`** | `0.3 °C` above target | **`0 %`** | Shuts completely if sunlight or body heat warms the room |

  *(Without an outdoor sensor or weather entity, TPI still works great using just the `60 %` per `°C` indoor term, closing to `0 %` at `21.0 °C`.)*
- **Best for:** Bedrooms (ultra-quiet), well-insulated modern homes, rooms with 2+ radiators, and any room that overshoots with `(AI) Time Based` or `Aggressive`.

---

### 3. `(AI) MPC v2 (QP + Kalman, experimental)`

- **In plain English:** A physics-based predictive controller built specifically for **`Direct Valve Based`** TRVs. Like an experienced driver coasting before a red traffic light, it tracks how much heat is already stored inside the radiator metal and room air, predicting ahead so it closes the valve *before* thermal momentum overshoots your setpoint.
- **What it brings over simpler controllers:**
  - **Kalman state observer:** Estimates the hidden thermal inertia of your room (`MPC v2 Virtual Temperature`) and automatically adapts to coarse room sensors (such as `0.5 °C` step sensors) without staircase jumps in valve movement.
  - **Smooth quadratic-program (QP) horizon:** Penalizes both temperature error *and* unnecessary valve travel, keeping motors quiet.
  - **Automatic disturbance & parameter adaptation:** Tracks unmodeled heat gains like sunshine or people (`MPC v2 Disturbance`) and refines its room physics (`MPC v2 Coupling`, `MPC v2 Room Time Constant`) cleanly in the background once enough history accumulates.
- **Choosing `MPC v2 room size` (Screen 2):**
  - **`Auto` (Default):** Derives the initial room time constant from learned heat-loss behavior (falling back to a default ~`8 h` residential envelope) and re-identifies your room's exact thermal parameters over several days.
  - **`Small` / `Medium` / `Large`:** Fixes a starting thermal profile (`Small`: fast ~`3 h` envelope; `Medium`: standard ~`8 h` envelope; `Large`: slow ~`12 h` envelope) when you want a specific time-constant prior from Hour 1.
- **Diagnostic entities:** Enable the 4 opt-in sensors (`MPC v2 Virtual Temperature`, `MPC v2 Coupling`, `MPC v2 Disturbance`, `MPC v2 Room Time Constant`) on the device page if you want to inspect the live model ([Entities & attributes](/setup/entities/)).
- **Best for:** Rooms with **`Direct Valve Based`** TRVs (Sonoff TRVZB, Shelly BLU TRV, Eurotronic Spirit) where you want tight comfort with minimal overshoot. *(On `Offset Based` or `Target Temperature Based` TRVs, its fine 1–2 % valve adjustments get rounded by the TRV's `0.5 °C` setpoint grid—use `TPI Controller` or `(AI) Time Based` there instead.)*

#### Why MPC v2 does not use the flow temperature
**Notes:**

- "Writes the valve directly" applies with the *Direct Valve Based* calibration type (see [Direct valve control](#direct-valve-control) below)
- **MPC v2** (experimental) and **No Calibration** are left out of the table; see their sections above
- **PID Controller** is in beta and may require further algorithm fine-tuning

## How algorithms and calibration types combine

The **Calibration Mode** (algorithm) works together with the **Calibration Type**:

- **Target Temperature Based:** The algorithm calculates what target temperature to send to the TRV. For example, if you want 20°C but the room is cold, it might send 22°C to the TRV to make it heat more.

- **Offset Based:** The algorithm calculates what temperature offset to send to the TRV. For example, if the TRV's internal sensor reads 21°C but your external sensor reads 20°C, it sends an offset of -1°C.

- **Direct Valve Based:** The algorithm's valve opening is written to the TRV's valve position entity. Modes that produce no valve opening send a target temperature instead, as with Target Temperature Based.

Not all TRVs support offset-based calibration or direct valve control. Better Thermostat detects your TRV's capabilities and only offers the calibration types it supports.

## Direct valve control

Some TRV devices support **direct valve control**, where Better Thermostat can directly set the valve opening percentage (0-100%) instead of only adjusting target temperatures or offsets. That sends the selected algorithm's valve decision straight to the TRV, instead of letting the TRV's own controller decide what to do with a setpoint.

### What direct valve control is

With direct valve control, Better Thermostat can:

- Set the exact valve opening (e.g., "open valve to 45%")
- Bypass the TRV's internal temperature control logic
- Achieve more precise and responsive heating control
- Better implement advanced algorithms like MPC and PID

### Devices that support it

Direct valve control is available for TRVs that expose valve position as a controllable entity, including:

- **Sonoff TRVZB** (via Zigbee2MQTT or ZHA)
- **TRVs exposed via MQTT** with valve position entities
- **ZHA TRVs** whose quirk exposes the valve position as a number entity. A valve position that ZHA only shows as a sensor, such as the PI heating demand of the Bosch Radiator Thermostat II, reports the opening but cannot set it
- **Z-Wave JS TRVs** that expose a valve position entity, and the Eurotronic Spirit Z / Aeotec ZWA021
- **Shelly BLU TRV**, once its built-in thermostat is switched off on the Shelly side and Home Assistant shows its valve position entity. Reload Better Thermostat after switching, so it finds the entity
- **Other Zigbee TRVs** that expose valve control through their integration

Better Thermostat detects whether your TRV supports direct valve control and then offers the **Direct Valve Based** calibration type. It only writes the valve when you select that type.

The valve entity has to accept writes. Where the device exposes both a writable valve number and a valve sensor that only reports the opening, Better Thermostat writes the number. A valve closing degree, such as the one on the Sonoff TRVZB, is never taken as the valve entity, because it holds the opposite of the opening. If the TRVZB's valve opening degree entity is disabled, the TRV has no direct valve control and Better Thermostat logs a warning naming that entity; enable it and reload Better Thermostat.

### How the algorithms use it

With the Direct Valve Based calibration type:

- **MPC Predictive**: Calculates a valve opening from its prediction of where the room is heading, and that opening is written as it stands.

- **PID Controller**: Directly outputs valve position based on temperature error and trends.

- **TPI Controller**: Sets valve opening based on heating duty cycle calculations.

- **MPC v2**: Computes the valve opening from its optimisation.

- **AI Time Based**: Derives a valve opening from the heating power it has learned.

- **External Sensor Offset Only, Aggressive, No Calibration**: These produce no valve opening. Better Thermostat sends them to the TRV as a target temperature instead.

- **Boost preset**: While Boost is heating, Better Thermostat opens the valve to the TRV's *Valve Max Opening* under every calibration mode. When the TRV is switched off during Boost (Better Thermostat off, a window open, no heat called for), it closes the valve to 0 %, as it does under the modes that write the valve.

The valve opening is sized from your room sensor, not from the TRV's own temperature. A TRV that stops reporting its own temperature still has its valve opened and closed; only the target temperature that accompanies the valve command is held back until the TRV reports a temperature again.

### Without direct valve control

If your TRV doesn't support direct valve control, or you pick another calibration type, Better Thermostat uses **setpoint manipulation**:

- Adjusts the target temperature sent to the TRV
- Or adjusts the temperature offset (if supported)
- The TRV's internal controller then adjusts the valve based on its own logic

This still works well but gives the TRV's internal algorithm more influence over the final valve position.

### Checking whether you have it

1. Open the Better Thermostat entry in **Settings → Devices & services** and choose **Configure**
2. Open the **Calibration type** dropdown in the advanced step
3. If it offers **Direct Valve Based**, your TRV supports direct valve control

For MQTT/Zigbee2MQTT users, you can also check if your TRV exposes entities like:

- `number.your_trv_valve_position`
- `number.your_trv_valve_opening_degree`

### What it buys you

The algorithm sets the valve opening itself rather than asking the TRV's own logic for it, so the response arrives without a detour and the room's reaction is a cleaner signal to learn from. Valve position is not heat output — flow temperature and the valve's own authority still sit in between — but it is the most direct handle Better Thermostat can get on the device. Every mode runs without it.

### If you are buying new TRVs

Users with heat pumps or weather-compensated boilers often ask why MPC v2 has no setting or sensor input for radiator flow (supply) temperature.

Internally, MPC v2 models a reference `65 °C` heat source because **what determines the required valve opening is the ratio between flow temperature and radiator surface area**: a heat pump running `35 °C` water through oversized low-temperature radiators (or underfloor loops) needs almost the same steady valve percentage as a `65 °C` gas boiler feeding smaller classic radiators.

In Better Thermostat's 40-scenario thermal benchmark (`S29`, `S33`, `S39`, `S40`):
- Feeding a lower flow temperature *without* knowing the exact physical radiator wattage caused severe under-driving and nearly **4× more daily valve travel** on heat pumps (`0.108 K` vs `0.044 K` mean error).
- Even with a live flow-temperature sensor and matched radiator sizing, accuracy improved by just `0.016 °C`—far smaller than a room thermometer's own accuracy—while MPC v2's disturbance observer and `Auto` re-identification absorb gradual heating-curve shifts automatically.
- If your boiler temporarily stops heating radiators altogether (for example during a 45-minute domestic hot water cycle), use the **[Pause Learning switch](/setup/pause-learning/)** so no algorithm misinterprets cold pipes as room heat loss.

---

### 4. `PID Controller` *(Beta)*

- **In plain English:** Classic industrial cruise-control combining three reactions:
  - **P (`Proportional`):** Pulls harder the further the room is below target right now.
  - **I (`Integral`):** Slowly adds extra valve opening if a cold winter day leaves the room stubbornly `0.2 °C` below target for an hour.
  - **D (`Derivative`):** Hits the brakes as soon as room temperature starts rising fast (or when afternoon sun / cooking heats the room).
- **What you'll notice:** Tight setpoint holding and quick recovery from sudden disturbances, at the cost of **more frequent valve adjustments** (and slightly faster battery drain) than `TPI` or `(AI) Time Based`.
- **Best setup:** Shines with **`Direct Valve Based`** TRVs and a responsive room sensor reporting **`0.1 °C`** changes every 1–5 minutes. Avoid PID if your room sensor only updates in `0.5 °C` jumps (coarse steps cause derivative spikes).

#### Auto-tuning and manual tuning

When `PID Controller` is selected, Better Thermostat exposes four configuration entities on the device ([Entities & attributes](/setup/entities/)):

- **`PID Auto Tune`** (`switch`, **On** by default): Watches natural temperature crossings around your setpoint and automatically tunes P, I, and D gains (stored per `0.5 °C` target bucket, so `18 °C` night and `21 °C` day targets each keep their own learned gains). Expect **3–5 full heating oscillations** before Auto-Tune settles; during those first cycles, mild overshoot is normal.
- **`PID Kp (Proportional)`**, **`PID Ki (Integral)`**, **`PID Kd (Derivative)`** (`number` sliders): Show the active gains and let you fine-tune manually. **Important:** Changing any of the three gain sliders manually automatically switches **`PID Auto Tune` Off** so your manual tune is never overwritten!

| What you observe with PID | Manual adjustment |
|---|---|
| Room warms up too slowly | Increase **`PID Kp (Proportional)`** by 20–30 % |
| Room settles `0.2–0.4 °C` below target and stays there | Slightly increase **`PID Ki (Integral)`** |
| Room overshoots on initial heat-up | Lower **`PID Ki (Integral)`** and/or raise **`PID Kd (Derivative)`** |
| Valve hunts back and forth continuously | Lower **`PID Kp (Proportional)`** and **`PID Kd (Derivative)`** (or switch to **`TPI Controller`**) |
| Want a clean slate after changing radiators or sensors | Run action **`better_thermostat.reset_pid_learnings`** (enable `apply_pid_defaults` to restore baseline `Kp`/`Ki`/`Kd`) |

---

### 5. `Aggressive`

- **In plain English:** Built for speed over finesse. The moment your room drops below target (skipping the normal tolerance start delay!), `Aggressive` pushes an extra **`+2.5 °C` boost** onto the TRV (`+2.5 °C` target shift or `-2.5 °C` extra negative offset) to rip the valve wide open until the target temperature is reached.
- **What you'll notice:** Radiators get hot very fast, which feels great in cold, drafty, or rarely used rooms—but heavy cast-iron radiators or small well-insulated rooms will **overshoot** past the target once the valve shuts.
- **Important notes:**
  - Always keep **`Overheating protection?` turned On** with `Aggressive` so Better Thermostat forces the valve shut (`8×` closing push) if residual heat carries the room past `target + tolerance`.
  - `Aggressive` does **not** calculate a `0–100 %` valve percentage; do not pair it with `Direct Valve Based`.

---

### 6. `External Sensor Offset Only` *(formerly called "Normal")*

- **In plain English:** Pure 1-to-1 thermometer correction—nothing more. If your room sensor reads `19.0 °C` and the TRV reads `23.0 °C`, Better Thermostat tells the TRV *"the temperature is `4.0 °C` lower than you think"* and leaves 100 % of the heating decisions to the TRV's own built-in firmware.
- **Key difference from every other mode:** `External Sensor Offset Only` applies **no tolerance band**, **no boost**, **no learning**, and **no `Overheating protection?` hook**. Every meaningful room-sensor update refreshes the offset/setpoint mapping directly.
- **Best for:** Premium TRVs (such as tado° or modern Danfoss Ally valves) whose built-in internal PID controller you already love, when all you want is to replace their biased radiator-side thermometer with your room sensor plus Better Thermostat's window pause, Summer Mode, and presets.

---

### 7. `MPC Predictive (Beta)`

- **In plain English:** Better Thermostat's first-generation 30-minute thermal model (`MPC Gain`, `MPC Loss`, `MPC Insulation (Ka)`, `Virtual Temperature`).
- **Should I use it?** In Better Thermostat 2.0's thermal benchmark suite, `MPC Predictive (Beta)` moves the valve motor noticeably more often than `TPI` or `PID`, does not beat them on overshoot, and can over-react when a room has multiple radiators.
  - For **`Direct Valve Based`** TRVs, choose **`(AI) MPC v2 (QP + Kalman, experimental)`** instead.
  - For **`Offset Based`** or **`Target Temperature Based`** TRVs, choose **`TPI Controller`** or **`(AI) Time Based (Default)`** instead.

---

### 8. `No Calibration`

- **In plain English:** Passes your exact target temperature (`21.0 °C` → `21.0 °C`) straight through to the TRV without shifting its offset, setpoint, or valve percentage. *(Unlike `External Sensor Offset Only`, the configured `Tolerance` band still gates when Better Thermostat switches between heating and idle.)*
- **Best for:** Setups where your TRV **already receives an external room sensor natively** (for example via a direct Zigbee binding or manufacturer bridge) and you only want Better Thermostat's **wrapper features**: window & door pause, 24h-damped Summer Mode, multi-TRV room grouping, presets, child lock, and fail-soft monitoring.
