"""Tests for the per-module coverage floors.

The floors are the thing that notices when a change leaves code uncovered, so
the check itself has to be right about three cases: a module that held its
level passes, one that dropped fails, and one nobody has measured yet does not
count as a regression.
"""

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "coverage_floors.py"

MODULE = "custom_components/better_thermostat/climate.py"
OTHER = "custom_components/better_thermostat/sensor.py"


def _load_script():
    """Import the floors script as a module."""
    spec = importlib.util.spec_from_file_location("coverage_floors", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def floors(tmp_path, monkeypatch):
    """Point the script at a floors file inside the test's own directory."""
    script = _load_script()
    monkeypatch.setattr(script, "FLOORS_FILE", tmp_path / "floors.json")
    return script


def _report(tmp_path, **modules) -> Path:
    """Write a coverage JSON report holding the given per-module percentages."""
    path = tmp_path / "coverage.json"
    path.write_text(
        json.dumps(
            {
                "files": {
                    name: {"summary": {"percent_covered": percent}}
                    for name, percent in modules.items()
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def test_update_records_a_floor_below_the_measurement(floors, tmp_path):
    """A floor is the measurement rounded down, so it is never unreachable."""
    report = _report(tmp_path, **{MODULE: 88.76})

    assert floors.update(report) == 0

    recorded = json.loads(floors.FLOORS_FILE.read_text(encoding="utf-8"))
    assert recorded == {MODULE: 88.7}


def test_check_passes_when_every_module_holds_its_floor(floors, tmp_path):
    """Coverage at or above the recorded level is what the check is for."""
    floors.update(_report(tmp_path, **{MODULE: 88.7, OTHER: 93.0}))

    assert floors.check(_report(tmp_path, **{MODULE: 88.7, OTHER: 95.0})) == 0


def test_check_fails_when_a_module_drops(floors, tmp_path, capsys):
    """A module below its floor fails the check and is named."""
    floors.update(_report(tmp_path, **{MODULE: 88.7, OTHER: 93.0}))

    assert floors.check(_report(tmp_path, **{MODULE: 88.2, OTHER: 93.0})) == 1

    output = capsys.readouterr().out
    assert MODULE in output
    assert OTHER not in output.split("dropped below")[-1]


def test_a_module_without_a_floor_is_not_a_regression(floors, tmp_path, capsys):
    """A module nobody has measured yet has nothing to fall below.

    Failing on it would make every new file a red build until someone
    re-recorded, which turns the ratchet into a chore rather than a guard.
    """
    floors.update(_report(tmp_path, **{MODULE: 88.7}))

    assert floors.check(_report(tmp_path, **{MODULE: 88.7, OTHER: 12.0})) == 0

    assert "no floor yet" in capsys.readouterr().out


def test_update_names_the_floors_it_lowers(floors, tmp_path, capsys):
    """Re-recording a lower level says so, so it cannot pass unnoticed."""
    floors.update(_report(tmp_path, **{MODULE: 88.7}))

    floors.update(_report(tmp_path, **{MODULE: 70.0}))

    assert "lowered" in capsys.readouterr().out


def test_the_recorded_floors_match_the_modules_that_exist(floors):
    """Every recorded floor names a module that is still in the repository."""
    recorded = json.loads(
        (REPO_ROOT / ".coverage-floors.json").read_text(encoding="utf-8")
    )

    missing = sorted(name for name in recorded if not (REPO_ROOT / name).exists())

    assert not missing, f"floors recorded for modules that are gone: {missing}"
