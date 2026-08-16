---
title: Calibration Algorithms
description: The Better Thermostat calibration algorithms and how to choose one.
slug: calibration_algorithms
---

Better Thermostat offers several calibration algorithms (also called "Calibration Modes") that control how your TRV (Thermostatic Radiator Valve) is adjusted to maintain your desired temperature. Each algorithm has different characteristics and is suited for different situations.

## Choosing an algorithm

Start here if you are unsure:

| Your situation | Algorithm |
| --- | --- |
| Setting up for the first time | AI Time Based (default) |
| Room heats too slowly | Aggressive |
| Temperature often overshoots | MPC Predictive |
| You want fine control and know PID tuning | PID Controller |
| You want something simple | Normal or TPI Controller |

## The algorithms

### Normal

Normal mode uses your external temperature sensor to correct the TRV's internal sensor. The TRV reads the actual room temperature from your external sensor and adjusts accordingly.

It is simple, reliable, works with most TRVs and costs almost nothing to run. In exchange it does not optimise for efficiency and does not adapt to the room.

Use it as a starting point when you want reliable control and no complexity.

---

### Aggressive

Aggressive works like Normal but pushes the TRV harder: it reports the internal temperature much lower than it is while heating, and higher while cooling, so the TRV runs at full power until the target is reached.

That gets a slow or poorly insulated room warm quickly. The cost is overshoot, wasted energy when the speed was not needed, and more valve movement.

Use it when the room takes a long time to warm up, or when you need a fast change, such as coming home to a cold house.

---

### AI Time Based

This is the default, and the right choice for most rooms.

It learns your room's heating characteristics over time. It still reads your external temperature sensor, but derives the calibration from its own model rather than leaving the decision to the TRV's built-in logic, so it adapts to how fast your room actually heats and cools.

Once settled it balances comfort against energy use, keeps overshoot down and copes with changing conditions. The trade-off is the learning phase: expect two to three days before the results are good, and accept that behaviour in that window is not yet tuned.

---

### MPC Predictive

MPC (Model Predictive Control) predicts how your room temperature will change over the next hour. It reads several inputs, among them:

- Room temperature, its trend and your target
- Learned thermal properties of your room (how fast it heats and cools)
- Outdoor temperature, daylight and solar intensity
- Window state, and the valve opening it last asked for

From that prediction it picks the correction that reaches your target smoothly instead of driving hard and correcting afterward, and it keeps updating the model as the room behaves. With direct valve control that correction is a valve opening; without it, the correction reaches the valve through the setpoint the TRV sees.

Of all the modes it is the best at avoiding overshoot and the most economical with energy. It is also the most complex, and it reacts deliberately rather than quickly, which can read as sluggish at first. Give it about a day of operation before judging it.

Pick it when you overshoot regularly, when efficiency matters more to you than reaction speed, and when your heating system itself is reasonably stable.

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

**Timeline:**

- **Initial period (Days 1-3):** The controller starts with default values (Kp=20, Ki=0.02, Kd=400) and begins learning your room's behavior. You may notice slight temperature oscillations as it adjusts.

- **Learning phase (Days 4-7):** The algorithm makes adjustments every 5 minutes (minimum) based on:
  - **Overshoot detection:** If temperature overshoots target, it decreases Kp (makes it less aggressive) and increases Kd (improves damping)
  - **Sluggish response:** If heating is too slow, it increases Ki (improves steady-state accuracy)
  - **Steady-state drift:** If temperature drifts near target, it decreases Ki (prevents accumulation)

- **Settled phase (Week 2+):** After about 1-2 weeks, the parameters should stabilize and provide smooth temperature control with minimal overshooting.

**What to expect:**

- Adjustments happen at least 5 minutes apart (300 seconds) to avoid over-tuning
- Parameters are constrained to safe ranges:
  - Kp: 10-500
  - Ki: 0.001-1.0
  - Kd: 100-10,000
- Auto-tuning is conservative - it makes small changes and learns gradually

**Manual tuning:**

