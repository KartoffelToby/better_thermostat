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
clocks; time arrives inside its inputs. Its heart is one function:

```text
decide(snapshot, state) -> (desired, state')
```

- `snapshot.py` — `WorldSnapshot`: the immutable observation of one control
  cycle (temperatures, modes, environment, per-TRV reported state).
- `desired.py` — `DesiredState` / `TrvDesired`: the intent per TRV
  (mode, setpoint, valve percent, offset). Intent, not commands.
- `decide.py` — the precedence cascade: lifecycle & maintenance gate →
  mode OFF → open window or door → call-for-heat → heating. Reachability
  is an address filter applied across it rather than a cascade tier;
  unreachable TRVs are dropped from the commanded set. `decide()` never
  mutates its input state, it returns a successor state.
- `fsm/` — one small state machine per concern (*region*): `window`
  (debounced open/closed; instantiated twice, as the window and the door
  region), `maintenance` (valve exercise with a liveness
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
The snapshot is built fresh per cycle rather than kept as a maintained
cache, so a decision always sees one coherent world; reactivity comes from
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
boundaries. Writes go through the safety hull and the write budget, and
cycles are requested through the scheduler. The shell applies intent; it
does not second-guess the kernel after `decide()` ran.

Run the test suite with `uv run pytest tests/`.

## How Can I Contribute?

## New Adapter

If you want to add a new adapter, please create a new Python file with the name of the adapter in the adapters folder. The file should contain all functions found in the generic.py. If your adapter needs special handling for one of the base functions, override it, if you can use generic functions, use them like:

```python
async def set_temperature(self, entity_id, temperature):
    """Set new target temperature."""
    return await generic_set_temperature(self, entity_id, temperature)
```

## Translations

See the [translation contributor guide](custom_components/better_thermostat/translations/README.md) for the catalog format, placeholder rules, and validation command.

Translations can also be edited with the [INLANG Editor](https://inlang.com/editor/github.com/KartoffelToby/better_thermostat).

### Reporting Bugs

You can create an issue if you have any kind of bug or error but please use the issue template.

## Closing a device- or configuration-specific bug

Most bugs reported here are not calculation errors. They are a device that
speaks a slightly different dialect, or a combination of configuration options
nobody had put together before. Such a bug is closed with **two** artefacts,
not one:

1. **A regression test at the level the bug sat.** A wrong number gets a unit
   test; a write that never reached the device gets an integration test.
2. **A row in the device matrix** — a `DeviceProfile`, `RoleScenario` or
   `GroupScenario` in `tests/integration/device_profiles.py` describing the
   shape the report came from.

The first artefact proves this bug is gone. The second is what catches the
next one: every test parametrized over the matrix runs against that shape from
then on, so what the report taught us is not confined to the single test
written for it.

A profile states a whole device — its integration, its calibration strategy,
its mode vocabulary, its setpoint grid, whether its entity carries a device
registry entry — because those are inseparable in the field. A Zigbee2MQTT
head *is* the mqtt integration plus local calibration, and pairing one with
the other integration describes a device that does not exist. Reuse the
profile that already matches; add a row only when the shape is genuinely new.

Some reports are not about one device at all. A room fitted with several
heads behaves in ways no single head has — they can disagree about the mode,
and one of them can go off the air while the others heat on — so its shape is
a `GroupScenario`, which names the heads the entry drives together rather than
one device.

`SHAPES_FROM_REPORTS` in the same file names the shapes that reached us this
way, and a test asserts each of them is still part of the matrix the
integration suite runs over — a profile that quietly drops out of the matrix
stops covering anything.

Not every report has a device shape. A bug in the config flow, or in what
happens while entities are still coming up, belongs in
`tests/integration/test_config_flow.py` or
`tests/integration/test_startup_scenarios.py` instead; those drive
configurations and timelines rather than devices.

## Fixtures never use the value they are meant to rule out

A test that restores a setting and asserts it came back has to configure it to
a value the thermostat would *not* have arrived at on its own. A fixture on the
production default cannot tell "restored what the user configured" from "fell
back to the built-in value" — it passes either way, and it keeps passing after
the restore breaks.

`tests/integration/test_setting_round_trips.py` holds that rule for the
settings it covers: each case names the default it has to differ from, and a
guard reads that default off a thermostat nobody configured, so a case cannot
go blind without a test failing. When a new setting joins the matrix, give it a
configured value and a default, not just a configured value.

The same applies to the entry a test starts from. A `make_entry()` that omits a
key the config flow always writes does not weaken a test — it takes the branch
behind that key out of the run entirely, and every assertion downstream of it
passes for the wrong reason.
## Naming

Three conventions carry the naming here, and none of them is ours:

- **Spelling:** [PEP 8](https://peps.python.org/pep-0008/) and
  [PEP 257](https://peps.python.org/pep-0257/), the same sources Home Assistant's
  development guidelines defer to. They cover casing, underscores,
  `CAPS_WITH_UNDER` for constants, `CapWords` for classes, and a leading
  underscore for internals.
- **Word choice:** [§3.16 of the Google Python Style
  Guide](https://google.github.io/styleguide/pyguide.html#316-naming): *"Avoid
  abbreviation. In particular, do not use abbreviations that are ambiguous or
  unfamiliar to readers outside your project, and do not abbreviate by deleting
  letters within a word."* Plus its *Names to Avoid* list (no single-character
  names outside counters, exception identifiers and file handles; no type
  information glued onto a name) and *"descriptiveness should be proportional to
  the name's scope of visibility"*. Only that section: the rest of that guide
  prescribes Google-style docstrings and we use numpy ones (below).
  Abbreviations are therefore spelled out: it is `temperature`, not `temp`. The
  exceptions are the words Home Assistant itself is built from, `config` and
  `entity_id`, which are neither ambiguous nor unfamiliar.
- **Domain terms:** `glossary.toml`. One term per concept, the same term in the
  code, in the documentation and in issues.

Two areas deviate from PEP 8 deliberately, under its own clause *"when applying the
guideline would make the code less readable"*: `utils/calibration/` carries the
notation of the control theory it implements (`A`, `B`, `T_room`, `kalman_P`), and
the modules under `model_fixes/` are named after the device model string they are
matched against, not after an identifier anyone chose.

### Zones

Before renaming anything, ask who owns the name. Every glossary term records it.

- **Zone A, free:** locals, arguments, attributes, dataclass fields, private
  functions. Nobody outside the code sees them, so renaming is pure refactoring.
- **Zone B, migratable:** persisted keys in `config_entry.data` and in the `Store`.
  Renameable, but each rename needs a migration step and a test that starts from a
  real old entry.
- **Zone C, contract:** what users write in automations and templates. The keys
  from `extra_state_attributes`, the trigger, condition and action types from
  `device_trigger.py` and its siblings, and everything named verbatim in `docs/`.
  Renaming one costs users their automations, so it is a release decision, not a
  refactoring.

### What the guides leave open

Six rules, because no external guide covers them.

**Entity ids end in `_entity_id`.** Not `*_id`, not `*_entity`, not a bare noun.
The one exception is the key name `entity_id` itself.

**The `bt_` prefix is collision avoidance, not part of the name.** It is permitted
only where a Home Assistant property of the same name lives on the entity class:

| Field | Colliding HA property | Prefix required? |
|---|---|---|
| `bt_hvac_mode` | `hvac_mode` | **yes** |
| `bt_min_temp` | `min_temp` | **yes** |
| `bt_max_temp` | `max_temp` | **yes** |
| `bt_target_temp` | `target_temperature` | no |
| `bt_target_cooltemp` | `target_temperature_high` | no |

Where a BT quantity sits next to the same-named TRV quantity, the owner prefix
`trv.` separates them: `heat_target_temperature` versus `trv.setpoint`.

**A loop over keys and a loop over values must not share a variable name.**
`for trv in self.real_trvs` binds a `str`, `for trv in self.real_trvs.values()`
binds a `Trv`. The key is `entity_id`, the value is `trv`.

**Units are spelled out, in lowercase, and only where they are not the norm.**
Absolute temperatures are °C throughout and carry no suffix:
`room_temperature`, `heat_target_temperature`. Conversion happens at the adapter
seam, and only there may a
`_fahrenheit` name appear. Everything else spells its unit out: `_kelvin` for
temperature differences, `_kelvin_per_min` for rates, `_seconds` or `_minutes` for
durations, `_percent` for percentages. Never `_C`, `_K`, `_k`, `_s`, `_pct`,
`delta_T`, `dT`. Durations carry their unit even though seconds are the norm,
because the persisted configuration mixes seconds and minutes.

**A `CONF_*` constant and its string agree.** `CONF_HEATER = "thermostat"` and
`CONF_WINDOW_TIMEOUT = "window_off_delay"` are the shape to avoid. The constant
follows the string, not the other way round: the string is zone B, the constant is
zone A, so only one of the two is free to move.

**Verb prefixes have fixed meanings.** `get_` is a pure read that does no IO and
cannot fail; `read_` and `fetch_` perform IO; `compute_` is calculation without
state; `build_` constructs an object; `resolve_` picks from several sources by a
precedence rule; `is_`, `has_`, `should_` and `supports_` return `bool`.

### The vocabulary

`glossary.toml` holds one term per concept, its zone, and the spellings it replaces.
Look a concept up there before inventing a name for it, and add a term by pull
request when a concept has none. Two names for one thing is a defect. The codebase
has carried a duplicated field name long enough that a unit test reimplemented a
production predicate from the wrong half of it, and the test still passes.

Sometimes a rejected spelling is the correct name anyway: `current_temperature` is
the Home Assistant property this integration implements. `glossary.toml` records
each such exception together with its reason.

New and touched code follows the convention. The spellings the codebase still
carries come out in their own pull requests, so a rename you did not sign up for
never lands in yours. `scripts/check_naming.py` tells you where you stand, and CI
runs it:

```bash
uv run python scripts/check_naming.py list <path>    # what a file still carries
uv run python scripts/check_naming.py check          # what CI runs
```

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
uv run yamllint --strict .   # lint YAML
```

CI runs these with `uv run --locked` (and `uv sync --locked`) to fail on any
drift between `pyproject.toml` and `uv.lock`; locally the simpler forms above
are fine after `uv sync`.

Dependencies are declared in `pyproject.toml` (`[project]` for the runtime
platform, `[dependency-groups].dev` for tooling) and pinned in `uv.lock`. To
update a dependency, run e.g. `uv lock --upgrade-package homeassistant` and
commit the changed `uv.lock`.

## Coverage floors

CI measures coverage per module and compares it against `.coverage-floors.json`,
which holds the level each module is at today. A change that leaves one of them
less covered than it was fails the build; a module nobody has measured yet has
nothing to fall below and passes.

The floors are per module rather than one number for the project because a
single project-wide threshold is bought back by adding tests where they are
easiest to write, and every user-visible bug this project has had came from a
sparsely covered edge instead.

The measured number is branch coverage (`branch = true` under
`[tool.coverage.run]`). A guard whose condition is only ever met one way costs
percentage even though both of its lines ran, so the direction a test never
takes is visible in the number rather than only in the code. `coverage report
--show-missing` marks such a guard with an arrow (`123->exit`, `123->130`) at
the line the untaken branch leaves from.

`scripts/uncovered_guards.py` turns the same report into a work list: one line
per untaken direction, with the source of the deciding line.

Raising coverage does not update the file — record the new level explicitly, so
that the level being held is a decision someone made:

```bash
uv run pytest tests --cov=custom_components/better_thermostat --cov-report=json:coverage.json
uv run python scripts/coverage_floors.py update
```

`update` prints every floor it lowers. If a pull request lowers one, the diff
says which module gave up coverage and by how much.

## The maintenance line

`1.9` is the maintenance line and `develop` is what ships as the next major
version. A change wanted on both is written twice, one commit per line, because
the lines have diverged far enough that a cherry-pick no longer applies. A
change written only on `1.9` is a gap, and squash-merges hide it: a pair shares
no patch id, so `git cherry` reports every commit as missing and says nothing.

`scripts/forward_port_gaps.py` compares the text instead. For every commit on
`1.9` that `develop` does not contain it takes up to twelve distinctive added
lines and looks each one up in `develop`'s *tree*. Reading the tree rather than
the history is what survives the squash: a line that arrived under any commit
is in the tree.

```bash
git fetch origin 1.9:refs/remotes/origin/1.9        # once, if you have no 1.9
uv run python scripts/forward_port_gaps.py list     # every commit, with its hit rate
uv run python scripts/forward_port_gaps.py check    # what CI runs
```

A commit under a 50% hit rate is a candidate to forward-port. Where it stays
behind on purpose — the same defect fixed in a different place on each line, for
instance — record it in `.forward-port-gaps.json` with the reason. The reason is
written by hand: a generated one would say nothing, and the reason is the point.

This is the release gate for the next major version. It runs on a pull request
from `develop` to `master` and on demand from the Actions tab, and not on
ordinary pull requests: a gap on the maintenance line is not something an
unrelated change has to close.

The script names its own blind spots in its docstring. The one to know before
reading the output: a commit carrying fewer than three markers is not scored at
all, so version bumps and prose-only commits are listed apart rather than
judged, and a real change small enough to leave no marker is listed with them.
