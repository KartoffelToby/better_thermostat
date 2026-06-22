# Benchmark Design & Rationale

This document explains *how* the calibration benchmark is designed and
*why* — the reasoning behind the simulation approach, the scenario
library, the metrics, and the scoring. For *running* the benchmark and
reading its output tables, see [`README.md`](README.md); this document is
the "why" behind those numbers.

The numbered sections are referenced directly from the source
(`metrics.py`, `scoring.py`, `actuator.py`, `sensor.py`).

---

## §1 — What question the benchmark answers

A single one:

> Which Better Thermostat calibration mode keeps the room closest to the
> setpoint — under which conditions, and at what cost in actuator wear and
> energy?

Better Thermostat ships several calibration modes (`mpc`, `pid`, `tpi`,
`heating_power`, the offset family `default` / `aggressive` /
`no_calibration`). They cannot be compared on real hardware on equal
footing: every house, TRV, and week of weather is different, and a single
field test confounds the controller with its environment. The benchmark
removes that confound by putting every controller in the *same* simulated
houses, through the *same* scripted situations, and scoring them by the
*same* criteria.

It is a **comparison and regression tool**, not a deployment validation.
The absolute scores are simulation artefacts; the *relative ranking* and
the *per-dimension breakdown* are what carry meaning.

---

## §2 — Design philosophy

### Pure simulation, fully deterministic

No Home Assistant runtime, no hardware, no recorded data. Every result is
a deterministic function of *code + seeds*. The same commit always
produces the same matrix. This is what makes the benchmark usable as a
regression gate: a number that moves is a number that a code change moved,
not weather noise.

All randomness (sensor noise, sample jitter, stochastic windows,
synthetic weather) flows from explicit seeds. The sensor RNG is seeded
per scenario (derived from the scenario name via a stable CRC, not
Python's salted `hash`) so noise realisations are decorrelated across
scenarios yet reproducible run to run.

### Functional decomposition: plant / sensor / actuator / controller

The simulator separates the things a real controller cannot see from the
things it acts on, mirroring the physical signal chain:

```
            commanded valve %                    measured °C
controller ───────────────► actuator ──► PLANT ──► sensor ──► controller
 (adapter)                  (flow curve)  (RC2/3)   (lag,        (next step)
                                                     noise,
                                                     dropout)
```

* **Plant** (`plant.py`) — the room's thermal truth (an RC2/RC3 model,
  §8). It owns `T_room`, `T_rad`, optionally `T_wall`. The controller
  never reads this directly.
* **Sensor** (`sensor.py`) — converts the plant's true temperature into
  what the controller actually *sees*: sampled, lagged, optionally noisy,
  biased, drifting, or dropped out. This is where "garbage in" enters.
* **Actuator** (`actuator.py`) — converts the controller's commanded
  valve-% into the *delivered* flow the plant receives (a non-linear flow
  curve, deadband, stiction, quantisation). This is where "intent ≠
  action" enters.
* **Controller adapter** (`adapters/`) — the unit under test. Wherever
  possible it calls the **real production code**: `mpc_adapter`,
  `pid_adapter`, `tpi_adapter` import and invoke `compute_mpc` /
  `compute_pid` / `compute_tpi` unchanged. The adapter only translates
  between the benchmark's `BenchmarkContext`/`BenchmarkOutput` and the
  controller's native interface; it adds no control logic of its own.

This separation is the core design decision. It lets one controller face
a sensor fault and an actuator fault as *independent* perturbations, and
it keeps the comparison honest: every controller sees the identical plant,
sensor, and actuator.

> Fidelity caveat: the `heating_power` adapter and the offset-family
> adapters re-implement their logic in pure Python because the production
> versions are bolted onto the HA entity and cannot be called headless. A
> drift-guard test (`tests/test_production_drift.py`) pins the
> `heating_power` formula to its production source so the re-implementation
> cannot silently diverge.

---

## §3 — Why oracle-normalisation

A raw metric is uninterpretable. Is "0.3 K overshoot" good? It depends
entirely on the house and the disturbance — 0.3 K on a fast small room in
a gentle step is poor; 0.3 K on a leaky house during a −15 K cold snap is
excellent. Comparing raw metrics across scenarios is meaningless.

