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
1. Adapter support in Better Thermostat
2. A compatible offset capability on your TRV/integration

Currently, integrations with local calibration support include:

- Tado
- Zigbee2MQTT
- deCONZ
- Z-Wave JS, when the TRV exposes a calibration entity. A configuration parameter is used as one when its unit is degrees Celsius or a tenth or hundredth of one: the temperature offset of the Eurotronic Spirit and the Aeotec ZWA021, parameter 8, counts tenths of a degree and is read and written in that unit, and its reserved value -128 (external sensor) is never written. Home Assistant creates configuration parameters disabled, so enable that entity and reload Better Thermostat
- Any other integration whose TRV exposes a calibration entity (a `number` or `select`) that Better Thermostat can find

## Direct valve control (recommended for MPC/PID)

Direct valve control requires both:

1. Adapter support in Better Thermostat
2. A controllable valve position entity exposed by your TRV/integration

Devices that expose a controllable valve position work best with the advanced control modes (MPC/PID), because BT can set the valve opening directly. Select the **Direct Valve Based** calibration type to use it.

### Shelly BLU TRV (direct valve control)

Use Home Assistant's native Shelly integration with a compatible BLU gateway. The BLU TRV's own automatic temperature control must be disabled in Shelly's settings. Home Assistant then exposes its writable **Valve position** `number` instead of the read-only position sensor. Wait for the number to become available before configuring Better Thermostat. It must belong to the same TRV device as the `climate` entity; a gateway can serve several TRVs.

In Better Thermostat, select the physical TRV and the room temperature sensor, then set the **Calibration type** to **Direct Valve Based** and the **Calibration mode** to **AI Time Based**, **MPC**, **MPC v2**, **PID** or **TPI**. The calibration modes External Sensor Offset Only (stored as Normal), Aggressive and No Calibration do not produce a valve opening and are rejected for this combination in both setup and options. A stored configuration with an incompatible or unknown calibration mode raises a Repairs issue; BT requests 0% and blocks opening that valve, including Boost, until its options are corrected. The entry stays loaded to retry closure if the device is temporarily unreachable. BT does not replace the saved calibration mode automatically.

During regulation, the valve percentage is the only actuator: Better Thermostat does not send a setpoint, HVAC mode or calibration offset to the BLU TRV. A device that still reports a target temperature follows the same rule. Change the room's target and mode on Better Thermostat; setpoint or mode reports from the physical TRV do not change them. Temperature and availability reports are still read. Periodic reconciliation checks the valve position, leaving differences in thermostat settings alone. An OFF or open-contact decision closes the valve rather than switching the device's thermostat off. Standard startup tweaks and the common valve-maintenance routine are unchanged.

This policy is specific to an adopted BLU TRV valve. The Shelly TRV Gen1 also exposes a valve number, but its thermostat keeps running; that number does not enable this direct-control path. Ordinary target-temperature and offset control retain their existing behaviour.

#### Switching back to Shelly's temperature control

First set Better Thermostat to OFF and wait until the valve reports 0%. Stop any other automation that writes the valve. Re-enable automatic temperature control in Shelly, wait for Home Assistant to update the device's entities, then set the **Calibration type** in Better Thermostat to **Target Temperature Based** (or stop using Better Thermostat for that device). For direct control again, disable Shelly's automatic control, wait for the writable number, and set the calibration type to Direct Valve Based with a supported calibration mode. Do not switch Shelly modes while Better Thermostat is actively controlling the valve.

#### Unavailable devices and outages

An unavailable valve number does not cause Better Thermostat to fall back to setpoint or HVAC commands. The normal write budget and retry handling apply; there is no extra sleep inside the control lock for BLU TRVs. Restore the gateway connection or reload the Shelly integration after a firmware update if its entities do not recover. If an entity was disabled, re-enable it; if the device's mode changed, restore the intended mode and reconfigure BT.

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
## Room temperature written to the TRV

Some TRVs can regulate on a room temperature sent to them instead of their own sensor. For these models Better Thermostat writes its room temperature to the device:

- **Sonoff TRVZB and TRV-ZBT** (Zigbee2MQTT): to `external_temperature_input`, and switches the device's temperature sensor selection to it. A TRVZB paired through ZHA gets it in `external_temperature_sensor_value`, and its `external_temperature_sensor` switch is turned on.
- **Danfoss Ally, Popp 701721, Hive UK7004240** (Zigbee2MQTT): to `external_measured_room_sensor`, in hundredths of a degree Celsius as Zigbee2MQTT expects, but only with the calibration mode **No Calibration**. The device corrects for its own sensor itself once it has the room temperature, so any other calibration mode would correct the same bias a second time. A change of at least 0.1 °C is written at once. An unchanged value goes out again with Better Thermostat's 30-minute refresh, which stays within the time after which the device drops the value and returns to its own sensor: 3 hours, or 35 minutes with `radiator_covered` on. Better Thermostat does not change `radiator_covered`; set it to match how the radiator is mounted. Open windows are not reported through `window_open_external`, and `heat_available` is not used.

No value is written while the room sensor gives no reading, or when the room sensor belongs to the TRV itself.

## Devices that are known to work

## Shelly BLU TRV (direct valve control & repair warning)

Use Home Assistant's native **Shelly** integration with a compatible Shelly BLU Gateway (e.g. BLU Gateway Gen3):
- 014G2461 (Danfoss Ally, also Danfoss 014G2463, Popp 701721 and Hive UK7004240)
- BHT-002-GCLZB
- BTH-RM
- BTH-RM230Z
- COZB0001 (Eurotronic Comet Zigbee)
- Heater (TermoWeb electric radiator, through the ha-termoweb integration)
- ME167 (Avatto)
- SEA801-Zigbee / SEA802-Zigbee
- SHTRV-01 (Shelly TRV, Gen1)
- SPZB0001 (Eurotronic Spirit Zigbee)
- TRV-ZBT (Sonoff TRV-ZBT)
- TRVZB (Sonoff TRVZB)
- TS0601
- TS0601_thermostat
- TV02-Zigbee
- ZWA021 (Eurotronic Spirit Z / Aeotec ZWA021, Z-Wave)

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
