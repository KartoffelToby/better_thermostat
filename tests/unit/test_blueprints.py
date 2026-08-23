"""Validation tests for the bundled automation blueprints.

An optional input that defaults to an empty string and is then used where Home
Assistant requires a valid value at *save* time — a service name
(``<domain>.<name>``) or an ``entity_id`` — makes the whole blueprint unsavable
via the UI. Runtime ``if`` / condition guards do not prevent that validation
error.

For each blueprint every input is substituted with its declared default — the
scenario of a user saving the form untouched — and these checks assert that:

* every ``service`` / ``action`` value is a valid service or a template, and
* every ``entity_id`` (in triggers and service targets) is a valid entity id.

The two fields are validated directly via ``cv.service`` / ``cv.entity_ids``
rather than the full ``cv.SCRIPT_SCHEMA``: full schema validation pulls in Home
Assistant's event-loop / frame guards that are unavailable outside a running
instance.
"""

from ast import literal_eval
from datetime import datetime
from pathlib import Path

from homeassistant.components.automation import config as automation_config
from homeassistant.components.blueprint.models import Blueprint, BlueprintInputs
from homeassistant.components.blueprint.schemas import BLUEPRINT_SCHEMA
import homeassistant.helpers.config_validation as cv
from homeassistant.util.yaml import loader as yaml_loader
import jinja2
import pytest
import voluptuous as vol
import yaml

from custom_components.better_thermostat.device_trigger import TRIGGER_SCHEMA
from custom_components.better_thermostat.utils.const import DOMAIN

BLUEPRINTS_DIR = Path(__file__).resolve().parents[2] / "blueprints"
BLUEPRINT_FILES = sorted(BLUEPRINTS_DIR.glob("*.yaml"))


class _Input:
    """Marker object standing in for a ``!input <name>`` reference."""

    def __init__(self, name: str):
        self.name = name


class _BlueprintLoader(yaml.SafeLoader):
    """SafeLoader that understands the blueprint ``!input`` tag."""


_BlueprintLoader.add_constructor(
    "!input", lambda loader, node: _Input(loader.construct_scalar(node))
)


def _load(path: Path) -> dict:
    """Parse a blueprint YAML file, keeping ``!input`` tags as ``_Input``."""
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_BlueprintLoader)


def _placeholder_for(spec) -> object:
    """Return a valid stand-in value for a required input (one without a default)."""
    selector = (spec.get("selector") or {}) if isinstance(spec, dict) else {}
    if "target" in selector:
        return {"entity_id": "climate.bt_test"}
    if "device" in selector:
        return "bt_test_device_id"
    if "entity" in selector:
        return "sensor.bt_test"
    if "number" in selector:
        return 1
    if "boolean" in selector:
        return False
    if "time" in selector:
        return "00:00:00"
    return "bt_test"


def _resolve_inputs(blueprint: dict) -> dict:
    """Map every input to its declared default (or a placeholder if required)."""
    resolved = {}
    for name, spec in blueprint["blueprint"]["input"].items():
        if isinstance(spec, dict) and "default" in spec:
            resolved[name] = spec["default"]
        else:
            resolved[name] = _placeholder_for(spec)
    return resolved


def _substitute(value, inputs):
    """Recursively replace every ``_Input`` with its resolved value."""
    if isinstance(value, _Input):
        return inputs[value.name]
    if isinstance(value, dict):
        return {k: _substitute(v, inputs) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, inputs) for v in value]
    return value


def _is_template(value) -> bool:
    """Return True if *value* is a string carrying a Jinja template marker."""
    return isinstance(value, str) and ("{{" in value or "{%" in value)


def _service_problem(value):
    """Return a reason string if *value* is not a savable service, else None."""
    if _is_template(value):
        return None
    if not isinstance(value, str) or value == "":
        return f"empty/invalid service: {value!r}"
    try:
        cv.service(value)
    except vol.Invalid:
        return f"service does not match <domain>.<name>: {value!r}"
    return None


def _entity_problem(value):
    """Return a reason string if *value* is not a valid entity id, else None."""
    if _is_template(value):
        return None
    try:
        cv.entity_ids(value)
    except vol.Invalid:
        return f"invalid entity_id: {value!r}"
    return None


def _collect_problems(config) -> list[str]:
    """Walk the substituted config, flagging bad services and entity ids."""
    problems: list[str] = []

    def walk(node, path):
        if isinstance(node, dict):
            for key, val in node.items():
                here = f"{path}.{key}"
                if key in ("service", "action") and isinstance(val, str):
                    reason = _service_problem(val)
                    if reason:
                        problems.append(f"{here}: {reason}")
                if key == "entity_id":
                    reason = _entity_problem(val)
                    if reason:
                        problems.append(f"{here}: {reason}")
                walk(val, here)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]")

    walk(config, "")
    return problems


