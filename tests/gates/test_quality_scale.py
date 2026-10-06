"""``quality_scale.yaml`` and the tests that hold its rules stay in step.

The file says which rules of Home Assistant's Integration Quality Scale the
integration meets. A ``done`` with nothing behind it is a claim, so every rule
that is ``done`` or ``todo`` needs a test marked with it, and every marker
needs a rule the file records. Exempt rules carry the reason, and no test.

The markers are read from the source rather than from a pytest collection, so
the check sees the whole suite even when a run selects part of it.
"""

import ast
from collections import defaultdict

from tests.quality_scale import BRONZE_RULES, REPO_ROOT, STATUSES, recorded_rules

TESTS_ROOT = REPO_ROOT / "tests"

# Rules no test can decide, each with what decides it instead.
REVIEWED_BY_HAND = {
    "common-modules": (
        "Shared entity code lives in entity.py; whether a new module belongs "
        "there is a review question."
    )
}


def _marked_rules() -> dict[str, list[str]]:
    """Return each rule a ``quality_rule`` marker names, with where it is."""
    found: dict[str, list[str]] = defaultdict(list)
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "quality_rule"
            ):
                (argument,) = node.args
                assert isinstance(argument, ast.Constant) and isinstance(
                    argument.value, str
                ), f"{path}:{node.lineno}: name the rule as a string literal"
                location = f"{path.relative_to(REPO_ROOT)}:{node.lineno}"
                found[argument.value].append(location)
    return dict(found)


def test_the_file_records_every_bronze_rule_and_nothing_else():
    assert set(recorded_rules()) == BRONZE_RULES


def test_every_rule_has_a_known_status():
    unknown = {
        rule: entry.get("status")
        for rule, entry in recorded_rules().items()
        if entry.get("status") not in STATUSES
    }
    assert not unknown


def test_an_exempt_rule_says_why():
    silent = [
        rule
        for rule, entry in recorded_rules().items()
        if entry["status"] == "exempt" and not entry.get("comment", "").strip()
    ]
    assert not silent


def test_every_marker_names_a_recorded_rule():
    recorded = recorded_rules()
    stray = {
        rule: where for rule, where in _marked_rules().items() if rule not in recorded
    }
    assert not stray


def test_every_rule_to_meet_has_a_test():
    marked = _marked_rules()
    untested = sorted(
        rule
        for rule, entry in recorded_rules().items()
        if entry["status"] in {"done", "todo"}
        and rule not in marked
        and rule not in REVIEWED_BY_HAND
    )
    assert not untested


def test_an_exempt_rule_has_no_test():
    marked = _marked_rules()
    contradicted = {
        rule: marked[rule]
        for rule, entry in recorded_rules().items()
        if entry["status"] == "exempt" and rule in marked
    }
    assert not contradicted


def test_a_rule_reviewed_by_hand_has_no_test():
    """A rule that gained a test has left the hand-reviewed list."""
    assert not set(REVIEWED_BY_HAND) & set(_marked_rules())


def test_a_rule_reviewed_by_hand_is_recorded():
    assert set(REVIEWED_BY_HAND) <= set(recorded_rules())