So every scenario is *also* run by an **`IdealOracle`** — a controller
that knows the plant's exact parameters and inverts them to deliver the
physically best-achievable response. Its metrics define the per-scenario
reference:

* Oracle's value → score **1.0** (the asymptote of perfect knowledge)
* A controller at the **failure threshold** (§6) → score **0.0**
* Linear interpolation in between, clamped to `[0, 1]`

This makes scores comparable *across* scenarios and houses: 0.85 always
means "85 % of the way from catastrophic to physically perfect, in this
specific situation". It also means **no real controller can score 1.0** —
the oracle cheats by construction. Read the oracle as a ceiling, not a
target (it sits near 0.96 overall because some scenarios have permanent
disturbances where settling-to-setpoint is impossible).

---

## §4 — The five user-priority dimensions

The benchmark frames "good control" as five dimensions a user actually
cares about. Four are measured per run; the fifth is suite-level.

1. **Comfort** — how close the room stays to the setpoint. The headline
   axis; everything else is a cost paid to achieve it.
2. **Actuator longevity** — valve wear and TRV battery drain. A
   controller that tracks perfectly by thrashing the valve is a bad trade
   on battery-powered Zigbee hardware.
3. **Energy** — heat actually spent. Both over-heating (waste) and
   under-heating (missed comfort) are deviations from the optimum.
4. **Resilience** — behaviour under faults (sensor dropout, large
   disturbances). Not a separate per-run score: it surfaces as
   catastrophic comfort/actuator values on the fault scenarios (§7-E/F),
   captured by averaging across the library.
