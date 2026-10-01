"""Guard: a thermostat stand-in cannot invent the state it was never given.

A test that builds its BetterThermostat as a bare ``MagicMock`` gets a
truthy mock for every attribute it forgot. Production then reads a
maintenance run, an open window or a kernel region the test never set up,
and the test passes on a branch it did not mean to exercise.
``tests.factories.ThermostatStandIn`` raises for such a read, and
``make_bt`` builds on it. No test file builds a bare one.
"""

from __future__ import annotations

import ast
from pathlib import Path

_TESTS = Path(__file__).resolve().parents[1]

# The names a test gives its BetterThermostat stand-in.
_STAND_IN_NAMES = frozenset({"bt", "mock_bt", "mock_self"})

_BARE_MOCKS = frozenset(
    {"MagicMock", "Mock", "NonCallableMagicMock", "NonCallableMock"}
)


def _bare_stand_ins(path: Path) -> int:
    """Count ``bt = MagicMock()`` and its spellings in one file."""
    count = 0
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if called not in _BARE_MOCKS:
            continue
        count += sum(
            isinstance(target, ast.Name) and target.id in _STAND_IN_NAMES
            for target in node.targets
        )
    return count


def _suite_counts() -> dict[str, int]:
    """Return the bare stand-in count of every test file that has one."""
    return {
        path.relative_to(_TESTS.parent).as_posix(): found
        for path in sorted(_TESTS.rglob("*.py"))
        if (found := _bare_stand_ins(path))
    }


def test_a_bare_stand_in_is_counted_in_each_spelling(tmp_path):
    """Both mock classes and both import styles count; the strict stand-in does not."""
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "bt = MagicMock()\n"
        "mock_bt = mock.Mock()\n"
        "mock_self = Mock()\n"
        "bt = ThermostatStandIn()\n"
        "trv = MagicMock()\n",
        encoding="utf-8",
    )

    assert _bare_stand_ins(probe) == 3


def test_no_file_builds_a_bare_stand_in():
    """Every thermostat stand-in in the suite is the strict one."""
    bare = _suite_counts()

    assert not bare, (
        f"build these on tests.factories.ThermostatStandIn or make_bt: {bare}"
    )
