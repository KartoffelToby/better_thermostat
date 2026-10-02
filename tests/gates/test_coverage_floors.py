"""Tests for the per-module coverage floors.

The floors are the thing that notices when a change leaves code uncovered, so
the check itself has to be right about four cases: a module that held its
level passes, one that dropped fails, one that vanished from the report fails
too, and one nobody has measured yet does not count as a regression.
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


def test_check_fails_when_a_recorded_module_is_missing_from_the_report(
    floors, tmp_path, capsys
):
    """A floor nothing measures is not being held, so its absence has to fail.

    Passing here would retire a module's floor the moment the module stopped
    being measured, and the run would still report that every floor holds.
    """
    floors.update(_report(tmp_path, **{MODULE: 88.7, OTHER: 93.0}))

    assert floors.check(_report(tmp_path, **{MODULE: 88.7})) == 1

    output = capsys.readouterr().out
    assert OTHER in output
    assert "hold their floor" not in output


def test_the_missing_module_failure_names_the_re_record_path(floors, tmp_path, capsys):
    """Re-recording is the only legitimate exit, so the failure has to name it.

    A module that really was deleted or renamed has to leave the floors file
    somehow; a reader who is not told which command does that will edit the
    file by hand or stop trusting the gate.
    """
    floors.update(_report(tmp_path, **{MODULE: 88.7, OTHER: 93.0}))

    floors.check(_report(tmp_path, **{MODULE: 88.7}))

    assert "coverage_floors.py update" in capsys.readouterr().out


def test_check_counts_every_recorded_floor_when_it_reports_success(
    floors, tmp_path, capsys
):
    """The success line may only be claimed once every recorded floor was measured.

    Its count is the number a reader takes as the size of the guarded set, so
    a run that compared fewer modules than it names is worse than no report.
    """
    floors.update(_report(tmp_path, **{MODULE: 88.7, OTHER: 93.0}))

    assert floors.check(_report(tmp_path, **{MODULE: 90.0, OTHER: 93.0})) == 0

    assert "all 2 modules hold their floor" in capsys.readouterr().out


def test_update_drops_the_floor_of_a_module_the_report_no_longer_covers(
    floors, tmp_path
):
    """Re-recording has to accept the very report the check rejects.

    Both modes read the same report, so a missing module must stay a finding
    of the check alone — if update refused it too, a deleted module would
    leave the floors file unfixable by the tool that owns it.
    """
    floors.update(_report(tmp_path, **{MODULE: 88.7, OTHER: 93.0}))

    assert floors.update(_report(tmp_path, **{MODULE: 88.7})) == 0

    recorded = json.loads(floors.FLOORS_FILE.read_text(encoding="utf-8"))
    assert recorded == {MODULE: 88.7}


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
