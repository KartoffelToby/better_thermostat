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

from pathlib import Path

import homeassistant.helpers.config_validation as cv
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