def test_blueprints_present():
    """Sanity check: there are blueprints to validate."""
    assert BLUEPRINT_FILES, f"no blueprints found in {BLUEPRINTS_DIR}"


@pytest.mark.parametrize("path", BLUEPRINT_FILES, ids=lambda p: p.name)
def test_blueprint_saves_with_default_inputs(path):
    """Every blueprint must validate with all inputs left at their defaults."""
    blueprint = _load(path)
    inputs = _resolve_inputs(blueprint)

    relevant = {
        key: _substitute(blueprint[key], inputs)
        for key in ("trigger", "triggers", "action", "actions")
        if key in blueprint
    }

    problems = _collect_problems(relevant)
    assert not problems, f"{path.name} would fail to save:\n" + "\n".join(problems)


# ── Input shapes beyond the declared defaults ────────────────────────────────
#
# Substituting only the declared defaults exercises the one case that was
# already green while #2039 was open. These are the shapes a saved automation
# actually holds once the presence and pause inputs became list selectors,
# including the plain string a config saved before that change still carries.

WEEKLY_SCHEDULE = BLUEPRINTS_DIR / "weekly_heating_schedule.yaml"

_INPUT_SHAPES = [
    pytest.param({}, id="form-untouched"),
    # The shape from #2039: the notification target is optional, but clearing
    # it left `service: !input notify_target` holding '' -- which is not a
    # valid service name, so the whole automation refused to save. Runtime
    # guards cannot help; validation happens before any of them run.
    pytest.param({"notify_target": ""}, id="notification-target-cleared"),
    pytest.param(
        {"notify_target": "", "presence_entity": [], "pause_switch": []},
        id="notification-target-cleared-and-presence-empty",
    ),
    pytest.param(
        {"presence_entity": [], "pause_switch": []}, id="cleared-to-empty-list"
    ),
    pytest.param(
        {"presence_entity": ["person.a"], "pause_switch": ["input_boolean.p"]},
        id="single-item-list",
    ),
    pytest.param(
        {
            "presence_entity": ["person.a", "person.b"],
            "pause_switch": ["input_boolean.p", "input_boolean.q"],
        },
        id="multi-item-list",
    ),
    pytest.param(
        {"presence_entity": "person.a", "pause_switch": "input_boolean.p"},
        id="legacy-plain-entity-id",
    ),
]


_REQUIRED = {"thermostat_target": {"entity_id": "climate.bt_test"}}


async def _validate_weekly_schedule(hass, overrides):
    """Validate the blueprint the way Home Assistant does when saving.

    `_collect_problems` above checks the two fields that are known to break a
    save; this goes through the real path instead -- `async_substitute()` then
    the automation config validator -- so the whole trigger/condition/action
    schema is applied, not just those fields.
    """
    data = await hass.async_add_executor_job(
        yaml_loader.load_yaml, str(WEEKLY_SCHEDULE)
    )
    blueprint = Blueprint(
        data,
        path=WEEKLY_SCHEDULE.name,
        expected_domain="automation",
        schema=BLUEPRINT_SCHEMA,
    )
    inputs = BlueprintInputs(
        blueprint,
        {
            "use_blueprint": {
                "path": WEEKLY_SCHEDULE.name,
                "input": {**_REQUIRED, **overrides},
            }
        },
    )
    inputs.validate()
    return await automation_config.async_validate_config_item(
        hass, "automation", inputs.async_substitute()
    )


@pytest.mark.parametrize("overrides", _INPUT_SHAPES)
async def test_weekly_schedule_validates_for_every_input_shape(hass, overrides):
    """Every shape a stored config can hold has to survive a save."""
    config = await _validate_weekly_schedule(hass, overrides)

    assert config is not None
    assert config.validation_status == "ok", config.validation_error


async def test_weekly_schedule_rejects_an_empty_string_input(hass):
    """A known limitation, pinned so it is not mistaken for a fixed case.

    An empty string is the shape reported in #2039. It cannot be repaired
    inside the blueprint: a trigger `entity_id` accepts no templates, so there
    is nowhere to normalise the value before Home Assistant validates it. It
    is also not reachable through the UI, since saving is what fails.
    """
    with pytest.raises(vol.Invalid, match="neither a valid entity ID"):
        await _validate_weekly_schedule(
            hass, {"presence_entity": "", "pause_switch": ""}
        )


# ── Runtime behaviour of the presence / pause variables ──────────────────────


