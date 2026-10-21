# Calibration Controller Benchmark

Pure-simulation comparison framework for Better Thermostat's calibration
controllers (`mpc_v2`, `mpc`, `pid`, `tpi`) against reproducible thermal-dynamics
scenarios. No Home Assistant runtime, no hardware, no external data —
every result is a deterministic function of code + seeds.

This README covers *running* the benchmark and *reading* its output. For
*why* it is designed this way — the simulation approach, the oracle
normalisation, the scenario taxonomy, and the metric/scoring rationale —
see [`DESIGN.md`](DESIGN.md).

## Quick start

```sh
uv run python -m tests.benchmark.runner
```

prints a controller × dimension score matrix:

```text
============================================================================================
Score matrix — user profile: balanced (w_c=0.5, w_a=0.3, w_e=0.2)
Scores are 0..1, oracle-normalised; 1.0 = oracle-equivalent.
σ = population stdev across scenarios (lower = steadier per dimension).
============================================================================================
  controller          overall      σ  comfort      σ  actuator      σ   energy      σ    n
 *ideal_oracle          1.000  0.000    1.000  0.000     1.000  0.000    1.000  0.000   36
  mpc_v2                0.853  0.098    0.965  0.039     0.577  0.313    0.988  0.026   36
  tpi                   0.750  0.173    0.735  0.227     0.626  0.340    0.971  0.019   36
  pid                   0.747  0.167    0.829  0.193     0.453  0.363    0.983  0.024   36
  mpc                   0.697  0.148    0.837  0.182     0.277  0.298    0.977  0.025   36
  bangbang              0.598  0.170    0.678  0.230     0.216  0.303    0.971  0.044   36
```

Higher is better. Each dimension is **oracle-normalised**: `1.0` matches
a hypothetical perfect-knowledge controller on the same scenario, `0.0`
is catastrophic. The `overall` column is a weighted sum under the
selected user profile.

Each metric column is followed by its **σ** — the population stdev of
that dimension across scenarios. The mean alone hides bimodal behaviour
(e.g. a controller that's great on most scenarios but catastrophic on a
few). A high σ on `actuator` typically means the controller's valve
discipline is plant-dependent; a high σ on `comfort` means setpoint
tracking is scenario-dependent.

**n** is the number of scenario runs aggregated into the row. Direct
controllers see each scenario once; `*+indirect_trv` rows multiply by
the four vendor presets, so they aggregate more runs than direct rows.

## Common runs

| Command | What it shows |
|---|---|
| `uv run python -m tests.benchmark.runner` | All controllers × all scenarios (default) |
| `uv run python -m tests.benchmark.runner --controller mpc --scenario S01_setpoint_step_small` | One controller × one scenario |
| `uv run python -m tests.benchmark.runner --profile longevity_first` | Re-weight toward actuator wear |
| `uv run python -m tests.benchmark.runner --per-scenario` | Add per-scenario detail table |
| `uv run python -m tests.benchmark.runner --multi-trv` | Add multi-TRV matrices (parallel radiators sharing one room) |
| `uv run python -m tests.benchmark.runner --plant-sweep` | Realistic + DOE envelope classes, cross-plant summary |

Run `uv run python -m tests.benchmark.runner --help` for the full flag list.

## How to read the results

### What each dimension measures

**comfort** — how close the room temperature stays to the setpoint.
Internally a weighted sum: 0.4·overshoot-excess + 0.4·settling-time ratio
+ 0.2·steady-state-error excess, all measured against the Oracle on the
same scenario.

| Score | What it means in practice |
|---|---|
| 1.00 | Matches the Oracle. |
| 0.90–0.99 | Excursions and settling times within a comfort margin a user wouldn't notice. |
| 0.70–0.90 | Noticeable temperature swings or a slow setpoint follow-through. |
| 0.50–0.70 | Clear comfort violations on the harder scenarios. |
| < 0.50 | Significant deviations, often a scenario the controller never converges on. |

**actuator** — `1 − (excess valve travel) / (4 × max(Oracle travel, 125 %))`,
where travel is `Σ|Δu_pct|` over the run. The 125 % floor keeps the score
continuous on scenarios where the Oracle barely moves the valve.

| Score | What it means in practice |
|---|---|
| 1.00 | As little actuator activity as the Oracle. |
| 0.70–1.00 | Healthy modulation. |
| 0.40–0.70 | Detectable wear / battery drain on Zigbee TRVs. |
| 0.20–0.40 | High wear; expect motor lifetime and battery hits. |
| < 0.20 | Pathological cycling (BangBang territory). |

