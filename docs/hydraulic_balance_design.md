# Decentralized Hydraulic Balance in Better Thermostat

This document captures the design and reasoning behind the decentralized hydraulic balance feature.

## Problem
We want to emulate a hydraulic balance across multiple TRVs without any global supply signals or direct valve feedback. Most devices only accept a temperature setpoint. Some devices (e.g., Sonoff TRVZB) expose min/max opening as percentages.

## Approach
- Per room/thermostat, use:
  - ΔT = setpoint - current temperature
  - Temperature trend (slope dT/dt over ~5–10 minutes)
  - Window state (to suppress heating)
- Compute a desired valve opening percentage (0–100). If the device cannot accept a valve percentage, derive an equivalent setpoint reduction (flow_cap_K) so that the effective target becomes target - flow_cap.
- For Sonoff TRVZB provide recommendations for min_open% and max_open% caps.

## Glossary and Symbols
- TRV: Thermostatic Radiator Valve
- ΔT: Temperature difference = setpoint − current temperature (Kelvin)
- slope: Temperature trend dT/dt in K/min (positive = rising, negative = falling)
- valve_percent p: Desired valve opening 0–100%
- flow_cap_K c: Setpoint reduction in Kelvin applied to emulate throttling on setpoint-only devices
- band_near, band_far: Inner/outer ΔT bands controlling fine vs. full action

## Per-room contract (inputs/outputs)
Inputs (available today in BT):
- current_temp_C (sensor)
- target_temp_C (BT target)
- window_open (binary)
- temp_slope_K_per_min (computed from recent readings, 5–10 min window)

Outputs (consumed by controlling/adapters):
- valve_percent (0–100) for devices with valve control
- setpoint_eff_C = target_temp_C − flow_cap_K for setpoint-only devices
- flow_cap_K (0..cap_max_K) for telemetry/tuning
- sonoff_min_open_pct, sonoff_max_open_pct for Sonoff TRVZB

State (lightweight, per room key):
- ema_slope (smoothed slope)
- last_percent (smoothed valve percentage)
- last_update_ts (rate limiting)

## Algorithm (summary)
- If ΔT >= band_far → 100% (fully open)
- If ΔT <= -band_far → 0% (close/hold)
- Else map ΔT linearly to 0..100% and correct by slope:
  - positive slope → reduce opening (prevent overshoot)
  - negative slope (while ΔT>0) → ensure at least a minimum opening
- Smooth via EMA, apply hysteresis and a minimum update interval to save battery and reduce traffic.
- flow_cap_K = cap_max_K * (1 - valve_percent/100)
- For Sonoff: sonoff_max_open_pct = valve_percent; sonoff_min_open_pct ≈ 0–5% depending on overshoot.

## Algorithm (details)
1) Base mapping from ΔT to p (0..100):
  - Clamp ΔT to [−band_far, +band_far]
  - Linear map: 0% at −band_far, 100% at +band_far
  - Formula: $p_{base} = 100 \cdot \frac{\Delta T + band\_far}{2\, band\_far}$

2) Slope smoothing (EMA):
  - $s_{ema} \leftarrow 0.6\, s_{ema} + 0.4\, s$ (on new slope s)

3) Slope correction on p:
  - $p_{adj} = p_{base} + G \cdot s_{ema}$ with $G<0$ (positive slope reduces opening)

4) Near-setpoint refinement (|ΔT| ≤ band_near):
  - If $s_{ema} ≥ s\_{up}$: $p_{adj} \leftarrow p_{adj} \cdot 0.7$ (throttle)
  - If $s_{ema} ≤ s\_{down}$ and ΔT>0: $p_{adj} \leftarrow \max(p_{adj}, 60)$ (ensure opening)

5) Full-demand/overshoot guards:
  - If ΔT ≥ band_far → p = 100
  - If ΔT ≤ −band_far → p = 0
  - Else p = clamp(p_adj, 0..100)

6) Percentage smoothing (EMA) + hysteresis + rate limit:
  - $p_{smooth} = (1-\alpha)\, p_{last} + \alpha\, p$
  - Apply only if |p_smooth − p_last| ≥ hysteresis and Δt ≥ min_update_interval

7) Setpoint throttling (generic devices):
  - $c = cap\_max \cdot (1 - p/100)$
  - $setpoint\_{eff} = setpoint - c$ (only when ΔT ≥ 0; on overshoot, reduction allowed as well)

8) Sonoff min/max recommendations:
  - $max\_open \%= p$
  - $min\_open \% \approx 0$ on overshoot (ΔT ≤ −band_near), else a small comfort value (e.g., 5%)

All steps are per-room and require no global information.

