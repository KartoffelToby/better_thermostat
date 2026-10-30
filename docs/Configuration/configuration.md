---
title: All settings reference
description: Complete field-by-field reference for Screen 1 and Screen 2 of Better Thermostat, with concrete examples of what every setting does and causes.
slug: configuration
---

To add a new room, go to **Settings → Devices & Services → Integrations → + Add Integration → Better Thermostat**, or click:

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=better_thermostat)

To change settings on an existing room later, go to **Settings → Devices & Services → Better Thermostat** and click the gear icon (**Configure**) next to that room.

:::tip[Just setting up your first room?]
Start with the quick **[Setup dialog walkthrough](/setup/configuration-walkthrough/)** or **[Recommended settings](/optimal-settings/recommended-settings/)**, and come back to this page whenever you want the exact rules for a specific option.
:::

---

## First step

Screen 1 configures the room, its sensors, its comfort deadband (`Tolerance`), and its presets.

### Core room & climate entities

#### `Name` *(Required)*
- **What it does:** Sets the display name and initial entity ID of your virtual thermostat (e.g. `Living Room` → `climate.living_room`).

#### `The real thermostat` *(Required)*
- **What it does:** One or more physical radiator thermostats (`climate.*`) in this room.
- **Multi-TRV rooms:** Select all TRVs of the room together here so Better Thermostat coordinates them as one group. Another Better Thermostat entity cannot be selected here, and `climate` entities without a writable target temperature are rejected.

#### `Room temperature sensor` *(Required)*
- **What it does:** The standalone thermometer (`sensor.*`) placed in the living area of the room (`1.2–1.5 m` high, away from radiators, sun, and drafts).
- **If it goes offline or reports invalid values:** After 2 minutes without a valid reading between `-50 °C` and `60 °C`, Better Thermostat steps down gracefully to the average of your TRVs' built-in thermometers (`sensor_fallback`). A self-clearing [Degraded mode](/faq/degraded-mode/) repair notice is raised only when the room sensor state is `unavailable` or `unknown` (out-of-range numeric readings trigger `sensor_fallback` without creating that repair notice).

#### `The cooling device (optional)`
- **What it does:** Adds an air conditioner or cooling `climate` entity alongside your radiators.
- **What changes when enabled:**
  - Better Thermostat coordinates heating and cooling from one card so the radiator and AC never run against each other.
  - Enabled presets switch from a single temperature slider to a **`Min` (heating)** and **`Max` (cooling)** pair ([Presets](/setup/presets/)).
  - Includes a built-in **4-minute (240 s) compressor anti-short-cycling guard** between cooling runs.

#### `Humidity sensor` *(Optional)*
- **What it does:** Shows current ambient humidity on the Better Thermostat entity and powers the `humidity_high` and `current_humidity_changed` device triggers (used by the [High Humidity Alert blueprint](/setup/automation-blueprints/)). It does not alter target temperatures.

---

### Window & door pause settings

#### `Window sensor` *(Optional)*
- **What it does:** A `binary_sensor` (or Binary Sensor Group Helper combining all windows in the room) that turns every TRV in the room off while airing, without altering your saved HVAC mode or target temperature.
- **Offline safety:** If a contact sensor battery dies (`unavailable` or `unknown`), Better Thermostat 2.0 treats it as **closed** so the room never freezes ([Window sensor](/faq/window-sensor/)).

#### `Delay before the thermostat should turn off when the window is opened` *(Default: `0 s` — Recommended: `120–180 s`)*
- **What happens when you change it:** At `0 s`, opening a patio window for 10 seconds immediately drives your valve motors closed. Setting `120 s` ignores brief openings and saves TRV battery life.

#### `Delay before the thermostat should turn on when the window is closed` *(Default: `0 s` — Recommended: `120–300 s`)*
- **What happens when you change it:** Right after closing a winter window, a pocket of cold air lingers around your room sensor. Waiting 2–5 minutes lets room air mix back toward its true temperature so Better Thermostat doesn't blast an unnecessary overshoot spike.