**energy** — heat the radiator delivered to the room, relative to the
Oracle's optimum, symmetric around 1.0× so under-heating (missed
setpoint) costs the same as over-heating (wasted energy). It follows the
plant, not the command: a command that deadband or stiction holds back
adds no heat over a closed valve, however often it is sent. Overshoot costs
little here, since 0.3 K above the setpoint raises the losses by only a
few per cent, so most controllers land between 0.95 and 1.00.

| Score | What it means in practice |
|---|---|
| 1.00 | Same heat input as the Oracle. |
| 0.75–1.00 | Within ~25 % of the optimum either way. |
| 0.50–0.75 | 25–50 % over- or under-heating. |
| 0.00 | Either 2× the Oracle's heat or the room missed the setpoint entirely. |

**overall** — profile-weighted sum. Defaults to `balanced`
(0.5·comfort + 0.3·actuator + 0.2·energy). Other profiles
(`comfort_first`, `longevity_first`, `energy_first`) re-weight; the
ranking can flip. Always check the per-dimension columns to see *why*
a controller wins.

### Anchor points

* **Oracle = 1.0** — the ceiling by construction. Every dimension
  normalises against the Oracle, so it scores itself 1.0 on every
  scenario, including the permanent-disturbance ones (diurnal outdoor,
  multi-day weather) where the disturbance cancels on both sides.
* **BangBang ≈ 0.60** — noise floor. A deliberately naive on/off
  controller; anything close to BangBang has a problem.
* **Production controllers (`pid`, `tpi`, `mpc`, `heating_power`) ≈
  0.67–0.75, `mpc_v2` ≈ 0.85.** That band is the realistic operating
  range. A controller above 0.75 is beating most of the field; near the
  BangBang floor means a clear weakness in at least one dimension.

Rule of thumb: when a controller scores well below the Oracle, look at
which dimension column dropped. A 0.92 comfort with 0.25 actuator means
the controller tracks the setpoint by thrashing the valve.

### Signal path

Every controller sees the room through the signal path Better Thermostat
runs in production, not through the plant's truth:

* **Room sensor** (`REALISTIC_SENSOR_PARAMS`): sampled once a minute,
  90 s thermal lag, 0.02 K noise, reported in 0.1 K steps whenever the
  value changes by a tenth. A scenario can set its own sensor.
* **Input stage** (`input_stage.py`): BT commits a reading only when it
  moves the committed value by 0.11 K, when same-direction moves add up
  to that, or when a smaller move holds for 120 s. Controllers see the
  committed value; PID and MPC v1 also get its EMA (τ = 300 s) and the
  slope of that EMA, as in production.
* **TRV sensor**: the TRV reads the room plus a tenth of the radiator's
  excess and reports in 0.1 K steps. The TRV's own regulation runs on the
  unrounded value.
* **Control cycles**: the production controllers compute only when BT
  runs a cycle, on a committed reading, a changed TRV report, a setpoint
  or window change, and on the 300 s tick (not for `heating_power`). In
  between the TRV keeps the last command, written as a whole percent.

The room sensor is the least certain part of that path. No measurement
of a real Zigbee sensor stands behind 0.02 K: it lies between the high-
and medium-repeatability modes of a Sensirion SHT3x (3σ = 0.04 / 0.08 K).
Air turbulence adds nothing a 90 s sensor body would pass on. How often
the sensor reports matters as much: Aqara documents reports on a 0.5 K
change or hourly, Sonoff's SNZB-02 defaults to about 0.2 K. Mean overall
score by noise and reporting threshold (balanced profile, 36 scenarios):

| σ | threshold | mpc_v2 | tpi | pid | mpc | heating_power | bangbang |
|---|---|---|---|---|---|---|---|
| 0.00 K | 0.1 K | 0.862 | 0.807 | 0.752 | 0.697 | 0.723 | 0.597 |
| **0.02 K** | **0.1 K** | **0.853** | **0.750** | **0.747** | **0.697** | **0.668** | **0.598** |
| 0.05 K | 0.1 K | 0.822 | 0.661 | 0.730 | 0.685 | 0.570 | 0.602 |
| 0.00 K | 0.5 K | 0.735 | 0.790 | 0.670 | 0.647 | 0.725 | 0.588 |
| 0.02 K | 0.5 K | 0.745 | 0.781 | 0.674 | 0.659 | 0.721 | 0.583 |
| 0.05 K | 0.5 K | 0.731 | 0.777 | 0.684 | 0.651 | 0.712 | 0.592 |