def _render(template_text: str, context: dict, states: dict):
    """Render a blueprint variable the way Home Assistant would.

    Home Assistant strips a rendered `variables:` entry and then runs
    `literal_eval` over the result (`homeassistant.helpers.template.
    _parse_result`), keeping the raw string when that fails. A branch
    rendering the bare word `false` therefore yields the *string* `"false"`,
    which is truthy -- so the value a template produces has to be a Python
    literal, not just look like one.
    """
    env = jinja2.Environment()
    env.globals["states"] = lambda entity: states.get(entity, "unknown")
    env.globals["is_state"] = lambda entity, value: states.get(entity) == value
    rendered = env.from_string(template_text).render(**context).strip()
    try:
        return literal_eval(rendered)
    except ValueError, TypeError, SyntaxError, MemoryError:
        return rendered


@pytest.fixture(name="schedule_variables")
def schedule_variables_fixture():
    """The `variables:` block of the weekly schedule blueprint."""
    return _load(WEEKLY_SCHEDULE)["variables"]


@pytest.mark.parametrize(
    ("selection", "expected"),
    [
        pytest.param(["person.a", "person.b"], ["person.a", "person.b"], id="list"),
        pytest.param(["person.a"], ["person.a"], id="single-item-list"),
        pytest.param("person.a", ["person.a"], id="legacy-plain-entity-id"),
        pytest.param([], [], id="cleared-to-empty-list"),
        pytest.param("", [], id="empty-string"),
    ],
)
def test_presence_selection_normalises_to_a_list(
    schedule_variables, selection, expected
):
    """Every stored shape has to reduce to a list of entity ids."""
    assert (
        _render(
            schedule_variables["presence_entities"],
            {"presence_entity_selection": selection},
            {},
        )
        == expected
    )


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        pytest.param({"person.a": "not_home", "person.b": "home"}, True, id="one-home"),
        pytest.param({"person.a": "home", "person.b": "home"}, True, id="both-home"),
        pytest.param(
            {"person.a": "not_home", "person.b": "not_home"}, False, id="none-home"
        ),
    ],
)
def test_anyone_home_reads_every_selected_entity(schedule_variables, states, expected):
    """Reading only the first entity applied the vacation preset with someone home."""
    result = _render(
        schedule_variables["anyone_home"],
        {"enable_presence_mode": True, "presence_entities": ["person.a", "person.b"]},
        states,
    )

    assert result is expected


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        pytest.param(
            {"input_boolean.p": "off", "input_boolean.q": "on"}, True, id="one-on"
        ),
        pytest.param(
            {"input_boolean.p": "off", "input_boolean.q": "off"}, False, id="none-on"
        ),
    ],
)
def test_schedule_paused_reads_every_selected_entity(
    schedule_variables, states, expected
):
    """Same defect as presence: only the first switch was ever evaluated."""
    result = _render(
        schedule_variables["schedule_paused"],
        {
            "enable_pause_switch": True,
            "pause_switch_entities": ["input_boolean.p", "input_boolean.q"],
        },
        states,
    )

    assert result is expected


@pytest.mark.parametrize(
    ("name", "context", "expected"),
    [
        pytest.param(
            "anyone_home",
            {"enable_presence_mode": False, "presence_entities": []},
            True,
            id="presence-disabled",
        ),
        pytest.param(
            "anyone_home",
            {"enable_presence_mode": True, "presence_entities": []},
            True,
            id="presence-enabled-but-nothing-selected",
        ),
        pytest.param(
            "schedule_paused",
            {"enable_pause_switch": False, "pause_switch_entities": []},
            False,
            id="pause-disabled",
        ),
        pytest.param(
            "schedule_paused",
            {"enable_pause_switch": True, "pause_switch_entities": []},
            False,
            id="pause-enabled-but-nothing-selected",
        ),
    ],
)
def test_disabled_branches_render_real_booleans(
    schedule_variables, name, context, expected
):
    """A bare `false` renders as a truthy string, which gated every slot away.

    `schedule_paused` is the one that bit: with the pause feature off -- the
    default -- it produced the string `"false"`, so the slot condition
    `{{ not schedule_paused }}` evaluated to False and no preset was applied.
    """
    result = _render(schedule_variables[name], context, {})

    assert isinstance(result, bool), f"{name} rendered {result!r}, not a bool"
    assert result is expected


_SLOT_TIMES = {
    "slot1_time": "06:30:00",
    "slot2_time": "08:30:00",
    "slot3_time": "17:00:00",
    "slot4_time": "22:30:00",
}


