---
title: Supported devices & compatibility
description: Which Home Assistant integrations and TRV models work with Better Thermostat, Zigbee2MQTT vs. ZHA differences, Shelly BLU TRV setup, built-in model quirks, and known limitations.
---

## Supported devices

**Any radiator thermostat or wall thermostat exposed as a `climate` entity in Home Assistant (with a writable target temperature) works with Better Thermostat.**

Because Better Thermostat talks through Home Assistant entities rather than directly to a radio stick, **your underlying integration determines which [`Calibration type`](/calibration_algorithms/) options are unlocked:**

| Integration | `Target Temperature Based` | `Offset Based` | `Direct Valve Based` (`0–100 %`) | Notes |
|---|---|---|---|---|
| **Zigbee2MQTT (Z2M)** *(Recommended for Zigbee)* | ✅ All TRVs | ✅ Most TRVs (`number.*_local_temperature_calibration`) | ✅ Supported models (`TRVZB`, `SPZB0001`, Danfoss/Popp/Hive) | Exposes full device clusters (offsets, valve position, child lock, external sensor input). Enable Z2M's **Availability** tracking so dead batteries trigger [Degraded mode](/faq/degraded-mode/). |
| **ZHA (Zigbee Home Automation)** | ✅ All TRVs | ⚠️ Device-dependent (many TRVs hide offset in ZHA) | ⚠️ Rarely exposed | Works reliably via `Target Temperature Based` even when ZHA only creates a `climate` entity. |
| **Shelly (Native HA)** | ✅ Shelly TRV Gen1 (`SHTRV-01`) | ✅ Where exposed | ✅ **Shelly BLU TRV** (via BLU Gateway Gen3) | Disable Shelly's internal auto-temp mode on BLU TRVs to unlock writable valve control (see below). |
| **Homematic(IP) Local / OpenCCU** | ✅ All TRVs | ✅ Yes (via `select.*_temperature_offset` once un-ignored) | ❌ Not writable | Un-ignore `TEMPERATURE_OFFSET` in OpenCCU ([instructions](/configuration/#homematicip--openccu-enabling-offset-based-calibration)) and keep the **HomematicIP** 10-minute duty-cycle option enabled. |
| **Z-Wave JS** | ✅ All TRVs | ✅ When exposed as a `number` entity | ✅ **Eurotronic Spirit Z / Aeotec `ZWA021`** | On `ZWA021`, Better Thermostat automatically engages *Manufacturer Specific* mode when `Direct Valve Based` is chosen. |
| **tado° / deCONZ / Matter / Generic `climate`** | ✅ All TRVs | ✅ tado°, deCONZ, and any device exposing an offset entity | ❌ Usually not exposed | Works out of the box. |

---

## Built-in device quirks & optimizations

Better Thermostat automatically detects the device model in Home Assistant's Device Registry and loads dedicated quirk fixes where needed—no manual configuration required:

| Model ID / Family | Brand & Device | What Better Thermostat handles automatically |
|---|---|---|
| **`TRVZB`** | **Sonoff Zigbee TRV** | Supports **`Direct Valve Based`** (`0–100 %`), **`Offset Based`** (note `-12.7 °C` firmware lower bound), and automatically **mirrors your external room sensor reading** into the TRV's external temperature input when available. |
| **`TRV-ZBT`** | **Sonoff Zigbee TRV Gen2** | Automatically mirrors your external room sensor reading into the TRV's external sensor input. *(Does **not** use `Direct Valve Based`: its `heating_valve_position` / `idle_valve_position` numbers only set internal limits rather than positioning the motor directly.)* |
| **Shelly BLU TRV** | **Shelly BLU TRV (via BLU Gateway)** | Dedicated **`Direct Valve Based`** adapter (`0–100 %` valve actuator, `25 %` emergency opening on total sensor loss). See dedicated setup rules below. |
| **`SHTRV-01`** | **Shelly TRV (Gen1 Wi-Fi)** | Prevents `4.0 °C` minimum-setpoint writes from unintentionally reporting back as the user turning the thermostat `off` (floors active closing writes one `0.5 °C` step above `4.0 °C`). |
| **`SPZB0001`** | **Eurotronic Spirit Zigbee** | Supports **`Direct Valve Based`** (`0–100 %`) and safely clamps **`Offset Based`** writes to the hardware's `±5.0 °C` limit. |
| **`ZWA021`** | **Eurotronic Spirit Z / Aeotec Z-Wave** | Automatically switches the TRV into its hidden Z-Wave *Manufacturer Specific* mode when **`Direct Valve Based`** is selected (and leaves standard `climate` control untouched otherwise). |
| **`COZB0001`** | **Eurotronic Comet Zigbee** | Compensates for firmware that applies `local_temperature_calibration` to the internal setpoint instead of the reported temperature reading. |
| **`SEA801-Zigbee` / `SEA802-Zigbee`** | **Saswell / Tuya OEM Zigbee TRVs** | Normalizes offset rounding quirks. *(Hardware has a small local offset window—switch to **`Target Temperature Based`** if the radiator sits in a hot pocket!)* |
| **`BTH-RM` / `BTH-RM230Z`** | **Bosch Smart Home Thermostats** | Adapts temperature ranges and dual-setpoint climate handling. |
| **`ME167`** | **Avatto Zigbee TRV** | Verified offset passthrough and state synchronization. |
| **`BHT-002-GCLZB`** | **Moes / Beca Wall Thermostat** | Sanitizes erroneous `±1e6` floating-point calibration values sometimes published via Zigbee2MQTT. |
| **`TS0601` / `TS0601_thermostat` / `TV02-Zigbee`** | **Tuya / Moes / TuYa OEM Zigbee TRVs** | Direction-aware offset rounding and mode handling for quirky Tuya cluster implementations. |
| **`Heater` (TermoWeb)** | **TermoWeb Electric Radiators (`ha-termoweb`)** | Automatically inverts the temperature offset sign (`+1 K` offset lowers reported reading by `1 K` on TermoWeb hardware). |

:::caution[Community notes on tricky budget TRV firmware]
- **Moes `BRT-100`:** Known in community reports for aggressive internal eco/step jumps (`1–2 °C` steps) that fight external offset loops; use **`Target Temperature Based`** and disable all internal schedules/eco modes in Zigbee2MQTT.
- **Nedis `ZBHTR10WT`:** Lacks full converter exposure in several Zigbee stacks; verify that `climate` setpoints apply reliably in Home Assistant before wrapping it.
:::

Missing a quirk for your TRV model? [Open a GitHub issue](https://github.com/KartoffelToby/better_thermostat/issues) with a [Diagnostics download](/faq/debugging/).

---

## Shelly BLU TRV (direct valve control & repair warning)

Use Home Assistant's native **Shelly** integration with a compatible Shelly BLU Gateway (e.g. BLU Gateway Gen3):

1. **Disable Shelly's internal automatic temperature control** in the Shelly device settings first. Home Assistant will then replace the read-only valve sensor with a writable **`Valve position` (`number.*`)** entity on the same TRV device. Wait until that `number` entity is available before setting up Better Thermostat.
2. In Better Thermostat (Screen 2), select:
   - **`Calibration type`:** **`Direct Valve Based`**
   - **`Calibration mode`:** One of the **5 valve-computing modes** — **`(AI) Time Based (Default)`**, **`TPI Controller`**, **`(AI) MPC v2 (QP + Kalman, experimental)`**, **`PID Controller`**, or **`MPC Predictive (Beta)`**.
3. **Why you might see the *"Incompatible calibration mode for direct valve control"* repair warning (`invalid_valve_calibration`):**
   - `External Sensor Offset Only`, `Aggressive`, and `No Calibration` do not compute a `0–100 %` valve opening. Setup refuses that combination, and if an older stored configuration carries one, Better Thermostat raises a Repair warning, commands **`0 %` (closed)** for safety (blocking `Boost` too), and waits until you open **Configure** and pick one of the 5 valve modes above.
4. **How daily operation differs on an adopted Shelly BLU TRV:**
   - The `0–100 %` valve `number` is the **only** actuator: Better Thermostat sends no setpoints, HVAC modes, or offsets to the BLU TRV, and physical target/mode reports from the BLU TRV do not alter Better Thermostat's room target. Change temperatures and modes on the Better Thermostat entity.
   - Turning Better Thermostat `off` or opening a window/door closes the valve to **`0 %`**.
   - **Total sensor loss (`hold` mode):** Because Shelly's internal thermostat loop is disabled, if *both* your room sensor and the TRV's own temperature reading fail simultaneously, Better Thermostat requests a fixed **`25 %` emergency valve opening** (unless Better Thermostat is `off` or a window/door is open) until a temperature reading returns for 5 minutes ([Degraded mode](/faq/degraded-mode/)).
5. **Switching back to Shelly's built-in temperature control:**
   - First set Better Thermostat to **`off`** and wait until the valve reports **`0 %`**. Re-enable automatic temperature control inside Shelly, wait for Home Assistant to refresh the entities, and then reconfigure Better Thermostat to **`Target Temperature Based`** (or remove the room entry). Never flip Shelly's internal control mode while Better Thermostat is actively driving the valve.

---

## Known limitations

### 1. Better Thermostat can only use entities your integration exposes
Better Thermostat has no Zigbee, Z-Wave, or Bluetooth radio of its own:
- **`Offset Based`** requires a writable `number` or `select` offset entity on the TRV device (Z-Wave raw configuration parameters—like parameter 8 in tenths of a degree on Spirit Z-Wave—are intentionally not treated as offset entities).
- **`Direct Valve Based`** requires a writable valve-opening entity (or dedicated model quirk).
- **`Target Temperature Based`** works on every `climate` TRV.
- Better Thermostat cannot force a sleepy battery TRV to wake up faster, and it spaces routine writes at least **30 seconds** apart (**10 minutes** with the HomematicIP option) to protect batteries and radio airtime.

### 2. Frozen sensors require integration availability tracking
Every algorithm trusts your **`Room temperature sensor`**. When that sensor turns `unavailable`/`unknown` (or leaves `-50..60 °C`), Better Thermostat steps down safely to `sensor_fallback` after 2 minutes ([Degraded mode](/faq/degraded-mode/)). However, if a crashed sensor stops transmitting while its integration keeps showing a frozen `20.4 °C` forever, Home Assistant still marks it available!
- **Fix:** Turn on **Availability** in **Zigbee2MQTT** (and `expire_after` on custom MQTT sensors); ZHA enables timeout tracking automatically.

### 3. One physical TRV belongs to at most one Better Thermostat
Setup prevents assigning the same TRV to two Better Thermostat rooms, because two controllers fighting over one valve breaks calibration. Legacy configurations sharing a TRV raise a [Thermostat in multiple rooms](/faq/shared-trv/) repair warning. Put multiple TRVs of the same room into **one** Better Thermostat entry instead.

### 4. No built-in boiler relay or timetable
Better Thermostat controls room TRVs and an optional AC cooler—not central boiler switches, flow curves, or clock schedules. Pair it with a simple [`hvac_action` boiler automation](/deep-explanations/central-heating-thermostat/) and the bundled **[Weekly Heating Schedule blueprint](/setup/automation-blueprints/)**.

### 5. Humidity is for display and automation triggers only
Attaching a humidity sensor exposes live humidity on the `climate` entity and triggers `humidity_high` / `current_humidity_changed` automations, without shifting target temperatures.

### 6. Weather entities must provide a forecast
When using a `weather.*` entity for [Summer mode](/deep-explanations/summer-mode/), the integration must support Home Assistant weather forecasts. If forecast calls stop answering for more than 3 hours, Better Thermostat fails safe by allowing heating until forecasts return (or use a local **`Outdoor temperature sensor`** instead).

### 7. Fahrenheit systems: rate sensors stay in Kelvin & Celsius TRVs round to whole `°F`
- Diagnostic rate and error sensors (`Temperature Slope`, `Heating Power`, `Heat Loss`, `MPC Gain`, `MPC Loss`, `MPC v2 Disturbance` in `K/min`, and `PID Error` in `K`) always report temperature *differences* in **Kelvin** (`1 K = 1.8 °F`) ([Entities & attributes](/setup/entities/)).
- Most Zigbee TRVs work natively in `0.5 °C` (`0.9 °F`) steps and round Fahrenheit states in Home Assistant to whole degrees (`70 °F`, `71 °F`). To prevent endless reconciliation retries when `71.5 °F` echoes back as `71 °F` or `72 °F`, Better Thermostat keeps your room target at `70.5 °F` while sending whole-degree `°F` commands to whole-degree hardware.

### 8. Learning algorithms improve over days, not minutes
`(AI) Time Based`, `PID Auto Tune`, and `(AI) MPC v2` (`Auto`) learn across complete heating and cooling cycles. `PID Controller` and `MPC Predictive` are in Beta (`MPC Predictive` causes higher valve wear and can over-react in multi-TRV rooms), and `(AI) MPC v2` is experimental for direct-valve TRVs ([Calibration types & algorithms](/calibration_algorithms/)).