#### `Door sensor` *(Optional)*
- **What it does:** Works just like `Window sensor`, with its own independent timers so interior hallway doors can tolerate normal foot traffic while still pausing heating if a door is propped open ([Door sensor](/faq/door-sensor/)). Heating resumes once **every** configured window *and* door reports closed.

#### `Delay before the thermostat should turn off when the door is opened` *(Default: `0 s` — Recommended: `240–300 s`)*
#### `Delay before the thermostat should turn on when the door is closed` *(Default: `0 s` — Recommended: `30–60 s`)*
- **Tip:** Toggling the device's **`Window Detection`** switch Off temporarily bypasses **both** window and door sensors together ([Entities & attributes](/setup/entities/)).

---

### Summer Mode (automatic outdoor shutdown)

For the full explanation of 24-hour exponential smoothing and hysteresis, read **[Summer mode explained](/deep-explanations/summer-mode/)**.

#### `Outdoor temperature sensor` *(Optional)*
- **What it does:** Feeds local outdoor temperature into Summer Mode (and into `TPI` / `MPC` heat-loss compensation).
- **Important:** Better Thermostat smooths outdoor readings over a **24-hour time constant** (seeded from up to 3 days of Home Assistant Recorder history) so brief afternoon sunshine on a sensor wall doesn't prematurely kill heating. Mount the sensor in permanent north-side shade.

#### `Weather entity to get the outdoor temperature` *(Optional)*
- **What it does:** Alternative outdoor source if you don't own a physical outdoor sensor (must provide a forecast). If **both** an outdoor sensor and a weather entity are configured, the **`Outdoor temperature sensor` automatically takes priority**.
- **How it decides:** Heating stays allowed while *either* the weather entity's 24h-damped current temperature *or* its ~2-day forecast mean (averaging daily highs and lows) stays below your threshold.

#### `The outdoor temperature when the thermostat should turn off` *(Default: `20 °C` — Recommended: `15–17 °C`)*
- **What happens when you change it:**
  - Because readings are smoothed across **full 24-hour day-and-night cycles**, a warm `24 °C` spring afternoon followed by an `8 °C` night averages **`16 °C`**. Leaving this at `20 °C` keeps radiators active well into late spring; **`15–16 °C`** (well-insulated homes) or **`17–18 °C`** (older buildings) matches real heating-season cutoffs.
  - **Built-in `1.0 °C` (`1.8 °F`) hysteresis:** Once Summer Mode turns heating off at e.g. `16.0 °C` (`heating_blocked_by: no_call_for_heat`), heating stays off until the 24h-smoothed outdoor temperature drops below **`15.0 °C`**, preventing oscillation.
  - You can also adjust this threshold live without reloading via `number.<room>_outdoor_temperature_threshold`.

---

### Comfort band (`Tolerance`), presets & limits

#### `Tolerance, to prevent the thermostat from turning on and off too often` *(Default: `0.0 °C` — Recommended: `0.3 °C` for radiators, `0.5 °C` for underfloor)*
- **What happens when you change it:**
  - **Heating example (`20.0 °C` target, `0.3 °C` tolerance):** Better Thermostat heats up to `20.0 °C`, switches to `idle`, and lets the room rest quietly until it cools to **`19.7 °C`** before starting the next heating phase. Leaving it at **`0.0 °C`** causes frequent valve movement on every `0.11 °C` dip.
  - **Cooling example (`24.0 °C` cooling target, `0.3 °C` tolerance):** Better Thermostat waits until the room warms to **`24.3 °C`** before starting the AC, then cools back down to `24.0 °C` (the cooling band is always at least `0.2 °C` wide to protect AC compressors).
  - *(Note: `External Sensor Offset Only` mode ignores the tolerance band and passes continuous sensor offset updates directly to the TRV.)*
  - Live-adjustable anytime via `number.<room>_tolerance`.

#### `Enabled Presets` *(Default: `Eco`)*
- **What it does:** Chooses which preset modes (`Eco`, `Away`, `Boost`, `Comfort`, `Home`, `Sleep`, `Activity`) appear on this thermostat. Every enabled preset creates its own configurable temperature slider (`number.<room>_<preset>`) under the device's **Configuration** section ([Presets](/setup/presets/)).

