---
layout: default
title: Calibration Mode Algorithms
nav_order: 2
has_children: false
permalink: calibration_modes
---

Better Thermostat offers several calibration algorithms (also called "Calibration Modes") that control how your TRV (Thermostatic Radiator Valve) is adjusted to maintain your desired temperature. Each algorithm has different characteristics and is suited for different situations.

## Choosing the Right Algorithm

If you're unsure which algorithm to use, here's a quick guide:

- **Just starting out?** Try **Time Based** (default) - it works well for most situations
- **Room heats too slowly?** Try **Aggressive**
- **Temperature overshoots often?** Try **MPC Predictive** (tested & stable)
- **Have technical knowledge and want fine control?** Try **PID Controller** (⚠️ beta)
- **Want something simple and reliable?** Try **Normal** or **TPI Controller**

## Algorithm Descriptions

### Normal

**Best for:** Simple, straightforward temperature control

**How it works:** Normal mode uses your external temperature sensor to correct the TRV's internal sensor. The TRV reads the actual room temperature from your external sensor and adjusts accordingly.

**Pros:**

- Simple and reliable
- Works well with most TRVs
- Low computational overhead

**Cons:**

- May not optimize for efficiency
- Doesn't adapt to room characteristics

**When to use:** This is a good starting point if you want reliable temperature control without any complexity.

---

### Aggressive

**Best for:** Rooms that heat slowly or need faster temperature changes

**How it works:** Similar to Normal mode, but it pushes the TRV harder by setting the internal temperature sensor reading much lower (when heating) or higher (when cooling) than actual. This makes the TRV work at full power to reach your target faster.

**Pros:**

- Reaches target temperature quickly
- Good for poorly insulated rooms
- Effective for rapid warmup

**Cons:**

- May overshoot the target temperature
- Can waste energy if not needed
- More frequent valve adjustments

**When to use:** Your room takes a long time to heat up, or you need to quickly change temperature (e.g., coming home to a cold house).

---

### Time Based

**Best for:** Most users - balances comfort and efficiency (Default)

**How it works:** This algorithm learns your room's heating characteristics over time. It uses your external temperature sensor but calculates calibration values using a custom algorithm that improves on the TRV's built-in logic. It adapts to your room's thermal properties.

**Pros:**

- Automatically adapts to your room
- Balances comfort and energy efficiency
- Reduces temperature overshooting
- Works well in varying conditions

**Cons:**

- Takes a few days to fully learn your room
- May not be optimal during the learning period

**When to use:** This is the recommended default for most users. Expect good results after 2-3 days as it learns your room's behavior.

---

### MPC Predictive

**Best for:** Stable, efficient heating with minimal overshooting

**How it works:** MPC (Model Predictive Control) is an advanced algorithm that predicts how your room temperature will change over the next hour based on:

- Current valve position
- Temperature trends
- Learned thermal properties of your room (how fast it heats/cools)

It calculates the optimal valve opening to reach your target temperature smoothly without overshooting. It continuously learns and adapts to your room's behavior.

**Pros:**

- Excellent at preventing temperature overshoot
- Very energy efficient
- Smooth temperature control
- Learns room heating/cooling characteristics
- Predictive - anticipates temperature changes

**Cons:**

- Most complex algorithm
- Requires a short learning period (typically 1 day for fine-tuned performance)
- May seem slow to react initially (by design)

**When to use:**

- You experience frequent temperature overshoots
- Energy efficiency is a priority
- You want the most sophisticated control
- Your heating system is relatively stable

**Note:** MPC learns quickly - expect fine-tuned performance after just 1 day of operation. Initial behavior may seem conservative as it gathers data, but it rapidly adapts to your room's thermal characteristics. **This algorithm is considered stable and production-ready.**

---

### PID Controller

⚠️ **Beta Status:** The PID Controller is currently in beta and may require further fine-tuning in the algorithm. While it's functional and includes auto-tuning capabilities, you may experience some edge cases that need optimization. Feedback and real-world testing are appreciated.

**Best for:** Systems with varying heating power or external disturbances

**How it works:** PID (Proportional-Integral-Derivative) is a classic control method used in industrial applications. It adjusts the valve position based on:

- **P (Proportional):** How far you are from target temperature
- **I (Integral):** How long you've been away from target
- **D (Derivative):** How fast the temperature is changing

The algorithm automatically tunes these parameters over time for optimal performance.

**Pros:**

- Proven industrial control method
- Handles disturbances well (e.g., opening windows, sun through windows)
- Self-tuning capability
- Fast response to temperature changes
- Good for varying heating conditions

**Cons:**

- Can be aggressive initially
- May oscillate slightly during self-tuning
- More technical - understanding the parameters helps