5. **Adaptation** — learning quality across runs (e.g. `heating_power`'s
   EMA, MPC's online estimates). This is a *suite-level* property, not a
   single-run metric, so it is not in the score matrix; it shows up
   indirectly in the cold-start and restart scenarios (§7-H).

The score matrix reports the first three as explicit columns; resilience
and adaptation are emergent across scenarios. This is why the per-scenario
σ (stdev across scenarios) matters as much as the mean — a controller with
a great mean but a high comfort σ is fragile on a subset of conditions,
which is exactly a resilience signal.

---

## §5 — Metrics: what is measured and why

All metrics are computed from a `TimeSeries` (`metrics.py`) of
`(t, T_room, T_setpoint, valve_pct)`. Per dimension:

### Comfort

* **max_overshoot_K / max_undershoot_K** — peak excursion past the
  setpoint, measured only after `transient_start_s` (so the initial rise
  isn't counted as overshoot). Captures the worst-case the user feels.
* **settling_time_min** — minutes until `|T − setpoint| < 0.2 K` and
  *stays* there for ≥ 10 continuous minutes. The dwell requirement
  prevents a controller from "settling" on a single drive-by sample.
  Returns ∞ if it never settles — a deliberate resilience signal, not a
  number to average away.
* **steady_state_error_K** — mean absolute error over the final 30 min;
  the offset a user would read on the thermostat once things calm down.
* **rmse_tracking_K** — RMS error across the whole transient;
  complements settling (which is a single instant) with an integral view.

### Actuator longevity

* **total_valve_travel_pct** = Σ|Δu| over the run. This — not cycle count
  — is the headline wear/battery proxy. A precise controller (the oracle
  included) makes *many small* moves; counting "cycles" would punish it
  for being smooth. Summed absolute travel is the honest measure: every
  millimetre of motor movement costs the same battery regardless of
  whether it reverses direction.
* **valve_cycle_count** — direction reversals (with a 0.5 % deadband to
  ignore micro-noise). Reported as diagnostics, *not* scored.

### Energy

* **integral_valve_pct_min** = ∫ valve% dt. A proxy for delivered heat.
  Scored *symmetrically* around the oracle's optimum (§6): under-heating
  (missed setpoint) costs the same as over-heating (waste). This is a
  deliberate neutrality — the benchmark does not assume the user prefers
  saving energy over comfort. If your application is asymmetric, read the
  energy column alongside comfort rather than the weighted overall.

### Diagnostics (measured, not scored)

* **time_above/below_setpoint_K_h** — asymmetric comfort accounting in
  K·h (the BOPTEST `tdis_tot` split), for analyses where overshoot and
  undershoot have different cost.
* **valve_sweet_spot_residency_pct** — fraction of time the valve sits at
  40–60 %. Heat-pump COP suffers at the extremes; mid-range modulation is
  efficient. Surfaced for heat-pump scenarios (§7-G), not folded into the
  score.

---

## §6 — Scoring: thresholds and profiles

Each dimension maps its metrics to `[0, 1]` against the oracle, with
**failure thresholds** chosen so the scale is interpretable rather than
arbitrary:

* **Comfort** = 1 − (0.4·overshoot-excess + 0.4·settling-ratio +
  0.2·ss-error-excess). Failure at +1 K extra overshoot, 5× the oracle's
  settling time, or +0.5 K extra steady-state error. (Overshoot and
  settling are weighted equally because a user notices a temperature
  swing and a slow approach about equally; steady-state error is weighted
  less because it is usually small once settled.)
* **Actuator** = 1 − (excess travel / 4× oracle travel). Failure at 5×
  the oracle's total travel. When the oracle barely moves, the candidate
  is scored against an absolute floor so a low-activity controller still
  scores near 1.0.
* **Energy** = 1 − |ratio − 1|, symmetric. Failure at 2× the oracle's
  integral (over-heating) or 0 (room never heated). When the oracle's
  integral is below a small floor (the scenario barely heated), the
  candidate's *excess* usage is scored against that floor — so a grossly
  over-heating controller cannot escape via the edge case.

The **overall** column is a weighted sum under a `UserProfile`. Four
profiles bracket reasonable priorities — `balanced` (0.5 / 0.3 / 0.2),
`comfort_first`, `longevity_first`, `energy_first` — and the ranking can
flip between them. The profiles exist precisely because there is no single
correct weighting; the honest answer to "which controller is best" is
"for which priority?". Always read the dimension columns to see *why* a
controller wins, not just the overall.

---

## §7 — Scenario taxonomy: what is covered and why

The 37 scenarios are not arbitrary. Each probes a specific stress that a
real Better Thermostat install encounters. They group into families;
within a family, scenarios vary magnitude or plant to expose where a
controller breaks.

### A — Setpoint dynamics (tracking user commands)

| Scenario | Probes |
|---|---|
| `S01_setpoint_step_small` (20→21) | The canonical small step — baseline tracking. |
| `S02_setpoint_step_large` (19→23) | Large step — saturation, overshoot discipline. |
| `S03_frost_to_comfort` (12→21, −2 °C out) | Cold-home recovery: big error + heavy loss. |
| `S04_setpoint_drop` (22→19) | Downward step — must coast down without re-heating. |
| `S06_setpoint_during_heating` (20→22→21) | Mid-transient re-planning. |

*Why:* setpoint changes are the most frequent real event; this family is
the core tracking test (rise time, overshoot, re-planning).

### B — Outdoor / load disturbances (rejecting uncommanded change)

| Scenario | Probes |
|---|---|
| `S07_outdoor_step_cold` (5→−10, hold 21) | Sudden cold snap — disturbance rejection. |
| `S08_outdoor_ramp_warm` (5→12 over 6 h) | Slow load change — drift, integral action. |
| `S22_diurnal_outdoor` (24 h cycle) | Continuous disturbance — never settles. |

*Why:* heat loss is never constant; the controller must hold setpoint
against a moving load.

### C — Fast transient disturbances (the window problem)

| Scenario | Probes |
|---|---|
| `S09_window_open_short` (5 min) | Brief open — should not crank the valve open into the cold. |
| `S10_window_open_long` (20 min) | Sustained open — energy waste + recovery overshoot. |
| `S21_stochastic_windows` (3 random, Annex-79 style) | Realistic irregular venting. |

*Why:* open windows are the classic TRV failure mode (the valve opens
fully against the cold air); a key thing BT's window handling must get
right.

### D — Solar gain & forecast (anticipation)

| Scenario | Probes |
|---|---|
| `S11_solar_gain_morning` (trapezoid) | Free heat — should back off the valve. |
| `S32_forecast_mismatch_solar` (sees 1.0, gets 0.0) | Robustness to a *wrong* forecast. |

*Why:* solar gain is significant free heat that an anticipating
controller (MPC) exploits; the mismatch tests that anticipation degrades
gracefully when the forecast is wrong.

### E — Sensor faults (garbage-in resilience)

| Scenario | Probes |
|---|---|
| `S12_sensor_dropout` (10 min none) | Holds sane output through a reporting gap. |
| `S17_sensor_bias` (+0.5 K) | Constant offset — steady-state error it cannot see. |
| `S18_sensor_drift` (+0.05 K/h) | Slow drift — moving blind spot. |
| `S35_sample_jitter` (±45 s) | Irregular Zigbee report timing. |

*Why:* real Zigbee temperature sensors drop out, drift, and report
irregularly. This family is the resilience (§4) backbone.

### F — Actuator faults (intent ≠ action)

| Scenario | Probes |
|---|---|
| `S19_valve_stiction` (5 % stick-slip) | Hunting / limit-cycling on a sticky valve. |
| `S20_valve_deadband` (3 % deadband) | Small steps swallowed by the deadband. |
| `S31_boiler_cycle_stress` (20 % deadband + wobble) | Boiler short-cycling under coarse actuation. |

*Why:* cheap TRV valves stick and have deadbands; a controller that
ignores this hunts and wears the motor.

### G — Plant & HVAC topology (generalising beyond a radiator)

| Scenario | Probes |
|---|---|
| `S05_slow_radiator` (underfloor) | A slow plant — controllers tuned for fast radiators. |
| `S33_ufh_asymmetric` | Underfloor with asymmetric over/undershoot cost. |
| `S23_boiler_limited` (42 °C supply HP) | Low supply temperature — limited authority. |
| `S29_heatpump_steady_state` | Valve sweet-spot residency for heat-pump COP. |
| `S26_cooling_mode` (reverse-acting) | Chilled-water / cooling — sign-flipped control. |
| `S27_pipe_fill_after_idle` (120 s delay) | Boiler→radiator transport lag from cold. |

*Why:* BT runs on radiators, underfloor, heat pumps, and in cooling mode;
a controller must not assume a hot, fast, instantly-responding radiator.

### H — Lifecycle & operational (real-world operation)

| Scenario | Probes |
|---|---|
| `S13_cold_start` (no prior state) | First-run behaviour with nothing learned. |
| `S24_controller_restart` (reset at 2 h) | HA-restart state rehydration mid-run. |
| `S36_user_override` (API every 8 min) | High command throughput — must not drop updates. |
| `S25_demand_response` (pre-heat / setback) | Grid signals shifting the setpoint. |

*Why:* HA restarts, users fiddle, and grids signal; this family tests
state handling and command throughput — and indirectly adaptation (§4).

### I — Schedules & multi-day (long-horizon realism)

| Scenario | Probes |
|---|---|
| `S14_nightly_setback` | Setback → wake-up → setback. |
| `S15_daily_cycle` (24 h) | Morning / away / evening / night profile. |
| `S16_vacation` (6 d frost-protect) | Long idle then return. |
| `S30_frost_drift_recovery` (7 d frost, recovery scored) | Recovery from deep setback. |

*Why:* real thermostats run setback schedules; behaviour across day/night
and after long idle is part of everyday use.

### J — Realistic weather weeks (integration / face validity)

| Scenario | Probes |
|---|---|
| `S37_winter_week_humid_continental` (Chicago-like AR(1)) | A full week of synthetic-but-realistic weather. |
| `S38_winter_week_semi_arid` (Denver-like) | A drier, larger-swing climate. |

*Why:* an end-to-end integration test that exercises every subsystem
together over a realistic horizon, as a face-validity check on the
single-effect scenarios.

### K — Indirect-TRV native

| Scenario | Probes |
|---|---|
| `S28_indirect_trv_tado` | Offset-mode TRV behaviour as a first-class scenario (others apply the indirect-TRV wrapper instead). |

### Deliberate omissions

The library intentionally does **not** model: multi-zone thermal coupling
between rooms, occupancy/presence schedules beyond the diurnal profile,
photovoltaic-driven setpoint shifts, HVAC topologies beyond
radiator + boiler / heat pump, and cross-session learning state beyond the
single restart scenario. These are plant-architecture extensions, not
missing controller coverage — appropriate follow-up work, called out so
the absence reads as a choice, not an oversight.

---

## §8 — Modelling basis (plant, sensor, actuator)

The physical models are deliberately simple — lumped-parameter, not CFD —
because the goal is *relative* controller comparison, not absolute
building simulation. The modelling choices and the references already
carried in the source:

### Plant (`plant.py`)

A lumped-RC thermal network, integrated with explicit Euler (stable for
`tau_rad ≥ 5 min` and steps ≤ 60 s; the runner uses 30 s).

* **RC2** (room + radiator): heat flows radiator → room → outdoor.
* **RC3** (adds a wall mass): heat flows room → wall → outdoor, giving the
  room a slow "thermal coast" after the heater stops — closer to real
  buildings (Bacher & Madsen 2011; ISO 52016-1).
* **Pipe-transport delay**: an optional dead-time on the valve command
  models the boiler→radiator transport lag (≈ 30 s – 3 min).

13 plant profiles span fast small rooms to heavy slow buildings, plus
DOE-envelope and realistic (fitted) profiles, so a controller is never
judged on a single house.

### Sensor (`sensor.py`)

Two time-domain effects plus fault injection:

* **Thermal lag** — a first-order filter (time constant 30 s – 3 min): the
  sensor body has its own thermal mass and approaches the air temperature
  gradually. This is the dominant physical lag in residential sensors.
* **Sampling + EMA** — sensors only emit every `sample_interval_s`
  (1–5 min for Zigbee), optionally smoothed by an EMA matching BT's own
  periodic filter.
* **Faults** — noise, bias, drift, dropout, sample jitter (the §7-E
  family), all deterministic from the per-scenario seed.

### Actuator (`actuator.py`)

The commanded valve-% is mapped to delivered flow through one of four
profiles. `EQUAL_PERCENTAGE` (`flow = (pct/100)^α`, α ≈ 3) is the most
realistic for typical TRV hardware — a given fractional change in valve
position produces the same fractional change in flow, giving the
controller a roughly constant loop gain (Karlsson 1980). `LINEAR` is the
simple reference default. Deadband, hysteresis (stiction), and
quantisation model real valve imperfections (the §7-F family).

---

## §9 — Interpreting a result

Read the matrix top-down with three anchors (full tables in
[`README.md`](README.md#how-to-read-the-results)):

* **Oracle ≈ 0.96** — the ceiling. Not 1.0 because some scenarios have
  unsettleable permanent disturbances.
* **BangBang ≈ 0.60** — the noise floor. A naive on/off controller;
  anything near it has a real problem.
* **Production controllers ≈ 0.70–0.85** — the realistic band.

Then read *across* the dimension columns, not just `overall`:

* High comfort + low actuator → the controller tracks by thrashing the
  valve (good room temperature, bad battery life).
* High mean + high σ on a dimension → fragile on a subset of scenarios
  (a resilience signal, §4); use `--per-scenario` to find which.
* The ranking flips between profiles → the controllers trade comfort
  against wear/energy differently; pick the profile that matches the
  deployment.

---

## §10 — What not to conclude

* **It is a simulation.** The room is a 2–3 state RC model; real buildings
  have distributed mass, infiltration, and multi-zone coupling. Trust the
  *relative ranking* far more than the absolute scores.
* **The oracle cheats.** It is a definition of "achievable with perfect
  knowledge", not a deployable controller. It is an asymptote.
* **Scoring is opinionated.** A controller can win `balanced` and lose
  `longevity_first`. There is no profile-free "best".
* **Energy is symmetric.** Under- and over-heating cost the same here; if
  you optimise specifically for savings, read the columns separately.
* **Not a deployment validation.** The benchmark spots regressions,
  compares controllers on equal footing, and localises a weakness to a
  dimension. Confirming real-world quality still needs hardware field
  testing.