#### `Target minimum temperature`, `Target maximum temperature` & `Target temperature step`
- **What they do:** Bound the range and `+`/`−` step size users and voice assistants can set on Better Thermostat. `Target temperature step` offers **`Auto`**, **`Auto (legacy)`** (which may be the default on new forms), **`0.1 °C`**, **`0.2 °C`**, **`0.25 °C`**, **`0.5 °C`**, and **`1.0 °C`** (labeled `1 °C` in the dropdown). Leave min, max, and step on **`Auto`** to inherit the limits and grid your physical TRVs support.

#### `Availability guard` & `Grace period after the availability guard turns on` *(Default grace: `10 min`)*
- **What they do:** Optional `binary_sensor`, `switch`, or `input_boolean` that is `on` when your thermostats have mains power and `off` when powered down (for example, wired floor/wall thermostats powered directly by a heat pump or boiler circuit). While the guard is `off`—and for `10 minutes` after power returns—offline TRVs suppress log warnings and [Missing entity](/faq/missing-entity/) repair alerts while booting up.

---

## Second step

Screen 2 configures how Better Thermostat drives **each individual physical TRV** in the room.

#### `Calibration type` *(Required)*
- **`Direct Valve Based`:** Writes `0–100 %` directly to the valve motor (offered when your TRV exposes a writable valve opening, e.g. Sonoff TRVZB, Shelly BLU TRV, Eurotronic Spirit). Requires one of the 5 valve-computing modes (`(AI) Time Based`, `TPI`, `MPC v2`, `PID`, or `MPC Predictive`).
- **`Offset Based`:** Adjusts the TRV's local temperature calibration entity (`number.*` or `select.*`, such as HomematicIP steps). Keeps the TRV's physical screen showing your true room target, but can hit firmware limits (`±2.5–5 °C`) behind curtains or covers.
- **`Target Temperature Based`:** Shifts the setpoint sent to the TRV. Works universally on every Home Assistant `climate` TRV and never hits narrow offset caps.

#### `Calibration mode` *(Required)*
Chooses one of the **8 control algorithms**—see **[Calibration types & algorithms](/calibration_algorithms/)** for detailed numerical examples:
- **`(AI) Time Based (Default)`** — Self-learning room heating-speed controller (recommended default).
- **`TPI Controller`** — Zero-learning proportional controller; very gentle on batteries, minimal overshoot, great for bedrooms & multi-TRV rooms.
- **`(AI) MPC v2 (QP + Kalman, experimental)`** — Predictive physics controller tailored for **`Direct Valve Based`** TRVs.
- **`PID Controller`** — Responsive P+I+D loop with Auto-Tune; great for sunny rooms/kitchens with `Direct Valve Based` and `0.1 °C` room sensors.
- **`Aggressive`** — Adds an immediate `+2.5 °C` boost while heating for cold/drafty rooms (non-direct-valve).
- **`External Sensor Offset Only`** — Pure 1:1 sensor correction with no tolerance band or extra controller math.
- **`MPC Predictive (Beta)`** — Earlier 30-min predictive beta (higher valve wear; prefer `TPI` or `MPC v2`).
- **`No Calibration`** — Sends target temperatures unchanged while keeping window pause, Summer Mode, presets, and grouping.

#### `MPC v2 room size` *(Default: `Auto`)*
- **What it does:** Only used when `Calibration mode` is `(AI) MPC v2 (QP + Kalman, experimental)`. `Auto` adapts from learned thermal behavior, while `Small`, `Medium`, and `Large` fix a starting thermal envelope profile with approximate **3-hour**, **8-hour**, and **12-hour** room time constants (`tau_room_min = 180`, `480`, and `720` minutes), respectively.

#### `Overheating protection?` *(Default: `On`)*
- **What happens when enabled:** Only active in **`(AI) Time Based (Default)`** (non-direct-valve) and **`Aggressive`** modes. If residual radiator heat pushes the room above `target + tolerance` while Better Thermostat is already `idle`, it multiplies the excess temperature by **`8×`** in the closing direction to force leaky or sluggish TRV pins completely shut. *(TPI, PID, and MPC handle overshoot natively and ignore this checkbox.)*

