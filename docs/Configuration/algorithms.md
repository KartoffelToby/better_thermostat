---
layout: default
title: Calibration Algorithms
parent: Configuration
nav_order: 2
---

# Calibration Algorithms

Better Thermostat offers several calibration algorithms (also called "Calibration Modes") that control how your TRV (Thermostatic Radiator Valve) is adjusted to maintain your desired temperature. Each algorithm has different characteristics and is suited for different situations.

## Choosing the Right Algorithm

If you're unsure which algorithm to use, here's a quick guide:

- **Just starting out?** Try **AI Time Based** (default) - it works well for most situations
- **Room heats too slowly?** Try **Aggressive**
- **Temperature overshoots often?** Try **MPC Predictive**
- **Have technical knowledge and want fine control?** Try **PID Controller**
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

### AI Time Based

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

**When to use:** This is the recommended default for most users. Give it a week to learn your room's behavior for best results.

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
- Requires a learning period (1-2 weeks for best results)
- May seem slow to react initially (by design)

**When to use:** 
- You experience frequent temperature overshoots
- Energy efficiency is a priority
- You want the most sophisticated control
- Your heating system is relatively stable

**Note:** MPC works best when given time to learn. Initial behavior may seem conservative, but it improves significantly after learning your room's thermal characteristics.

---

### PID Controller

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

| Feature | Normal | Aggressive | AI Time Based | MPC Predictive | PID Controller | TPI Controller |
|---------|--------|------------|---------------|----------------|----------------|----------------|
| **Complexity** | Low | Low | Medium | High | Medium | Low |
| **Learning** | No | No | Yes | Yes | Yes | No |
| **Overshoot Prevention** | Basic | Poor | Good | Excellent | Good | Good |
| **Energy Efficiency** | Medium | Low | High | Very High | High | Medium |
| **Response Speed** | Medium | Fast | Medium | Measured | Fast | Medium |
| **Adaptation** | None | None | Good | Excellent | Good | None |
| **Best For** | Simple setups | Fast heating | Most users | Optimization | Variable systems | Simple control |

## Advanced: How the Algorithms Work Together with Calibration Types

The **Calibration Mode** (algorithm) works together with the **Calibration Type**:

- **Target Temperature Based:** The algorithm calculates what target temperature to send to the TRV. For example, if you want 20°C but the room is cold, it might send 22°C to the TRV to make it heat more.

- **Offset Based:** The algorithm calculates what temperature offset to send to the TRV. For example, if the TRV's internal sensor reads 21°C but your external sensor reads 20°C, it sends an offset of -1°C.

Not all TRVs support offset-based calibration. Better Thermostat will automatically detect your TRV's capabilities and offer appropriate options.

## Tips for Best Results

1. **Give it time:** Algorithms with learning (AI Time Based, MPC Predictive, PID Controller) need time to learn your room. Allow 1-2 weeks for optimal performance.

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
- Wait 1-2 weeks if using a learning algorithm
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