Bold is the default; the 0.5 K rows add an hourly report. TPI and PID
swap places around 0.02 K of noise, and a sensor that reports only every
0.5 K moves MPC v2 behind TPI. Read the ranking of TPI, PID and
`heating_power` as dependent on the sensor; MPC v2 leads as long as the
sensor reports tenths.

### Multi-TRV (`--multi-trv`)

Parallel radiators share one room state; BT's
`distribute_valve_percent` heuristic splits the single-output u across
them. Plant variants run independently — `multi-symmetric`,
`multi-asymmetric` (position-induced sensor offsets),
`multi-heterogeneous` (one TRV has a wide actuator deadband). Each
emits its own score matrix.

What it tests: does the controller's output shape *survive* the
distribution stage? Multi-TRV often inverts the single-TRV ranking —
smooth outputs (TPI, MPC) tend to distribute cleanly across the
radiators, while aggressive ones (PID, BangBang) divergence-amplify.

This is the closer-to-reality benchmark for **multi-radiator rooms**
(living rooms, larger kitchens) — common in residential setups. For
single-radiator rooms the single-TRV matrix is the relevant one.

### Indirect TRVs (`pid+indirect_trv`, `tpi+indirect_trv`, `mpc+indirect_trv`, `mpc_v2+indirect_trv`)

A wrapper that mediates the controller's valve-% intent through an
offset-mode TRV (Tado, Bosch BTH-RA, Sonoff TRVZB offset-mode, Tuya
TS0601). The TRV runs its own internal P-loop and only accepts a
quantised setpoint; the score reflects what physically reaches the room,
not what the controller intended. While a window is open, or when the
controller stands down, the wrapper closes the valve instead of mapping
a zero intent onto a setpoint, as BT turns the TRV off.

Each row aggregates the four vendor presets × all single-TRV
scenarios. The vendor parameters are heuristic operating points based
on observed user behaviour, **not calibrated truth** — read the row as
"indicative for offset-mode TRVs in general", not as "calibrated for
Tado specifically".

By default the wrapper maps the valve intent onto a setpoint the way
`calibration.py` does for the controller modes
(`setpoint_mapping="production"`): `T_trv + (T_max − T_trv)·u`, a
setpoint below the TRV's reading at `u = 0`, rounded up while heating
and down while idle, then clamped to the TRV's range. The mapping starts
from the TRV's own reading (see [Signal path](#signal-path)); the
scenario's `trv_sensor_rad_fraction`, 0.1 by default, sets how much of
the radiator's excess that reading picks up. The fraction is an
estimate. Between 0.05 and 0.2, TPI stays first on the Tuya and Sonoff
presets and MPC v2 on Tado; on Bosch, MPC v2 overtakes TPI at 0.2.
Absolute scores move by up to 0.06. The older `"heuristic"`
and `"inversion"` mappings remain selectable.

`reports_valve_position` decides what the inner controller gets as the
previous valve: off (the default for all four presets), its own
previous command; on, the opening the TRV actually chose, as Better
Thermostat passes it to MPC v2 when the TRV's climate entity carries a
`valve_position` attribute.

### Limitations

* **It's a simulation.** The room is a lumped-RC model (2 or 3
  states). Real buildings have distributed wall mass, infiltration,
  multi-zone coupling, and occupant behaviour. Trust the *relative
  ranking* across controllers more than the absolute scores.
* **The Oracle cheats.** The reference controller is plant-aware — it
  knows the simulator's exact parameters and inverts them. That is not
  a real controller, just a definition of "what's achievable with
  perfect knowledge". Read it as an asymptote, not a target.
* **Scoring weights are opinionated.** A controller can win
  `balanced` and lose `longevity_first`. The four profiles bracket
  reasonable user priorities, but if your application is asymmetric
  (e.g. comfort cost ≫ energy cost) the right answer may require
  reading the dimension columns, not the `overall`.
* **Energy scoring is symmetric.** Under- and over-heating cost the
  same. If you're optimising specifically for energy savings (where
  under-heating is acceptable and over-heating is the only cost), use
  the comfort and actuator columns separately and reweight outside.
* **The scenario library doesn't cover everything.** Notable
  omissions: multi-zone thermal coupling, occupancy schedules,
  photovoltaic-driven setpoint shifts, HVAC topologies beyond
  radiator + boiler / heat pump.
