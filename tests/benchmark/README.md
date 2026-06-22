# Calibration Controller Benchmark

Pure-simulation comparison framework for Better Thermostat's calibration
controllers (`mpc`, `pid`, `tpi`) against reproducible thermal-dynamics
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
 *ideal_oracle          0.963  0.109    0.954  0.141     0.987  0.079    0.951  0.172   37
  tpi                   0.783  0.153    0.694  0.228     0.844  0.263    0.914  0.157   37
  pid                   0.778  0.143    0.849  0.208     0.555  0.365    0.933  0.160   37
  mpc                   0.685  0.134    0.855  0.179     0.256  0.354    0.904  0.157   37
  bangbang              0.567  0.151    0.761  0.194     0.129  0.301    0.736  0.175   37
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

**actuator** — `1 − (excess valve travel) / (4× Oracle travel)`, where
travel is `Σ|Δu_pct|` over the run.

| Score | What it means in practice |
|---|---|
| 1.00 | As little actuator activity as the Oracle. |
| 0.70–1.00 | Healthy modulation. |
| 0.40–0.70 | Detectable wear / battery drain on Zigbee TRVs. |
| 0.20–0.40 | High wear; expect motor lifetime and battery hits. |
| < 0.20 | Pathological cycling (BangBang territory). |

**energy** — integral valve usage relative to the Oracle's optimum,
symmetric around 1.0× so under-heating (missed setpoint) costs the same
as over-heating (wasted energy).

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

* **Oracle ≈ 0.96** — practical ceiling. Even the Oracle isn't 1.0:
  some scenarios have permanent disturbances (diurnal outdoor, multi-day
  weather) where settling-to-setpoint is impossible by construction.
* **BangBang ≈ 0.57** — noise floor. A deliberately naive on/off
  controller; anything close to BangBang has a real problem.
* **Production controllers (`pid`, `tpi`, `mpc`, `heating_power`) ≈
  0.68–0.78.** That band is the realistic operating range. A controller
  above 0.80 is beating most of the field; near the BangBang floor means
  a clear weakness in at least one dimension.

Rule of thumb: when a controller scores well below the Oracle, look at
which dimension column dropped. A 0.92 comfort with 0.25 actuator means
the controller tracks the setpoint by thrashing the valve.

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

### Indirect TRVs (`pid+indirect_trv`, `tpi+indirect_trv`, `mpc+indirect_trv`)

A wrapper that mediates the controller's valve-% intent through an
offset-mode TRV (Tado, Bosch BTH-RA, Sonoff TRVZB offset-mode, Tuya
TS0601). The TRV runs its own internal P-loop and only accepts a
quantised setpoint; the score reflects what physically reaches the room,
not what the controller intended.

Each row aggregates the four vendor presets × all single-TRV
scenarios. The vendor parameters are heuristic operating points based
on observed user behaviour, **not calibrated truth** — read the row as
"indicative for offset-mode TRVs in general", not as "calibrated for
Tado specifically".

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

Existing wrappers under `adapters/` (`mpc_adapter`, `pid_adapter`,
`tpi_adapter`) serve as templates.

## Adding a scenario

A scenario is a `ScenarioConfig` literal in `scenarios.py`:

```python
SXX_MY_SCENARIO = ScenarioConfig(
    name="SXX_my_scenario",
    description="...",
    duration_min=180,
    initial=InitialConditions(T_room_C=20.0, T_rad_C=20.0),
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
├── sensor.py             Sampled-output sensor (lag, EMA, dropout, bias, drift, jitter)
├── actuator.py           Linear / threshold / exponential / equal-percentage flow
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
