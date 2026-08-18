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

    Home Assistant renders a `variables:` entry and then runs `literal_eval`
    over the result (`homeassistant.helpers.template._parse_result`), keeping
    the raw string when that fails. A branch rendering the bare word `false`
    therefore yields the *string* `"false"`, which is truthy -- so the value a
    template produces has to be a Python literal, not just look like one.
    """
    env = jinja2.Environment()
    env.globals["states"] = lambda entity: states.get(entity, "unknown")
    env.globals["is_state"] = lambda entity, value: states.get(entity) == value
    rendered = env.from_string(template_text).render(**context)
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
