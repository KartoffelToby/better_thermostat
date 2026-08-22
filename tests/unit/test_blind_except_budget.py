"""Tests for the per-file budget of blind exception handlers.

The budget is the thing that notices when a change adds a handler that swallows
a failure silently, so the check itself has to be right about three cases: a
file that stayed level passes, a file that gained one fails, and a file nobody
has budgeted fails on its first finding rather than on its second.
"""

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "blind_except_budget.py"

FILE = "custom_components/better_thermostat/climate.py"
OTHER = "custom_components/better_thermostat/sensor.py"


def _load_script():
    """Import the budget script as a module."""
    spec = importlib.util.spec_from_file_location("blind_except_budget", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def budget(tmp_path, monkeypatch):
    """Point the script at a budget file inside the test's own directory."""
    script = _load_script()
    monkeypatch.setattr(script, "BUDGET_FILE", tmp_path / "budget.json")
    return script


def _counts(script, monkeypatch, **files):
    """Make the script see the given per-file counts instead of scanning."""
    monkeypatch.setattr(script, "_measure", lambda: dict(files))


def test_update_records_the_counts_it_measured(budget, monkeypatch):
    """The budget is today's count, so it is reached but never exceeded."""
    _counts(budget, monkeypatch, **{FILE: 28, OTHER: 5})

    assert budget.update() == 0

    recorded = json.loads(budget.BUDGET_FILE.read_text(encoding="utf-8"))
    assert recorded == {FILE: 28, OTHER: 5}


def test_check_passes_when_every_file_stays_within_its_budget(budget, monkeypatch):
    """Counts at the recorded level are what the check is for."""
    _counts(budget, monkeypatch, **{FILE: 28, OTHER: 5})
    budget.update()

    assert budget.check() == 0


def test_check_fails_when_a_file_gains_a_handler(budget, monkeypatch, capsys):
    """One more blind handler in a budgeted file fails the check and is named."""
    _counts(budget, monkeypatch, **{FILE: 28, OTHER: 5})
    budget.update()
    capsys.readouterr()

    _counts(budget, monkeypatch, **{FILE: 29, OTHER: 5})

    assert budget.check() == 1
    output = capsys.readouterr().out
    assert f"{FILE}: 29 > 28" in output
    assert OTHER not in output


def test_check_fails_on_the_first_handler_in_an_unbudgeted_file(
    budget, monkeypatch, capsys
):
    """A file with no budget has no allowance, so its first finding fails.

    Adding a file to `per-file-ignores` silences `ruff check` for it but not
    this, so a blind handler cannot be hidden by editing the lint settings.
    """
    _counts(budget, monkeypatch, **{FILE: 28})
    budget.update()
    capsys.readouterr()

    _counts(budget, monkeypatch, **{FILE: 28, OTHER: 1})

    assert budget.check() == 1
    output = capsys.readouterr().out
    assert "no budget" in output
    assert f"{OTHER}: 1" in output


def test_check_passes_when_a_count_falls(budget, monkeypatch, capsys):
    """Converting handlers is the direction the budget exists to allow."""
    _counts(budget, monkeypatch, **{FILE: 28, OTHER: 5})
    budget.update()
    capsys.readouterr()

    _counts(budget, monkeypatch, **{FILE: 24, OTHER: 0})

    assert budget.check() == 0
    output = capsys.readouterr().out
    assert f"below budget: {FILE} at 24 of 28" in output
    assert f"below budget: {OTHER} at 0 of 5" in output


def test_update_names_the_budgets_it_raises(budget, monkeypatch, capsys):
    """Re-recording a higher number says so, so it cannot pass unnoticed."""
    _counts(budget, monkeypatch, **{FILE: 28})
    budget.update()
    capsys.readouterr()

    _counts(budget, monkeypatch, **{FILE: 30})
    budget.update()

    assert f"raised: {FILE} 28 -> 30" in capsys.readouterr().out


def test_the_recorded_budget_names_files_that_exist(budget):
    """Every recorded budget names a file that is still in the repository."""
    recorded = json.loads(
        (REPO_ROOT / ".blind-except-budget.json").read_text(encoding="utf-8")
    )

    missing = sorted(name for name in recorded if not (REPO_ROOT / name).exists())

    assert not missing, f"budget recorded for files that are gone: {missing}"


def test_the_repository_stays_within_its_recorded_budget():
    """The committed counts hold against a real scan of the working tree."""
    assert _load_script().check() == 0