#### `Use the minimum temperature instead of 'off'` *(Default: `Off`)*
- **What happens when enabled:**
  1. Whenever Better Thermostat wants to turn this TRV off (HVAC mode `off`, open window/door, or Summer Mode), it sends the TRV's own reported `min_temp` instead of `hvac_mode: off`—essential for TRVs whose firmware ignores or misbehaves on `off`. *(TRVs that don't advertise an `off` mode in Home Assistant already receive `min_temp` automatically.)*
  2. Turning the physical radiator knob all the way down to `min_temp` by hand tells Better Thermostat you turned the radiator off (once every TRV in the room is turned to `min_temp`, Better Thermostat switches to `off`; turning a knob back up switches Better Thermostat back to `heat`—unless Child Lock is active).

#### `If 'auto' means 'heat' for your TRV and you want to swap it` *(Default: `Off`)*
- **What it does:** Some TRV integrations map `heat` to an uncontrolled full-open "boost" mode while `auto` represents normal temperature control. Enable this checkbox to swap the two so Better Thermostat sends `auto` during normal heating.

#### `If your thermostat has no own maintenance mode, you can use this one` *(Default: `Off`)*
- **What it does:** Periodically exercises the valve pin fully open and closed so mineral scale does not jam the pin during long summer shutdowns. The next scheduled run appears on the `climate` entity's `next_valve_maintenance` attribute, and you can also trigger a run manually via `better_thermostat.run_valve_maintenance`.

#### `Ignore all inputs on the TRV like a child lock` *(Default: `Off`)*
- **What it does:** Reverts any manual knob turns (or direct HA service calls) made on the physical TRV so only changes on the Better Thermostat entity take effect. Also exposed live as a per-TRV **`Child Lock`** switch entity (which toggles the TRV's native hardware child-lock entity too when available!).

#### `If you use HomematicIP, you should enable this to slow down the requests to prevent the duty cycle` *(Auto-enabled for Homematic)*
- **What happens when enabled:** Spaces routine background writes to this TRV to **at most once every 10 minutes** (instead of the standard **30 seconds**) so your HomematicIP Access Point / OpenCCU never hits its strict 868 MHz 1 % radio airtime limit. Your first manual target change on Better Thermostat still goes out immediately at the normal 30-second pace, and critical safety/off commands are never dropped.

---

## HomematicIP / OpenCCU: Enabling `Offset Based` calibration

On **Homematic(IP) Local for OpenCCU**, Better Thermostat supports dropdown `select.*_temperature_offset` entities (`1.5K`, `2.0K`, etc.) automatically—but OpenCCU hides the `TEMPERATURE_OFFSET` parameter by default! To unlock `Offset Based` calibration:

1. Open **Settings → Devices & Services → Homematic(IP) Local** and click **Configure**.
2. Open **Advanced settings → UN-IGNORE parameters**.
3. Enable the `TEMPERATURE_OFFSET` master parameter for your radiator model, for example:
   - `TEMPERATURE_OFFSET:MASTER@HmIP-eTRV` *(Homematic IP)*
   - `TEMPERATURE_OFFSET:MASTER@HM-CC-RT-DN` or `TEMPERATURE_OFFSET:MASTER@HM-CC-RT-DN-BoM` *(Homematic Classic)*
4. Reload the Homematic integration so `select.<room>_temperature_offset` appears in Home Assistant, then open Better Thermostat's configuration to select **`Offset Based`**.

---

## Renaming entities safely

If you rename any entity ID that Better Thermostat uses in Home Assistant (a TRV, cooler, room sensor, window/door sensor, or outdoor sensor), **Better Thermostat detects the registry rename automatically**, migrates all per-TRV options, helper entities, and learned thermal/PID/MPC data to the new entity ID, and reloads itself cleanly. Removing a TRV from a room purges its learned state so reusing that entity ID later starts fresh.
