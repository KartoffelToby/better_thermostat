"""Tests for the per-file budget of blind exception handlers.

The budget is the thing that notices when a change adds a handler that swallows
a failure silently, so the check itself has to be right about three cases: a
file that stayed level passes, a file that gained one fails, and a file nobody
has budgeted fails on its first finding rather than on its second.

The count underneath those cases has to be right about one more thing: it is
taken with the repository's lint settings ignored, so the suppressions that
silence `ruff check` do not lower it.
"""

import importlib.util
import json
from pathlib import Path
import textwrap

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "blind_except_budget.py"

FILE = "custom_components/better_thermostat/climate.py"
OTHER = "custom_components/better_thermostat/sensor.py"

# Every route the lint settings offer to silence BLE001 for one file, at once.
SUPPRESSING_CONFIG = textwrap.dedent(
    """
    [tool.ruff]
    target-version = "py314"
    exclude = ["hidden.py"]
    extend-exclude = ["hidden.py"]

    [tool.ruff.lint]
    ignore = ["BLE001"]

    [tool.ruff.lint.per-file-ignores]
    "hidden.py" = ["BLE001"]

    [tool.ruff.lint.extend-per-file-ignores]
    "hidden.py" = ["BLE001"]
    """
)

BLIND_HANDLER = textwrap.dedent(
    '''
    """A module its own lint settings report nothing about."""


    def swallow() -> None:
        """Swallow whatever goes wrong."""
        try:
            pass
        except Exception:  # noqa: BLE001
            pass
    '''
)

# `except X, Y:` without parentheses is Python 3.14 syntax, so an older grammar
# makes this module a syntax error instead of the one handler it holds.
NEWER_GRAMMAR = textwrap.dedent(
    '''
    """A module in the grammar the repository targets."""


    def swallow() -> None:
        """Swallow whatever goes wrong."""
        try:
            pass
        except ValueError, TypeError:
            pass
        try:
            pass
        except Exception:
            pass
    '''
)


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

    The alternative reading — no entry means no limit — would let a file nobody
    has budgeted collect handlers until someone thought to budget it.
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


def test_the_scan_counts_a_handler_the_lint_settings_hide(
    budget, monkeypatch, tmp_path
):
    """No lint setting lowers the count: that is what the budget rests on.

    The module below is silenced by `per-file-ignores`, by
    `extend-per-file-ignores`, by the `ignore` list, by `exclude`, by
    `extend-exclude` and by a `noqa` comment, all at once, and is still counted.
    """
    (tmp_path / "pyproject.toml").write_text(SUPPRESSING_CONFIG, encoding="utf-8")
    (tmp_path / ".gitignore").write_text("hidden.py\n", encoding="utf-8")
    (tmp_path / "hidden.py").write_text(BLIND_HANDLER, encoding="utf-8")
    monkeypatch.setattr(budget, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(budget, "_python_files", lambda: ["hidden.py"])

    assert budget._measure() == {"hidden.py": 1}


def test_the_scan_refuses_a_file_it_could_not_parse(budget, monkeypatch, tmp_path):
    """A file ruff cannot read reports nothing, which must not pass as zero.

    The grammar is the one `pyproject.toml` names, so aiming the scan at an
    older `target-version` than the sources are written in is what makes ruff
    report nothing about a module that does hold a blind handler.
    """
    (tmp_path / "pyproject.toml").write_text(
        '[tool.ruff]\ntarget-version = "py310"\n', encoding="utf-8"
    )
    (tmp_path / "newer.py").write_text(NEWER_GRAMMAR, encoding="utf-8")
    monkeypatch.setattr(budget, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(budget, "_python_files", lambda: ["newer.py"])

    with pytest.raises(SystemExit) as refused:
        budget._measure()

    assert "newer.py" in str(refused.value)


def test_the_scan_reads_the_files_it_was_given(budget, monkeypatch, tmp_path):
    """The scan reads a list, not a directory, so no walk decides what it sees."""
    (tmp_path / "pyproject.toml").write_text(SUPPRESSING_CONFIG, encoding="utf-8")
    (tmp_path / "listed.py").write_text(BLIND_HANDLER, encoding="utf-8")
    (tmp_path / "unlisted.py").write_text(BLIND_HANDLER, encoding="utf-8")
    monkeypatch.setattr(budget, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(budget, "_python_files", lambda: ["listed.py"])

    assert budget._measure() == {"listed.py": 1}


def test_the_listed_files_are_the_ones_git_tracks(budget):
    """That list is the repository's Python files as git records them."""
    files = budget._python_files()

    assert "scripts/blind_except_budget.py" in files
    assert [name for name in files if not name.endswith(".py")] == []


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
