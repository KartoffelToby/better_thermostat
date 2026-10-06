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

import pytest

from tests import quality_scale
from tests.conftest import pytest_collection_modifyitems
from tests.quality_scale import (
    BRONZE_RULES,
    REPO_ROOT,
    SILVER_RULES,
    STATUSES,
    recorded_rules,
)

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


def test_the_file_records_every_bronze_and_silver_rule_and_nothing_else():
    assert set(recorded_rules()) == BRONZE_RULES | SILVER_RULES


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


class _MarkedTest:
    """A collected test carrying ``quality_rule`` markers, as the hook sees it."""

    def __init__(self, *rules: str) -> None:
        self.nodeid = "test_marked"
        self.rules = rules
        self.added: list[pytest.MarkDecorator] = []

    def iter_markers(self, name: str):
        assert name == "quality_rule"
        return [getattr(pytest.mark, name)(rule).mark for rule in self.rules]

    def add_marker(self, marker: pytest.MarkDecorator) -> None:
        self.added.append(marker)


@pytest.fixture
def statuses(monkeypatch):
    """Record the rule statuses the collection hook reads."""
    recorded: dict[str, str] = {}
    monkeypatch.setattr(quality_scale, "rule_status", recorded.__getitem__)
    return recorded


def test_a_todo_rule_runs_its_test_as_strict_xfail(statuses):
    statuses["brands"] = "todo"
    item = _MarkedTest("brands")

    pytest_collection_modifyitems(None, [item])

    (marker,) = item.added
    assert marker.name == "xfail"
    assert marker.kwargs["strict"] is True


def test_a_done_rule_runs_its_test_as_it_is(statuses):
    statuses["brands"] = "done"
    item = _MarkedTest("brands")

    pytest_collection_modifyitems(None, [item])

    assert item.added == []


def test_a_test_may_not_hold_a_todo_and_a_done_rule(statuses):
    statuses.update({"brands": "todo", "runtime-data": "done"})
    item = _MarkedTest("brands", "runtime-data")

    with pytest.raises(pytest.UsageError, match="brands"):
        pytest_collection_modifyitems(None, [item])
    assert item.added == []
