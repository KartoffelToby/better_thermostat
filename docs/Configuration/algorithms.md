---
title: Calibration Algorithms
description: The Better Thermostat calibration algorithms and how to choose one.
slug: calibration_algorithms
---

Better Thermostat offers several calibration algorithms (the **Calibration mode** option) that control how your TRV (Thermostatic Radiator Valve) is adjusted to maintain your desired temperature. Each algorithm has different characteristics and is suited for different situations.

[Internals: Calibration](/internals/calibration/) documents the two
calibration channels, the controllers, and how a change is verified.

## Choosing an algorithm

Start here if you are unsure:

| Your situation | Algorithm |
| --- | --- |
| Setting up for the first time | AI Time Based (default) |
| Room heats too slowly | Aggressive |
| Temperature often overshoots | TPI Controller |
| You want fine control and know PID tuning | PID Controller |
| You want something simple | External Sensor Offset Only or TPI Controller |
| Your TRV supports direct valve control and you want to try an experimental controller | MPC v2 |
| You want the TRV to regulate on its own | No Calibration |

## The algorithms

### External Sensor Offset Only

This mode uses your external temperature sensor to correct the TRV's internal one. Better Thermostat compares the two readings and sends the TRV an offset, or a setpoint that already carries the difference; the TRV keeps running on its own sensor and never sees yours.

It is simple, reliable, works with most TRVs and costs almost nothing to run. In exchange it does not optimise for efficiency and does not adapt to the room.

Use it as a starting point when you want reliable control and no complexity.

---

### Aggressive

Aggressive works like External Sensor Offset Only but pushes the TRV harder: while the thermostat is heating, it shifts the offset or setpoint a further 2.5 °C in the heating direction, so the TRV opens wider than it otherwise would. It also skips the tolerance delay before heating starts.

That gets a slow or poorly insulated room warm quickly. The cost is overshoot, wasted energy when the speed was not needed, and more valve movement.

Use it when the room takes a long time to warm up, or when you need a fast change, such as coming home to a cold house.

---

### AI Time Based

The setup form labels this mode *(AI) Time Based (Default)*. This is the default, and the right choice for most rooms.

It learns your room's heating characteristics over time. It still reads your external temperature sensor, but derives the calibration from its own model rather than leaving the decision to the TRV's built-in logic, so it adapts to how fast your room actually heats and cools.

Once settled it balances comfort against energy use, keeps overshoot down and copes with changing conditions. The trade-off is the learning phase: it updates its heating-power model a small step after each heating cycle it observes, so how soon the results are good depends on how often the room heats, not on a fixed number of days. Until then behaviour is not yet tuned.

---

### MPC Predictive

The setup form labels this mode *MPC Predictive (Beta)*.

MPC (Model Predictive Control) predicts how your room temperature will change over the next 30 minutes, in six steps of five minutes. It reads several inputs, among them:

- Room temperature, its trend and your target
- Learned thermal properties of your room (how fast it heats and cools)
- Outdoor temperature, daylight and solar intensity
- Window state, and the valve opening it last asked for

From that prediction it picks the correction that reaches your target smoothly instead of driving hard and correcting afterward, and it keeps updating the model as the room behaves. With direct valve control that correction is a valve opening; without it, the correction reaches the valve through the setpoint the TRV sees.

It aims at arriving at the target rather than at arriving quickly. It is the most complex of the modes and it reacts deliberately rather than fast, which can read as sluggish at first. Give it several heating cycles before judging it.

MPC is still in testing. In the project's benchmark it does not overshoot less than the other controller modes, it moves the valve more often than PID or TPI, and with several radiators in one room it can over-react. For a room that overshoots, start with TPI instead.