@pytest.mark.parametrize(
    ("clock", "expected_slot"),
    [
        pytest.param("06:30:00", 1, id="start-of-slot1"),
        pytest.param("07:15:00", 1, id="inside-slot1"),
        pytest.param("09:00:00", 2, id="inside-slot2"),
        pytest.param("18:00:00", 3, id="inside-slot3"),
        pytest.param("23:00:00", 4, id="inside-slot4"),
        pytest.param("03:00:00", 4, id="before-slot1-belongs-to-the-night-slot"),
    ],
)
def test_current_schedule_preset_follows_the_active_slot(
    schedule_variables, clock, expected_slot
):
    """Startup recovery, pause resume and arrival apply the current slot's preset.

    `active_slot` renders a bare digit, so `literal_eval` hands it on as an
    integer. A comparison against the strings `'1'`/`'2'`/`'3'` matches
    nothing and sends all three paths to the slot 4 preset.
    """
    active_slot = _render(
        schedule_variables["active_slot"],
        {"now": lambda: datetime.strptime(clock, "%H:%M:%S"), **_SLOT_TIMES},
        {},
    )

    assert active_slot == expected_slot
    assert isinstance(active_slot, int)

    preset = _render(
        schedule_variables["current_schedule_preset"],
        {"active_slot": active_slot}
        | {f"preset_slot{i}": f"preset{i}" for i in range(1, 5)},
        {},
    )

    assert preset == f"preset{expected_slot}"


def _branch_for_trigger(node, trigger_id):
    """Find the `choose` branch guarded by a given trigger id, at any depth."""
    if isinstance(node, dict):
        conditions = node.get("conditions")
        if isinstance(conditions, list) and any(
            isinstance(c, dict)
            and c.get("condition") == "trigger"
            and trigger_id
            in ([c.get("id")] if isinstance(c.get("id"), str) else (c.get("id") or []))
            for c in conditions
        ):
            return node
        for value in node.values():
            if (found := _branch_for_trigger(value, trigger_id)) is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            if (found := _branch_for_trigger(item, trigger_id)) is not None:
                return found
    return None


def test_pause_off_does_not_resume_while_another_switch_holds_the_pause(
    schedule_variables,
):
    """The trigger fires when *any* selected switch goes off.

    With more than one pause switch, turning one off must not resume the
    schedule while another is still on. The branch therefore has to re-check
    `schedule_paused`, which reads the whole selection.
    """
    blueprint = _load(WEEKLY_SCHEDULE)
    branch = _branch_for_trigger(blueprint["action"], "pause_off")
    assert branch is not None, "no branch is guarded by the pause_off trigger"

    guards = [
        c.get("value_template")
        for c in branch["conditions"]
        if isinstance(c, dict) and c.get("condition") == "template"
    ]
    assert any(g and "not schedule_paused" in g for g in guards), (
        f"pause_off branch does not re-check schedule_paused: {guards}"
    )

    # And the guard has to actually block: one switch off, the other still on.
    still_paused = _render(
        schedule_variables["schedule_paused"],
        {
            "enable_pause_switch": True,
            "pause_switch_entities": ["input_boolean.p", "input_boolean.q"],
        },
        {"input_boolean.p": "off", "input_boolean.q": "on"},
    )

    assert still_paused is True


# ── The device triggers the bundled blueprints are built on ──────────────────
#
# The checks above validate service names and entity ids by hand. That says
# nothing about whether Better Thermostat's own trigger platform accepts the
# trigger a blueprint writes: the blueprint picks a device and a trigger type,
# and the platform's schema is the thing that decides whether the automation
# saves at all.


def _bt_device_triggers(blueprint: dict) -> list[dict]:
    """Return the Better Thermostat device triggers a blueprint declares."""
    triggers = blueprint.get("trigger") or blueprint.get("triggers") or []
    if isinstance(triggers, dict):
        triggers = [triggers]
    return [
        trigger
        for trigger in triggers
        if isinstance(trigger, dict)
        and trigger.get("platform") == "device"
        and trigger.get("domain") == DOMAIN
    ]


BLUEPRINTS_WITH_DEVICE_TRIGGERS = [
    path for path in BLUEPRINT_FILES if _bt_device_triggers(_load(path))
]


def test_some_blueprint_uses_a_device_trigger():
    """Sanity check: the schema test below has blueprints to run against."""
    assert BLUEPRINTS_WITH_DEVICE_TRIGGERS


@pytest.mark.parametrize("path", BLUEPRINTS_WITH_DEVICE_TRIGGERS, ids=lambda p: p.name)
def test_bundled_device_triggers_pass_the_trigger_schema(path):
    """Every device trigger a blueprint declares validates against the platform.

    A blueprint that picks a device but no entity is the shape these are all
    written in, and it has to be a shape the trigger schema accepts — a
    rejected trigger takes the whole automation down at save time, with the
    blueprint itself looking perfectly fine.
    """
    blueprint = _load(path)
    inputs = _resolve_inputs(blueprint)

    for trigger in _bt_device_triggers(blueprint):
        resolved = _substitute(trigger, inputs)
        try:
            TRIGGER_SCHEMA(resolved)
        except vol.Invalid as err:
            raise AssertionError(
                f"{path.name}: trigger {resolved.get('type')} is rejected "
                f"by the platform schema: {err}"
            ) from err