* **Indirect-TRV presets are heuristic.** Vendor params reflect
  user-reported behaviour, not bench measurements. The wrapper itself
  is approximate (it ignores actual firmware quirks like Sonoff's
  pre-FW 1.3 deadband, which lives in the direct-valve actuator
  profile instead).

Treat the benchmark as a *triangulation tool*: useful for spotting
regressions, comparing controllers on equal footing, and identifying
which dimension a controller's weakness lives in. Not as a
deployment-quality validation — that still needs real-hardware
field-testing.

## Adding a controller

Implement `ControllerAdapter` from `adapters/base.py`:

```python
class MyAdapter:
    name = "my"
    family: ControllerFamily = "valve"

    def reset(self, prior: dict | None = None) -> None: ...
    def step(self, ctx: BenchmarkContext) -> BenchmarkOutput: ...
    def export_state(self) -> dict[str, Any]: ...
```

Register it in `runner.ADAPTER_FACTORIES`:

```python
ADAPTER_FACTORIES["my"] = MyAdapter
```

Existing wrappers under `adapters/` (`mpc_v2_adapter`, `mpc_adapter`,
`pid_adapter`, `tpi_adapter`) serve as templates.

## Adding a scenario

A scenario is a `ScenarioConfig` literal in `scenarios.py`:

```python
SXX_MY_SCENARIO = ScenarioConfig(
    name="SXX_my_scenario",
    description="...",
    duration_minutes=180,
    initial=InitialConditions(T_room=20.0, T_rad=20.0),
    plant=PROFILE_STANDARD,
    setpoint_schedule=schedules.step(30 * 60.0, 20.0, 21.0),
    outdoor_schedule=schedules.constant(5.0),
    transient_start_s=30 * 60.0,
)
```

Add the constant to the `ALL_SCENARIOS` dict at the bottom of the file.
Schedule shape generators (`step`, `ramp`, `pulse`, `piecewise_step`,
`sinus_diurnal`, `stochastic_windows`, `solar_trapezoid`) live in
`schedules.py`.

## Module layout

```text
tests/benchmark/
├── plant.py              RC2 / RC3 thermal plant + plant profiles
├── multi_trv_plant.py    N parallel radiators sharing one room
├── sensor.py             Room sensor (lag, resolution, reporting, faults) + TRV sensor
├── input_stage.py        BT's acceptance gate, EMA and slope of the room temperature
├── actuator.py           Linear / threshold / quadratic / equal-percentage flow
├── schedules.py          Setpoint / outdoor / disturbance schedule builders
├── scenarios.py          ScenarioConfig library
├── weather/              Synthetic AR(1) outdoor + solar generator
├── metrics.py            Comfort, actuator, energy metrics from a time series
├── scoring.py            0..1 dimension scores + UserProfile weighting
├── reporter.py           Score-matrix rendering
├── adapters/             Controller adapters
├── runner.py             CLI entry point
├── multi_trv_runner.py   Drives the multi-TRV plant
├── plant_fit/            Plant-fit demo + synthetic dataset generator
└── tests/                Unit tests
```

## Plant-fit demo

`plant_fit/fit_plant.py` demonstrates deriving `PlantParams` from a
recorder-style temperature export. It finds natural cooling phases and
fits `T(t) = T_outdoor + (T0 − T_outdoor) · exp(−t/τ)` per window.

```sh
uv run python -m tests.benchmark.plant_fit.fit_plant
```

Defaults to an in-memory synthetic dataset (deterministic, no committed
CSV). Pass a path to fit against an external HA recorder export:

```sh
uv run python -m tests.benchmark.plant_fit.fit_plant path/to/recorder_export.csv
```

Run `uv run python -m tests.benchmark.plant_fit.generate_synthetic_data` to dump
the synthetic dataset as a CSV in the recorder format, for example to
inspect what shape `fit_plant` expects.

## Tests

```sh
uv run python -m pytest tests/benchmark/tests -p no:homeassistant
```

`tests/test_regression_gate.py` runs `mpc`, `mpc_v2`, `pid`, `tpi` and
`default` over five scenarios and holds each controller's mean
score per dimension above a floor recorded in `_FLOORS`, about 0.02
below the current value. A floor more than 0.04 below the measured mean
fails as well, and the message names the value to record. A change that
moves a controller therefore updates `_FLOORS` in the same pull request,
downward with a reason or upward to lock in the gain.
