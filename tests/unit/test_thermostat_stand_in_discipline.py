"""Guard: a thermostat stand-in cannot invent the state it was never given.

A test that builds its BetterThermostat as a bare ``MagicMock`` gets a
truthy mock for every attribute it forgot. Production then reads a
maintenance run, an open window or a kernel region the test never set up,
and the test passes on a branch it did not mean to exercise.
``tests.factories.ThermostatStandIn`` raises for such a read, and
``make_bt`` builds on it.

The files below still build bare mocks. Each count may only fall: a new
bare stand-in fails here, and a file that moved to the strict stand-in
has to leave the list so it cannot fall back.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

_TESTS = Path(__file__).resolve().parents[1]

# The names a test gives its BetterThermostat stand-in.
_STAND_IN_NAMES = frozenset({"bt", "mock_bt", "mock_self"})

_BARE_MOCKS = frozenset(
    {"MagicMock", "Mock", "NonCallableMagicMock", "NonCallableMock"}
)

# Bare stand-ins per file, as they stand. Lower a count, never raise it.
_REMAINING = {
    "tests/test_aggressive_calibration.py": 1,
    "tests/test_calibration_default_mode.py": 1,

    "tests/unit/test_calibration_cooling_gates.py": 1,
    "tests/unit/test_calibration_heating_power.py": 1,
    "tests/unit/test_calibration_mode_default.py": 1,
    "tests/unit/test_calibration_mpc_state_manager.py": 1,
    "tests/unit/test_calibration_pid_state_manager.py": 1,
    "tests/unit/test_calibration_sensor_fallback.py": 1,
    "tests/unit/test_calibration_tpi_state_manager.py": 1,
    "tests/unit/test_calibrator_strategy.py": 3,
    "tests/unit/test_standby_contract.py": 1,
}


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
    counts: Counter[str] = Counter()
    for path in sorted(_TESTS.rglob("*.py")):
        if found := _bare_stand_ins(path):
            counts[path.relative_to(_TESTS.parent).as_posix()] = found
    return dict(counts)


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


def test_no_file_adds_a_bare_stand_in():
    """No file builds more bare stand-ins than it is recorded with."""
    over = {
        path: count
        for path, count in _suite_counts().items()
        if count > _REMAINING.get(path, 0)
    }

    assert not over, (
        f"build these on tests.factories.ThermostatStandIn or make_bt: {over}"
    )


def test_the_recorded_counts_are_current():
    """A file that dropped bare stand-ins records the lower count."""
    counts = _suite_counts()
    stale = {
        path: (recorded, counts.get(path, 0))
        for path, recorded in _REMAINING.items()
        if counts.get(path, 0) < recorded
    }

    assert not stale, f"lower these counts in _REMAINING (recorded, now): {stale}"
