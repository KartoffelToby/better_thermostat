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

## Python version

The floor is Python 3.14.2, inherited from Home Assistant: `hacs.json` names the
minimum core release, and that release declares `Requires-Python: >=3.14.2`.
`pyproject.toml` repeats the floor for the tooling, in `requires-python`, in
ruff's `target-version = "py314"`, and in the pyrefly and pyright settings.

The code uses the grammar 3.14 allows. An `except` clause that binds no name
lists its types bare, as [PEP 758](https://peps.python.org/pep-0758/) permits
since 3.14:

```python
except TypeError, ValueError:
```

Both types are caught. Python 3.13 and older reject the line with
`SyntaxError: multiple exception types must be parenthesized`, so a checker or
an editor that flags it is running below the floor. Two contributors have read
it as Python 2 instead, where the name after the comma would have been the bound
exception. No Python 3 ever did that, and adding the parentheses back changes no
behaviour.

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

### Which branch a pull request targets

Open pull requests against `develop`, which is what ships as the next major
version. A fix the maintenance line needs too gets a second pull request
against `1.9`; [The maintenance line](#the-maintenance-line) explains why a
change for both lines is written twice. Never target `master`: it carries
releases and only receives pull requests from `develop` and `1.9`. GitHub
offers `master` as the default base, so change it when you open the pull
request.

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

## Where a test goes

- `tests/unit/` drives one module or function with its collaborators stood in.
- `tests/integration/` runs the integration inside a Home Assistant test
  instance against simulated devices.
- `tests/benchmark/` scores the calibration modes in a simulated room.
- `tests/gates/` checks the repository rather than the integration: the
  recorded budgets, the scripts that hold them, the release metadata, and the
  rules the suite keeps for its own fixtures.

A unit test that needs a thermostat builds it with `make_bt()` or
`ThermostatStandIn` from `tests/factories.py`, never a bare `MagicMock`. The
stand-in raises when the code under test reads state the test did not set,
where a bare mock would answer with a truthy mock and quietly take the test
down another branch. `tests/gates/test_thermostat_stand_in_discipline.py`
holds that rule.

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

## A test docstring names the requirement, not the run

A test asserts its rule only as well as somebody once wanted that rule. Where
the docstring retells what the code does, the test holds the behaviour that was
found, and it holds a defect as firmly as it holds a decision. Nothing in the
suite objects: the lines run, the module keeps its coverage floor, and a
mutation probe even reports the spot as well covered, because a defect planted
there turns it red. Whether the contract is the wanted one is a question to the
text, not to the execution.

"A single HomematicIP device slows the whole group down" is an observation.
"The room sensor follows its own debounce interval" is a requirement. Both
sentences describe the same run, and only the second says what the test is for.

Where the requirement cannot be phrased without sounding like a bug, it is one.
Take that literally. If the honest sentence comes out as "one slow head holds
up every sensor in the room", stop writing the docstring and go read the code.

A requirement that is real but not met yet still belongs in the tree, stated as
a requirement and marked `@pytest.mark.xfail(strict=True)` with a reason that
says what the code does today. `strict` is what makes that worth doing: the
test turns the marker into a failure the moment the behaviour arrives, so the
marker comes off with the fix instead of outliving it.

`scripts/restated_contracts.py list` collects the summaries where this question
is worth asking — a shouted quantifier, an `if any` clause, `regardless`, `even
when`, a bare interval copied out of the source — and
`.restated-contract-budget.json` holds the count, so the backlog gets worked off
rather than added to. A file whose count fell fails the check until
`restated_contracts.py update` records the lower number. A hit is a question, not a verdict: a requirement may well
say "never". Read the sentence and decide which of the two it is.

Those markers are a sample, not a survey: they reach a small share of the
tree's test docstrings, and the script's own docstring names the common shapes
they miss. A green check means the budget held, not that a docstring you are reading
is fine.

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
| `bt_target_temperature_step` | `target_temperature_step` | **yes** |

Where a BT quantity sits next to the same-named TRV quantity, the owner prefix
`trv.` separates them: `heat_target_temperature` versus `trv.commanded_setpoint`.

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
because the persisted configuration mixes seconds and minutes. Of those
spellings, only `delta_T`, `delta_t` and `dT` are in `glossary.toml`, so only
they are checked; the suffixes rest on review.

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

Sometimes a rejected spelling is the correct name in one place, such as a Home
Assistant property the integration has to implement under that name.
`glossary.toml` then records an exception with its paths and its reason. The
checker refuses an exception without a reason, and one for a spelling no term
rejects.

Under `tests/` a rejected spelling is charged only once production has stopped
using it. A test has to name the attribute it asserts on, so that spelling is
production's decision and not the test's, and a new test may use a rejected
spelling for as long as a production field still carries it. Renaming the
last production site is what makes its readers due, and they come out with it.

`scripts/check_naming.py` matches whole identifiers against the rejected
spellings in `glossary.toml`, a leading underscore included (`_offset` spells
`offset`), and CI runs it. The tree carries none of them, so
a single rejected spelling fails the check:

```bash
uv run python scripts/check_naming.py list <path>    # what a file carries
uv run python scripts/check_naming.py check          # what CI runs
```

A new term can reject a spelling the tree still uses. Its pull request then
records that backlog per file with `update --allow-raise`, which writes
`.naming-budget.json`: a file may not exceed its number, and a file that is not
in the budget may not carry one at all. The old spellings come out in their own
pull requests, each of which runs `update` to record the lower count, and the
file deletes itself once the last one is gone. `update` refuses to record a
count that grew without `--allow-raise`, which is also the flag for a file that
moved and took its backlog along.

The two halves are checked by different tools. `check_naming.py` reads vocabulary
and says nothing about case; `ruff check` reads case and shape through its `N`
rules and says nothing about which word was chosen.

Where the case rules give way, `pyproject.toml` says so in a `per-file-ignores`
entry, and there are five: the control-theory notation under
`custom_components/better_thermostat/utils/calibration/`, its three test mirrors
`tests/benchmark/`, `tests/unit/mpc_v2/` and
`tests/unit/test_mpc_comprehensive.py`, and the device model strings that name
the modules under `model_fixes/`. Each entry drops only the rules that fire
under it. Where a single line carries the notation rather than a tree, a
`# noqa: N8xx` with its reason does the job instead, as the two persisted field
names in `utils/state_manager.py` do. Both forms are capped by
`.pep8-naming-budget.json`, which records per file how many findings they hide,
and CI holds that number exactly: a count above it or below it fails, and
`update` records a lower one:

```bash
uv run python scripts/pep8_naming_budget.py check    # what CI runs
uv run python scripts/pep8_naming_budget.py update   # after a rename
```

Inside those paths both gates apply. The ruff exemption covers case alone, so
the notation may still not use a spelling `glossary.toml` rejects: a
temperature difference is `delta_kelvin` there too, never `delta_T`.

## Blind exception handlers

Ruff's `BLE001` flags an `except Exception` that neither re-raises nor logs the
traceback. `.blind-except-budget.json` records per file how many such handlers
the file carries today; a file may not exceed its number, and a file that is not
in the budget may not have one at all. A file that drops below its number fails
until `update` records the lower one. The scan ignores ruff's configuration and
every `noqa`, so the budget file is the only place a silent handler is recorded.

A broad handler that has to stay, one around a write to another integration's
device where a failure can arrive as any exception type, carries
`# noqa: BLE001` with its reason on the `except` line. The directive covers that
one line, and `ruff check` reports the next blind handler in the same file.
BLE001 does not reach `contextlib.suppress(Exception)`, which stays a review
concern.

```bash
uv run python scripts/blind_except_budget.py check     # what CI runs
uv run python scripts/blind_except_budget.py update    # after converting handlers
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

Three more checks run on every pull request:

- **Types:** `uv run pyrefly check`. Every module under
  `custom_components/better_thermostat` is checked at the strictness
  `[tool.pyrefly]` in `pyproject.toml` declares. The `sub-config` entries below
  it name the files that do not meet it yet and the rules each is exempt from.
  That list only shrinks: a new file is strict from the start, and
  `tests/gates/test_type_strictness_exemptions.py` holds it to a recorded
  ceiling.
- **hassfest:** Home Assistant's validator for the integration manifest and
  its metadata.
- **HACS:** the HACS action validates the repository as a HACS integration.

Neither of the last two has a local command here; read their result on the pull
request.

Dependencies are declared in `pyproject.toml` (`[project]` for the runtime
platform, `[dependency-groups].dev` for tooling) and pinned in `uv.lock`. To
update a dependency, run e.g. `uv lock --upgrade-package homeassistant` and
commit the changed `uv.lock`.

## Coverage floors

CI measures coverage per module and compares it against `.coverage-floors.json`,
which holds the level each module is at today. A change that leaves one of them
less covered than it was fails the build. A new module fails too until its floor
is recorded, so it is held to the coverage it arrives with from its first day.

A recorded module the report does not cover fails the build as well. A floor
nothing measures holds nothing back, so a module that was deleted or renamed
has to be re-recorded rather than leave its floor behind unenforced.

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

## Integration Quality Scale

Home Assistant grades integrations by the rules of its
[Integration Quality Scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/).
`custom_components/better_thermostat/quality_scale.yaml` records, in Home
Assistant's own format, which of them Better Thermostat meets: each rule is
`done`, `todo` or `exempt`, and an exempt rule says why. The file holds the
Bronze tier for now.

Hassfest checks that file for core integrations only, so the test suite holds
it here. A test that checks a rule carries its name:

```python
@pytest.mark.quality_rule("runtime-data")
async def test_a_loaded_entry_keeps_its_runtime_state_on_the_entry(hass, fake_trv): ...
```

A test of a `todo` rule runs as strict `xfail`: it has to fail, and once the
gap is closed it passes, which fails the run until the rule is switched to
`done` in the file. `tests/gates/test_quality_scale.py` requires a test for
every rule that is `done` or `todo`, apart from the few listed in
`REVIEWED_BY_HAND`, and refuses a marker for a rule the file does not record.

The rules a running instance decides are in
`tests/integration/test_quality_scale_bronze.py`; those the sources decide, the
manifest, the strings, the coverage floors and the documentation, are in
`tests/gates/test_quality_scale_bronze_sources.py`. A documentation test only
checks that the section exists; whether it explains anything is for review.

## The maintenance line

`1.9` is the maintenance line and `develop` is what ships as the next major
version. A change wanted on both is written twice, one commit per line, because
the lines have diverged far enough that a cherry-pick no longer applies. A
change written only on `1.9` is a gap, and history does not show it: the two
commits of a pair are written separately, so they share no ancestry below the
merge base, and `git cherry` matches by patch id, which ignores only whitespace
and line numbers. Two diffs that differ in anything else get different patch
ids, so `git cherry` finds no equivalent patch and reports such a pair as
missing, the same as a gap.

`scripts/forward_port_gaps.py` compares the text instead. For every commit on
`1.9` that `develop` does not contain it takes up to twelve distinctive added
lines and looks each one up in `develop`'s *tree*. Reading the tree rather than
the history is what finds a pair: a line that arrived under any commit is in
the tree. The lines come from production files only, since each line writes
its own tests, and only from lines `1.9` still holds, since a state a later
`1.9` commit replaced is judged by that commit. A name `develop` renamed onto
`glossary.toml` is looked up under its new spelling too.

```bash
git fetch origin develop:refs/remotes/origin/develop 1.9:refs/remotes/origin/1.9
uv run python scripts/forward_port_gaps.py list     # every commit, with its hit rate
uv run python scripts/forward_port_gaps.py check    # what CI runs
```

Both lines default to their `origin/` refs, so a local `develop` that lags
behind cannot report gaps that `origin/develop` has closed. To measure a local
branch instead, pass it as `--development`.

A commit under a 50% hit rate is a candidate to forward-port. Where it stays
behind on purpose — the same defect fixed in a different place on each line, for
instance — record it in `.forward-port-gaps.json` with the reason. The reason is
written by hand: a generated one would say nothing, and the reason is the point.

This is the release gate for the next major version. It runs on every pull
request to `master` and on demand from the Actions tab. One head passes without
being checked: a pull request from `1.9`, which is a maintenance release and
ships the very commits the report would name. Ordinary pull requests target
`develop` and never reach the gate. A merge pushed to `master` directly has no
pull request, and nothing watches that path.

The script names its own blind spots in its docstring. The one to know before
reading the output: a commit carrying fewer than three markers gets no hit rate
and counts as carried forward only when every one of its markers is on
`develop`. A commit with no production marker at all (version bumps, prose-only
and test-only commits) is listed apart rather than judged, and a real change
small enough to leave no marker is listed with them.