**When to use:**

- Your heating system power varies
- You have external factors affecting room temperature (sun, drafts, etc.)
- You want responsive temperature control
- You have some technical knowledge

#### PID Auto-Tuning and Manual Tuning

The PID Controller includes an **auto-tuning feature** that is enabled by default. Here's what you need to know:

**Auto-Tuning Timeline:**

- **Initial period (Days 1-3):** The controller starts with default values (Kp=20, Ki=0.02, Kd=400) and begins learning your room's behavior. You may notice slight temperature oscillations as it adjusts.

- **Learning phase (Days 4-7):** The algorithm makes adjustments every 5 minutes (minimum) based on:

  - **Overshoot detection:** If temperature overshoots target, it decreases Kp (makes it less aggressive) and increases Kd (improves damping)
  - **Sluggish response:** If heating is too slow, it increases Ki (improves steady-state accuracy)
  - **Steady-state drift:** If temperature drifts near target, it decreases Ki (prevents accumulation)

- **Settled phase (Week 2+):** After about 1-2 weeks, the parameters should stabilize and provide smooth temperature control with minimal overshooting.

**What to Expect:**

- Adjustments happen at least 5 minutes apart (300 seconds) to avoid over-tuning
- Parameters are constrained to safe ranges:
  - Kp: 10-500
  - Ki: 0.001-1.0
  - Kd: 100-10,000
- Auto-tuning is conservative - it makes small changes and learns gradually

**Manual Tuning (Advanced Users):**

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

**Monitoring Auto-Tuning:**

You can monitor the learned PID values in Home Assistant:

1. Go to Developer Tools → States
2. Find your Better Thermostat entity
3. Look for attributes containing PID debug info showing current Kp, Ki, Kd values

**Tips for Best PID Performance:**

