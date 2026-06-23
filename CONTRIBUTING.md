# Contributing to Better Thermostat

:+1::tada: First off, thanks for taking the time to contribute! :tada::+1:

The following is a set of guidelines for contributing to Better Thermostat. These are mostly guidelines, not rules. Use your best judgment, and feel free to propose changes to this
document in a pull request.

## Development

#### Requirements
- VSCode
- Docker
- Devcontainer Extension

#### Setup
1. Clone the repository
2. Open the repository in VSCode
3. Click on the green button in the bottom left corner and select "Reopen in Container"
4. Wait for the container to build
5. Open Task Runner and run "Run Home Assistant on port 9123"
6. Open the browser and go to http://localhost:9123 -> Inital DEV HA Setup


#### Nice to know

- Debugging is possible with the VSCode Debugger. Just run the HomeAssistant in Debugger and open your browser to http://localhost:9123 (No task run needed)
- Update your local in devcontainer configuration.yaml to the current version of the repository to get the latest changes. -> Run "Sync configuration.yaml (Override local)" in Task Runner
- Test BT in a specific HA version -> Run "Install a specific version of Home Assistant" in Task Runner and the version you want to test in the terminal prompt.
- Test BT with the latest HA version -> Run "upgrade Home Assistant to latest dev" in Task Runner

## Architecture

Better Thermostat separates a pure decision core from an imperative shell.
The core computes *what* every TRV should do; the shell observes Home
Assistant and performs the device writes.

### The core (`custom_components/better_thermostat/core/`)

The core imports no Home Assistant code, performs no IO, and reads no
clocks — time arrives inside its inputs. Its heart is one function:

```text
decide(snapshot, state) -> (desired, state')
```

- `snapshot.py` — `WorldSnapshot`: the immutable observation of one control
  cycle (temperatures, modes, environment, per-TRV reported state).
- `desired.py` — `DesiredState` / `TrvDesired`: the intent per TRV
  (mode, setpoint, valve percent, offset). Intent, not commands.
- `decide.py` — the precedence cascade: lifecycle gate → mode OFF →
  open window → call-for-heat → heating. Reachability is an address
  filter applied across it (unreachable TRVs are dropped from the
  commanded set), not a cascade tier. `decide()` never mutates its input
  state; it returns a successor state.
- `fsm/` — one small state machine per concern (*region*): `window`
  (debounced open/closed), `maintenance` (valve exercise with a liveness
  bound), `lifecycle` (startup/running/stopped), `mode`, `control_mode`
  (the fail-soft ladder OPTIMAL → SENSOR_FALLBACK → HOLD), `reachability`
  (per-TRV online/offline with retry backoff). Regions gate; controllers
  compute. Regions never read each other's internals.
- `safety.py` — the safety hull: clamps every outgoing setpoint, offset,
  and valve percentage to device limits and the frost floor. Every device
  write passes through it.
- `watchdog.py` — detects a silently stalled control loop.
- `recorder.py` — the flight recorder: a bounded ring of
  (snapshot, pre-decide state, desired) tuples. Exported in the HA
  diagnostics download; `replay()` re-runs an exported tuple through the
  kernel deterministically.
- `clock.py` — the `Clock` protocol plus a deterministic `FakeClock` for
  tests and replay.
- `calibrator.py` — the contract calibration strategies implement
  (capabilities, health).

### The shell

- `utils/snapshot.py` — `build_snapshot()`: the single seam that flattens
  entity attributes and HA states into a `WorldSnapshot`.
- `utils/controlling.py` — `compute_control_cycle()` (one observation and
  decision per cycle, recorded once), `control_trv()` (translates intent
  into adapter calls), the per-TRV/per-channel write budget (minimum
  spacing between non-safety writes), and `reconcile_tick()` (periodic:
  re-converges devices whose reported state diverged from the intent).
- `utils/scheduler.py` — `request_control_cycle()`: the only way to ask
  for a control cycle; requests coalesce.
- `climate.py` — the entity: HA lifecycle, event listeners, persistence
  (via `utils/state_manager.py`), and the kernel state it threads through
  the cycles.

### Control cycles: pulled, not polled

A control cycle is one pass of `build_snapshot() → decide() → apply`.
The snapshot is not a maintained cache — it is built fresh per cycle,
so a decision always sees one coherent world; reactivity comes from
events, user actions, and the five-minute ticks each *requesting* a
cycle (requests coalesce). A cycle writes only differences;
safety-relevant writes go out immediately, everything else is spaced by
the 30-second per-channel write budget. The full trigger and write
model, the regions, the fail-soft ladder, and the test strategy are
documented in depth under [docs/internals/](docs/internals/architecture.md)
(published at better-thermostat.org under *Internals*).

### Where new logic goes

A new rule about *what should happen* (a gate, a precedence, a mode)
belongs in the core: extend `decide()` or a region, with pure unit tests.
New *device interaction* belongs in the shell behind the existing
boundaries — writes go through the safety hull and the write budget, and
cycles are requested through the scheduler. The shell applies intent; it
does not second-guess the kernel after `decide()` ran.

Run the test suite with `pytest tests/`.

## How Can I Contribute?

## New Adapter

If you want to add a new adapter, please create a new Python file with the name of the adapter in the adapters folder. The file should contain all functions found in the generic.py. If your adapter needs special handling for one of the base functions, override it, if you can use generic functions, use them like:

```python
async def set_temperature(self, entity_id, temperature):
    """Set new target temperature."""
    return await generic_set_temperature(self, entity_id, temperature)
```

## Translations

[INLANG Editor](https://inlang.com/editor/github.com/KartoffelToby/better_thermostat)

### Reporting Bugs

You can create an issue if you have any kind of bug or error but please use the issue template.
## Docstring type

We use numpy type docstrings. Documentation can be found here:

https://sphinxcontrib-napoleon.readthedocs.io/en/latest/example_numpy.html

## Local setup (uv)

For the containerized workflow see [Development → Setup](#setup) above; this
section covers running the tooling directly on your machine.

This project uses [uv](https://docs.astral.sh/uv/) to manage the development
and test environment. Install uv, then create the environment from the lockfile:

```bash
uv sync --frozen
```

Install the pre-commit hooks (ruff check + format) once:

```bash
uv run pre-commit install
```

Common tasks:

```bash
uv run pytest tests          # run the test suite
uv run ruff check            # lint
uv run ruff format           # format
```

CI runs these with `uv run --locked` (and `uv sync --locked`) to fail on any
drift between `pyproject.toml` and `uv.lock`; locally the simpler forms above
are fine after `uv sync`.

Dependencies are declared in `pyproject.toml` (`[project]` for the runtime
platform, `[dependency-groups].dev` for tooling) and pinned in `uv.lock`. To
update a dependency, run e.g. `uv lock --upgrade-package homeassistant` and
commit the changed `uv.lock`.