Its learned model shows up in the *Virtual Temperature*, *MPC Gain*, *MPC Loss* and *MPC Insulation (Ka)* sensors. They start disabled on new installations; see [Entities](/setup/entities/#mpc-predictive).

---

### MPC v2

The setup form labels this mode *(AI) MPC v2 (QP + Kalman, experimental)*.

MPC v2 is an experimental predictive controller. It estimates the room's state with a Kalman observer and computes the valve opening by solving a small optimisation problem (a quadratic program) over a receding horizon. It is written for TRVs with direct valve control; see [Direct valve control](#direct-valve-control).

The **MPC v2 room size** option sets the room model it starts from. *Auto* derives it from the heat-loss rate Better Thermostat has learned for the room; the small, medium and large room presets use a fixed model instead.

Its estimates show up in the *MPC v2 Virtual Temperature*, *MPC v2 Coupling*, *MPC v2 Disturbance* and *MPC v2 Room Time Constant* sensors. They start disabled on new installations; see [Entities](/setup/entities/#mpc-v2).

---

### PID Controller

⚠️ **Beta Status:** The PID Controller is currently in beta and may require further fine-tuning in the algorithm. While it's functional and includes auto-tuning capabilities, you may experience some edge cases that need optimization. Feedback and real-world testing are appreciated.

PID (Proportional-Integral-Derivative) is the classic industrial control method. It sets the valve position from three terms:

- P (Proportional): how far you are from the target temperature
- I (Integral): how long you have been away from it
- D (Derivative): how fast the temperature is moving

It tunes those three itself over time.

PID reacts fast and handles disturbances well, which is what makes it a good fit for a room with sun through the windows, draughts or a heat source whose output varies. Early on it can be aggressive and oscillate a little while it tunes, and getting the most out of it means understanding roughly what the three parameters do.

Pick it when your heating power varies, when outside influences keep moving the room temperature, and when you want a responsive controller and are comfortable with the parameters.

#### Auto-tuning and manual tuning

Auto-tuning is on by default.

**How tuning proceeds:**

- **At the start:** The controller starts with default values (Kp=60, Ki=0.01, Kd=2000) and begins learning your room's behavior. You may notice slight temperature oscillations as it adjusts.

- **While tuning:** The algorithm adjusts the gains at most every 5 minutes, and only when one of these conditions holds (the "target band" is 0.1 °C either side of the target):
  - **Arriving at the target:** The room was outside the target band on the previous cycle and is inside it now, from either side. It decreases Kp (less aggressive), increases Kd (more damping) and decreases Ki
  - **Sluggish response:** The room is more than 0.1 °C below the target, its temperature changes by less than 0.005 °C per minute (0.3 °C per hour) and the valve output is below 95 %. It increases Ki and Kp
  - **Steady state:** The room is inside the target band and the valve output is below 20 %. It decreases Ki

- **Settled:** Kp and Kd stop changing once neither of the first two conditions occurs any more: the room no longer arrives at the target band and no longer heats up too slowly. While the room holds the target with a small valve output, Ki keeps shrinking every 5 minutes until it reaches its lower limit of 0.001. How many heating cycles that takes depends on the room; the code sets no fixed period.

**What to expect:**

- Adjustments happen at least 5 minutes apart (300 seconds) to avoid over-tuning
- Parameters are constrained to safe ranges:
  - Kp: 10-500
  - Ki: 0.001-2.0
  - Kd: 100-10,000
- Auto-tuning is conservative - it makes small changes and learns gradually

**Manual tuning:**

Each TRV's *PID Kp (Proportional)*, *PID Ki (Integral)* and *PID Kd (Derivative)* numbers accept Kp from 0 to 1000, Ki from 0 to 100 and Kd from 0 to 10,000, and Better Thermostat keeps a value set there, including 0 (Kd 0 makes a PI controller). With auto-tuning on, a value you set is the starting point it adjusts from; turn off the *PID Auto Tune* switch to keep fixed values.

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

To follow the controller over time, the device has diagnostic sensors: *PID Output* for the sum of the three terms in percent and *PID Error* for the target minus the room temperature. The MPC Predictive and MPC v2 modes have diagnostic sensors of their own. On new installations all of these start disabled; enable the ones you want on the device page, in the sensor's settings. Installations that had them before keep them enabled, and a sensor you enabled stays enabled when you switch the calibration mode away and back again. [Entities](/setup/entities/#algorithm-sensors) lists every sensor and what its value means.

**Getting the best out of PID:**

- Give auto-tuning time: it only changes the gains after an overshoot, a slow rise or a drift, so it needs a number of ordinary heating cycles
- Keep target temperatures consistent; auto-tuning reads a moving target as a disturbance
- Avoid changing the target often during the learning phase
- Place the external sensor away from heat sources and draughts
- Prefer a device with direct valve control (see [Direct valve control](#direct-valve-control))

**Turning auto-tuning off:**

While not recommended for most users, auto-tuning can be disabled with the *PID Auto Tune* switch of the device if you prefer fixed PID parameters. This is only useful if you have specific PID values you want to maintain.

---

### TPI Controller

TPI (Time Proportional Integral) turns the distance from your target (plus, when an outdoor temperature is available, the difference between target and outdoor temperature) into a duty cycle between 0 and 100 %. Better Thermostat uses that duty cycle as a steady valve opening: at 60 % demand the valve is held 60 % open.

The model is easy to follow and suits a radiator with real thermal inertia. It does less than MPC or PID, and it adapts less readily when conditions change.

Pick it when your heating system is consistent and you want predictable behaviour without the machinery of the learning modes.

---

## Comparison

| Feature | External Sensor Offset Only | Aggressive | AI Time Based | MPC Predictive | PID Controller | TPI Controller |
| --------- | -------- | ------------ | --------------- | ---------------- | ---------------- | ---------------- |
| **Complexity** | Low | Low | Medium | High | Medium | Low |
| **Learning** | No | No | Yes | Yes | Yes | No |
| **Response Speed** | Medium | Fast | Medium | Measured | Fast | Medium |
| **Writes the valve directly** | No | No | Yes | Yes | Yes | Yes |
| **Status** | Stable | Stable | Stable | Beta | Beta | Stable |
| **Best For** | Simple setups | Fast heating | Most users | Testing (beta) | Variable systems | Less overshoot, little valve wear |

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
- **Z-Wave JS TRVs** that expose a valve position entity, and the Eurotronic Spirit Z / Aeotec ZWA021
- **Shelly BLU TRV**, once its built-in thermostat is switched off on the Shelly side and Home Assistant shows its valve position entity. Reload Better Thermostat after switching, so it finds the entity
- **Other Zigbee TRVs** that expose valve control through their integration

Better Thermostat detects whether your TRV supports direct valve control and then offers the **Direct Valve Based** calibration type. It only writes the valve when you select that type.

### How the algorithms use it

With the Direct Valve Based calibration type:

- **MPC Predictive**: Calculates a valve opening from its prediction of where the room is heading, and that opening is written as it stands.

- **PID Controller**: Directly outputs valve position based on temperature error and trends.

- **TPI Controller**: Sets valve opening based on heating duty cycle calculations.

- **MPC v2**: Computes the valve opening from its optimisation.

- **AI Time Based**: Derives a valve opening from the heating power it has learned.

- **External Sensor Offset Only, Aggressive, No Calibration**: These produce no valve opening. Better Thermostat sends them to the TRV as a target temperature instead.

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

If you're purchasing new TRVs and want the best performance from Better Thermostat's advanced algorithms (especially MPC Predictive or PID Controller), consider devices that support direct valve control through Zigbee2MQTT or similar integrations.

## Getting good results

1. **Give it time:** AI Time Based, MPC Predictive and PID Controller learn from the heating cycles they observe. Judge them after several days of ordinary use, not after the first heat-up; how long they need depends on the room and how often it heats.

2. **Stable placement:** Keep your external temperature sensor in a consistent location away from heat sources, drafts, and direct sunlight.

3. **Start with defaults:** Try AI Time Based first. Only change if you have specific issues.

4. **Monitor and adjust:** Check the temperature graphs in Home Assistant after a few days. If you see problems (overshooting, slow response, etc.), try a different algorithm.

5. **Consider your heating system:**
   - Fast-responding systems (electric radiators): PID or TPI work well
   - Slow-responding systems (water radiators, underfloor): PID Controller works well
   - Inconsistent heating power: PID Controller handles this best

## Troubleshooting

**Temperature overshoots:**

- Try: TPI Controller, or enable overheating protection (it acts in AI Time Based and Aggressive)

**Too slow to reach temperature:**

- Try: Aggressive mode

**Temperature oscillates up and down:**

- Try: Increase the Tolerance setting in first configuration step

**Algorithm isn't working well:**

- Give AI Time Based, MPC Predictive and PID Controller several days of ordinary use before judging them
- Check sensor placement and accuracy
- Verify TRV is working correctly
- Try a different algorithm

## Technical details

For developers and advanced users who want to understand the implementation details, see:

- [Hydraulic balance](/deep-explanations/hydraulic-balance/) - How the controller modes drive the valve or the setpoint
- Source code in `custom_components/better_thermostat/utils/calibration/` directory

## Further reading

If you're still unsure which algorithm to use or experiencing issues:

1. Check the [FAQ](/faq/common-questions/) for common questions
2. Visit the [GitHub Discussions](https://github.com/KartoffelToby/better_thermostat/discussions)
3. Report bugs on [GitHub Issues](https://github.com/KartoffelToby/better_thermostat/issues)