- **Be patient:** Give auto-tuning at least 1-2 weeks to fully settle
- **Stable conditions:** Auto-tuning works best when you maintain consistent target temperatures
- **Avoid manual interference:** During the learning phase, avoid frequently changing target temperatures
- **Temperature sensor placement:** Ensure your external sensor is well-placed (away from heat sources, drafts)
- **Direct valve control:** PID works best with devices that support direct valve control (see [Direct Valve Control](#direct-valve-control) section)

**Disabling Auto-Tuning:**

While not recommended for most users, auto-tuning can be disabled through the advanced configuration if you prefer fixed PID parameters. This is only useful if you have specific PID values you want to maintain.

---

### TPI Controller

**Best for:** Simple, consistent heating patterns

**How it works:** TPI (Time Proportional Integral) calculates heating duty cycles based on how far your current temperature is from your target. It determines what percentage of time the valve should be open. For example, if you need 60% heating, it might open the valve fully for 6 minutes, then close for 4 minutes.

**Pros:**

- Simple and effective
- Good for consistent heating patterns
- Easy to understand
- Works well with radiators that have thermal inertia

**Cons:**

- Less sophisticated than MPC or PID
- May not adapt to changing conditions as well
- Simpler algorithm with fewer optimizations

**When to use:**

- You want simple, predictable behavior
- Your heating system is consistent
- You don't need advanced features
- You're looking for a straightforward alternative to Normal mode

---

## Comparison Table

| Feature                  | Normal        | Aggressive   | Time Based | MPC Predictive      | PID Controller   | TPI Controller |
| ------------------------ | ------------- | ------------ | ---------- | ------------------- | ---------------- | -------------- |
| **Complexity**           | Low           | Low          | Medium     | High                | Medium           | Low            |
| **Learning**             | No            | No           | Yes        | Yes                 | Yes              | No             |
| **Overshoot Prevention** | Basic         | Poor         | Good       | Excellent           | Good             | Good           |
| **Energy Efficiency**    | Medium        | Low          | High       | Very High           | High             | Medium         |
| **Response Speed**       | Medium        | Fast         | Medium     | Measured            | Fast             | Medium         |
| **Adaptation**           | None          | None         | Good       | Excellent           | Good             | None           |
| **Direct Valve Benefit** | Low           | Low          | Medium     | **High**            | **High**         | Medium         |
| **Status**               | Stable        | Stable       | Stable     | **Tested & Stable** | **Beta**         | Stable         |
| **Best For**             | Simple setups | Fast heating | Most users | Optimization        | Variable systems | Simple control |

**Notes:**

- "Direct Valve Benefit" indicates how much the algorithm benefits from direct valve control (see [Direct Valve Control](#direct-valve-control) section below)
- **MPC Predictive** is stable and production-ready
- **PID Controller** is in beta and may require further algorithm fine-tuning

## Advanced: How the Algorithms Work Together with Calibration Types

The **Calibration Mode** (algorithm) works together with the **Calibration Type**:

- **Target Temperature Based:** The algorithm calculates what target temperature to send to the TRV. For example, if you want 20°C but the room is cold, it might send 22°C to the TRV to make it heat more.

- **Offset Based:** The algorithm calculates what temperature offset to send to the TRV. For example, if the TRV's internal sensor reads 21°C but your external sensor reads 20°C, it sends an offset of -1°C.

Not all TRVs support offset-based calibration. Better Thermostat will automatically detect your TRV's capabilities and offer appropriate options.

## Direct Valve Control

Some TRV devices support **direct valve control**, where Better Thermostat can directly set the valve opening percentage (0-100%) instead of only adjusting target temperatures or offsets. This provides more precise control and is particularly beneficial with advanced algorithms.

### What is Direct Valve Control?

With direct valve control, Better Thermostat can:

- Set the exact valve opening (e.g., "open valve to 45%")
- Bypass the TRV's internal temperature control logic
- Achieve more precise and responsive heating control
- Better implement advanced algorithms like MPC and PID

### Which Devices Support It?

Direct valve control is available for TRVs that expose valve position as a controllable entity, including:

- **Sonoff TRVZB** (via Zigbee2MQTT or ZHA)
- **TRVs exposed via MQTT** with valve position entities
- **Other Zigbee TRVs** that expose valve control through their integration

Better Thermostat automatically detects if your TRV supports direct valve control.

### How Algorithms Use Direct Valve Control

When direct valve control is available:

- **MPC Predictive**: Calculates optimal valve opening based on predicted temperature changes. This is where direct valve control shines - the algorithm can precisely control heating power.

- **PID Controller**: Directly outputs valve position based on temperature error and trends. Very effective with direct valve control.

- **TPI Controller**: Sets valve opening based on heating duty cycle calculations.

- **Time Based, Normal, Aggressive**: These algorithms will still work but convert their output to valve positions when direct control is available.

### Without Direct Valve Control

If your TRV doesn't support direct valve control, Better Thermostat uses **setpoint manipulation**:

- Adjusts the target temperature sent to the TRV
- Or adjusts the temperature offset (if supported)
- The TRV's internal controller then adjusts the valve based on its own logic

This still works well but gives the TRV's internal algorithm more influence over the final valve position.

### Checking If You Have Direct Valve Control

1. Go to your Better Thermostat device in Home Assistant
2. Check the device attributes for entries like:
   - `valve_position_entity`
   - `valve_position_writable`
3. If these are present and `valve_position_writable` is `true`, you have direct valve control

For MQTT/Zigbee2MQTT users, you can also check if your TRV exposes entities like:

- `number.your_trv_valve_position`
- `number.your_trv_valve_opening_degree`

### Benefits of Direct Valve Control

✅ **More precise control** - Algorithms can set exact heating power  
✅ **Faster response** - No waiting for TRV's internal logic  
✅ **Better learning** - Algorithms can better understand room behavior  
✅ **Reduced overshooting** - Finer control over heating intensity  
✅ **Algorithm effectiveness** - MPC and PID work best with direct control

### Recommendation

If you're purchasing new TRVs and want the best performance from Better Thermostat's advanced algorithms (especially MPC Predictive or PID Controller), consider devices that support direct valve control through Zigbee2MQTT or similar integrations.

## Tips for Best Results

1. **Give it time:** Algorithms with learning need time to learn your room:

   - **MPC Predictive**: 1 day for fine-tuned performance
   - **Time Based**: 2-3 days for optimal performance
   - **PID Controller**: 1-2 weeks for auto-tuning to settle

2. **Stable placement:** Keep your external temperature sensor in a consistent location away from heat sources, drafts, and direct sunlight.

3. **Start with defaults:** Try Time Based first. Only change if you have specific issues.

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
- **Time Based**: Wait 2-3 days for learning
- **PID Controller**: Wait 1-2 weeks for auto-tuning to settle
- Check sensor placement and accuracy
- Verify TRV is working correctly
- Try a different algorithm

## Technical Details

For developers and advanced users who want to understand the implementation details, see:

- [Hydraulic Balance Design Document](/hydraulic_balance_design) - Deep technical documentation
- Source code in `custom_components/better_thermostat/utils/calibration/` directory

## Need More Help?

If you're still unsure which algorithm to use or experiencing issues:

1. Check the [Q&A section](../Q&A/qanda) for common questions
2. Visit the [GitHub Discussions](https://github.com/KartoffelToby/better_thermostat/discussions)
3. Report bugs on [GitHub Issues](https://github.com/KartoffelToby/better_thermostat/issues)