## Implementation
- balance.py: pure computation, no side effects.
- events/temperature.py: compute and store a smoothed slope (K/min).
- events/trv.py: integrate balance for setpoint-based devices; store debug info per TRV under `real_trvs[trv]['balance']`. Enabled by selecting calibration mode "Hydraulic Balance".
- utils/controlling.py: if a valve entity exists (e.g., MQTT/Z2M), call `set_valve` with the computed percentage (with hysteresis).

### Code integration points
- `balance.py`: pure math and simple per-room in-memory state
- `events/temperature.py`: computes `temp_slope` as a smoothed K/min trend
- `events/trv.py`: calls `compute_balance(...)` when calibration mode is set to "Hydraulic Balance"; applies `setpoint_eff` and records debug under `real_trvs[trv]['balance']`
- `utils/controlling.py`: if `valve_position_entity` exists for a TRV, sends `set_valve(percent)` with a 3% hysteresis
- `adapters/mqtt.py`: tries to discover a `valve_position_entity` to enable `set_valve`

## Configuration
- New calibration mode per TRV: `hydraulic_balance`.
- Future tuning parameters (cap_max, bands, hysteresis) can be exposed if needed.

Suggested defaults (conservative):
- band_near_K = 0.3
- band_far_K = 0.5
- cap_max_K = 0.8
- slope_up_K_per_min = +0.02
- slope_down_K_per_min = −0.01
- slope_gain_per_K_per_min = −1000.0 (e.g., +0.02 K/min → −20 pp)
- percent_smoothing_alpha = 0.3
- percent_hysteresis_pts = 3.0
- min_update_interval_s = 120

## Notes
- Works with existing BT signals only (current temperature, setpoint, window state). No valve feedback or global supply info is required.
- Emergent behavior: the weakest room tends to run with valve_percent ~ 100%, others get throttled near their setpoint.
- For Sonoff TRVZB, we will extend adapters to write min/max opening if available via the underlying integration.

## Next steps
- Expose "Hydraulic Balance" in the calibration mode dropdown.
- Extend adapters to detect and write Sonoff min/max opening endpoints where available.
- Optional: expose balance parameters in the UI.

## Edge cases and mitigations
- Very slow systems (e.g., floor heating): increase band_near, reduce slope_up (less aggressive throttling), and/or lower slope_gain magnitude.
- Many rooms heat simultaneously: local logic still works; emergent behavior limits strong rooms near setpoint. If desired later, add a mild global coordinator.
- Noisy sensors: EMA smoothing on slope and hysteresis on outputs reduce flapping.
- Battery/traffic: min update interval and percent hysteresis avoid frequent writes.
- Devices without OFF mode: balance only affects setpoint; existing “min temp as OFF” safeguards remain.
- Window open: heating is suppressed; balance logic is skipped.

## Telemetry and debugging
- Climate attributes:
  - `temp_slope_K_min`: current smoothed slope in K/min
  - `balance` (JSON per TRV): `{ "valve%": p, "flow_capK": c }`
- Per-TRV stored debug under `real_trvs[trv]['balance']` includes Sonoff min/max.

## Testing strategy
1) Unit-like tests in a dev instance:
  - Synthetic temperature ramps verifying percent and setpoint_eff reactions.
  - Overshoot scenarios: ensure throttling near setpoint.
2) Integration tests with MQTT/Z2M devices:
  - Detect `valve_position_entity` and validate `set_valve` hysteresis.
3) Field validation:
  - Observe average |ΔT| and overshoot frequency over multiple days.
  - Check frequency of setpoint/valve writes to confirm low traffic.

## Limitations
- Without real flow/pressure feedback, percent ↔ delivered power is heuristic.
- Interactions with TRV internal PID vary by model; throttling is designed to act mainly near setpoint to minimize conflicts.

## Future enhancements
- Expose tuning parameters in Advanced options per TRV.
- Adapter support for Sonoff TRVZB min/max open writing (Z2M/ZHA specifics).
- Optional lightweight global coordinator (e.g., “weakest wins” raising caps, soft budgets).
- Diagnostics panel showing trends and balance actions over time.

## FAQ
Q: Does this replace mechanical hydraulic balancing?
A: No. It emulates a dynamic throttling near setpoint to reduce overshoot and undue dominance by strong rooms. It improves comfort and potentially efficiency, but does not replace proper mechanical balancing.

Q: What happens when I change the boiler supply temperature?
A: The measured slopes decrease; the algorithm gradually reduces flow_cap (i.e., increases effective opening) until stability near setpoint is restored.

Q: Do I need valve feedback?
A: No. It’s optional. With valve feedback (via MQTT/Z2M), the algorithm can set a valve percentage; otherwise it uses setpoint throttling.

## Maintenance
This document is a living specification. Whenever we modify the hydraulic balance logic, configuration, adapters, or telemetry, update this file in the same change. Treat it as the single source of truth for the feature’s behavior, parameters, and integration points.