If you want to tune PID parameters manually or understand what the auto-tuning is doing:

1. **Kp (Proportional gain):** Controls immediate response to temperature error
   - Too high: Oscillations and overshoot
   - Too low: Slow response, takes long to reach target
   - Default: 20

2. **Ki (Integral gain):** Eliminates steady-state error over time
   - Too high: Oscillations, instability
   - Too low: Never quite reaches target (offset)
   - Default: 0.02

3. **Kd (Derivative gain):** Predicts future error based on rate of change
   - Too high: Sensitive to noise, erratic behavior
   - Too low: Overshoot, slow damping
   - Default: 400

**Monitoring the learned values:**

You can monitor the learned PID values in Home Assistant:

1. Go to Developer Tools → States
2. Find your Better Thermostat entity
3. Look for attributes containing PID debug info showing current Kp, Ki, Kd values

**Getting the best out of PID:**

- Give auto-tuning one to two weeks to settle
- Keep target temperatures consistent; auto-tuning reads a moving target as a disturbance
- Avoid changing the target often during the learning phase
- Place the external sensor away from heat sources and draughts
- Prefer a device with direct valve control (see [Direct valve control](#direct-valve-control))

**Turning auto-tuning off:**

While not recommended for most users, auto-tuning can be disabled through the advanced configuration if you prefer fixed PID parameters. This is only useful if you have specific PID values you want to maintain.

---

### TPI Controller

TPI (Time Proportional Integral) turns the distance from your target into a duty cycle: what share of the time the valve should be open. At 60 % demand it might hold the valve fully open for six minutes, then closed for four.

The model is easy to follow and suits a radiator with real thermal inertia, where a slow on/off rhythm is closer to how the heat actually arrives. It does less than MPC or PID, and it adapts less readily when conditions change.

Pick it when your heating system is consistent and you want predictable behaviour without the machinery of the learning modes.

---

## Comparison

| Feature | Normal | Aggressive | AI Time Based | MPC Predictive | PID Controller | TPI Controller |
| --------- | -------- | ------------ | --------------- | ---------------- | ---------------- | ---------------- |
| **Complexity** | Low | Low | Medium | High | Medium | Low |
| **Learning** | No | No | Yes | Yes | Yes | No |
| **Overshoot Prevention** | Basic | Poor | Good | Excellent | Good | Good |
| **Energy Efficiency** | Medium | Low | High | Very High | High | Medium |
| **Response Speed** | Medium | Fast | Medium | Measured | Fast | Medium |
| **Adaptation** | None | None | Good | Excellent | Good | None |
| **Direct Valve Benefit** | Low | Low | Medium | **High** | **High** | Medium |
| **Status** | Stable | Stable | Stable | Stable | Beta | Stable |
| **Best For** | Simple setups | Fast heating | Most users | Optimization | Variable systems | Simple control |

**Notes:**

- "Direct Valve Benefit" indicates how much the algorithm gains from direct valve control (see [Direct valve control](#direct-valve-control) below)
- **PID Controller** is in beta and may require further algorithm fine-tuning

## How algorithms and calibration types combine

The **Calibration Mode** (algorithm) works together with the **Calibration Type**:

- **Target Temperature Based:** The algorithm calculates what target temperature to send to the TRV. For example, if you want 20°C but the room is cold, it might send 22°C to the TRV to make it heat more.

- **Offset Based:** The algorithm calculates what temperature offset to send to the TRV. For example, if the TRV's internal sensor reads 21°C but your external sensor reads 20°C, it sends an offset of -1°C.

Not all TRVs support offset-based calibration. Better Thermostat will automatically detect your TRV's capabilities and offer appropriate options.

## Direct valve control

Some TRV devices support **direct valve control**, where Better Thermostat can directly set the valve opening percentage (0-100%) instead of only adjusting target temperatures or offsets. That gives the algorithms finer control, which matters most for MPC and PID.

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
- **Other Zigbee TRVs** that expose valve control through their integration

Better Thermostat automatically detects if your TRV supports direct valve control.

### How the algorithms use it

When direct valve control is available:

- **MPC Predictive**: Calculates optimal valve opening based on predicted temperature changes. This is where direct valve control shines - the algorithm can precisely control heating power.

- **PID Controller**: Directly outputs valve position based on temperature error and trends. Very effective with direct valve control.

- **TPI Controller**: Sets valve opening based on heating duty cycle calculations.

- **AI Time Based, Normal, Aggressive**: These algorithms will still work but convert their output to valve positions when direct control is available.

### Without direct valve control

If your TRV doesn't support direct valve control, Better Thermostat uses **setpoint manipulation**:

- Adjusts the target temperature sent to the TRV
- Or adjusts the temperature offset (if supported)
- The TRV's internal controller then adjusts the valve based on its own logic

This still works well but gives the TRV's internal algorithm more influence over the final valve position.

### Checking whether you have it

1. Go to your Better Thermostat device in Home Assistant
2. Check the device attributes for entries like:
   - `valve_position_entity`
   - `valve_position_writable`
3. If these are present and `valve_position_writable` is `true`, you have direct valve control

For MQTT/Zigbee2MQTT users, you can also check if your TRV exposes entities like:

- `number.your_trv_valve_position`
- `number.your_trv_valve_opening_degree`

### What it buys you

The algorithm sets the valve opening itself rather than asking the TRV's own logic for it, so the response arrives without a detour and the room's reaction is a cleaner signal to learn from. Valve position is not heat output — flow temperature and the valve's own authority still sit in between — but it is the finest handle Better Thermostat can get, and a finer handle leaves less room for overshoot. MPC and PID gain the most from it; every mode runs without it.

### If you are buying new TRVs

If you're purchasing new TRVs and want the best performance from Better Thermostat's advanced algorithms (especially MPC Predictive or PID Controller), consider devices that support direct valve control through Zigbee2MQTT or similar integrations.

## Getting good results

1. **Give it time:** Algorithms with learning need time to learn your room:
   - **MPC Predictive**: 1 day for fine-tuned performance
   - **AI Time Based**: 2-3 days for optimal performance
   - **PID Controller**: 1-2 weeks for auto-tuning to settle

2. **Stable placement:** Keep your external temperature sensor in a consistent location away from heat sources, drafts, and direct sunlight.

3. **Start with defaults:** Try AI Time Based first. Only change if you have specific issues.

4. **Monitor and adjust:** Check the temperature graphs in Home Assistant after a few days. If you see problems (overshooting, slow response, etc.), try a different algorithm.

5. **Consider your heating system:**
   - Fast-responding systems (electric radiators): PID or TPI work well
   - Slow-responding systems (water radiators, underfloor): MPC Predictive works well
   - Inconsistent heating power: PID Controller handles this best

## Troubleshooting

**Temperature overshoots:**

- Try: MPC Predictive or increase hysteresis settings

**Too slow to reach temperature:**

- Try: Aggressive mode or reduce hysteresis settings

**Temperature oscillates up and down:**

- Try: Increase the Tolerance setting in first configuration step
- Or: Increase hysteresis in advanced settings

**Algorithm isn't working well:**

- **MPC Predictive**: Wait at least 1 day for learning
- **AI Time Based**: Wait 2-3 days for learning
- **PID Controller**: Wait 1-2 weeks for auto-tuning to settle
- Check sensor placement and accuracy
- Verify TRV is working correctly
- Try a different algorithm

## Technical details

For developers and advanced users who want to understand the implementation details, see:

- [Hydraulic Balance Design Document](../../hydraulic_balance_design.md) - Deep technical documentation
- Source code in `custom_components/better_thermostat/utils/calibration/` directory

## Further reading

If you're still unsure which algorithm to use or experiencing issues:

1. Check the [FAQ](../faq/common-questions.md) for common questions
2. Visit the [GitHub Discussions](https://github.com/KartoffelToby/better_thermostat/discussions)
3. Report bugs on [GitHub Issues](https://github.com/KartoffelToby/better_thermostat/issues)
