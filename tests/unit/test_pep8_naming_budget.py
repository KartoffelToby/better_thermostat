"""Tests for the per-file budget of silenced PEP 8 naming findings.

The budget is the thing that notices when a change drops the naming convention
somewhere new, so the check itself has to be right about a file that stayed
level, a file that gained one, a file that fell, and a file nobody has budgeted
— which fails on its first finding rather than on its second.

The count underneath those cases has to be right about one more thing: it is
taken with the repository's lint settings ignored and with `noqa` overridden, so
a `per-file-ignores` glob and an inline directive are worth the same. That is
what lets the narrower of the two be chosen without the number moving.
"""

import importlib.util
import json
from pathlib import Path
import textwrap

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "pep8_naming_budget.py"

FILE = "custom_components/better_thermostat/utils/calibration/mpc.py"
OTHER = "custom_components/better_thermostat/utils/calibration/pid.py"

# Every route the lint settings offer to silence a naming rule for one file, at
# once.
SUPPRESSING_CONFIG = textwrap.dedent(
    """
    [tool.ruff]
    target-version = "py314"
    exclude = ["hidden.py"]
    extend-exclude = ["hidden.py"]

    [tool.ruff.lint]
    ignore = ["N"]

    [tool.ruff.lint.per-file-ignores]
    "hidden.py" = ["N"]

    [tool.ruff.lint.extend-per-file-ignores]
    "hidden.py" = ["N"]
    """
)

UPPERCASE_LOCAL = textwrap.dedent(
    '''
    """A module its own lint settings report nothing about."""


    def compute() -> float:
        """Bind a name the convention would have lowercased."""
        A = 2.0  # noqa: N806
        return A
    '''
)

# `except X, Y:` without parentheses is Python 3.14 syntax, so an older grammar
# makes this module a syntax error rather than the module it is.
NEWER_GRAMMAR = textwrap.dedent(
    '''
    """A module in the grammar the repository targets."""


    def compute() -> float:
        """Bind a name the convention would have lowercased."""
        try:
            pass
        except ValueError, TypeError:
            pass
        A = 2.0
        return A
    '''
)


def _load_script():
    """Import the budget script as a module."""
    spec = importlib.util.spec_from_file_location("pep8_naming_budget", SCRIPT)
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


def _record(script, monkeypatch, **files):
    """Record a budget from the given counts, whatever it was before."""
    _counts(script, monkeypatch, **files)
    return script.update(allow_raise=True)


def test_update_records_the_counts_it_measured(budget, monkeypatch):
    """The recorded number is the count measured, so today's tree is the ceiling."""
    assert _record(budget, monkeypatch, **{FILE: 33, OTHER: 12}) == 0

    recorded = json.loads(budget.BUDGET_FILE.read_text(encoding="utf-8"))
    assert recorded == {FILE: 33, OTHER: 12}


def test_check_passes_when_every_file_stays_within_its_budget(budget, monkeypatch):
    """Counts at the recorded level are what the check is for."""
    _record(budget, monkeypatch, **{FILE: 33, OTHER: 12})

    assert budget.check() == 0


def test_check_fails_when_a_file_gains_a_finding(budget, monkeypatch, capsys):
    """One more silenced name in a budgeted file fails the check and is named."""
    _record(budget, monkeypatch, **{FILE: 33, OTHER: 12})
    capsys.readouterr()

    _counts(budget, monkeypatch, **{FILE: 34, OTHER: 12})

    assert budget.check() == 1
    output = capsys.readouterr().out
    assert f"{FILE}: 34 > 33" in output
    assert OTHER not in output


def test_check_fails_on_the_first_finding_in_an_unbudgeted_file(
    budget, monkeypatch, capsys
):
    """A file with no budget has no allowance, so its first finding fails.

    This is what a glob over a whole tree gives away on its own: the files it
    matches that never tripped the rule are exempt for free, and a new one
    arriving under the glob is exempt before anybody has looked at it.
    """
    _record(budget, monkeypatch, **{FILE: 33})
    capsys.readouterr()

    _counts(budget, monkeypatch, **{FILE: 33, OTHER: 1})

    assert budget.check() == 1
    output = capsys.readouterr().out
    assert "no budget" in output
    assert f"{OTHER}: 1" in output


def test_check_passes_when_a_count_falls(budget, monkeypatch, capsys):
    """Renaming is the direction the budget exists to allow."""
    _record(budget, monkeypatch, **{FILE: 33, OTHER: 12})
    capsys.readouterr()

    _counts(budget, monkeypatch, **{FILE: 28, OTHER: 0})

    assert budget.check() == 0
    output = capsys.readouterr().out
    assert f"below budget: {FILE} at 28 of 33" in output
    assert f"below budget: {OTHER} at 0 of 12" in output


def test_update_refuses_to_record_a_count_that_grew(budget, monkeypatch, capsys):
    """A name that drops the convention does not arrive by accident."""
    _record(budget, monkeypatch, **{FILE: 33})
    capsys.readouterr()

    _counts(budget, monkeypatch, **{FILE: 35})

    assert budget.update(allow_raise=False) == 1
    assert f"raised: {FILE} 33 -> 35" in capsys.readouterr().out
    recorded = json.loads(budget.BUDGET_FILE.read_text(encoding="utf-8"))
    assert recorded == {FILE: 33}


def test_update_records_a_count_that_grew_when_it_is_allowed(budget, monkeypatch):
    """A file that moved took its notation along, which raises no new deviation."""
    _record(budget, monkeypatch, **{FILE: 33})

    assert _record(budget, monkeypatch, **{FILE: 35}) == 0

    recorded = json.loads(budget.BUDGET_FILE.read_text(encoding="utf-8"))
    assert recorded == {FILE: 35}


def test_update_removes_the_budget_once_nothing_is_left(budget, monkeypatch, capsys):
    """The budget is scaffolding: at zero it deletes itself."""
    _record(budget, monkeypatch, **{FILE: 33})
    capsys.readouterr()

    assert _record(budget, monkeypatch) == 0
    assert not budget.BUDGET_FILE.exists()
    assert "removed" in capsys.readouterr().out


def test_the_scan_counts_a_finding_the_lint_settings_hide(
    budget, monkeypatch, tmp_path
):
    """No lint setting lowers the count: that is what the budget rests on.

    The module below is silenced by `per-file-ignores`, by
    `extend-per-file-ignores`, by the `ignore` list, by `exclude`, by
    `extend-exclude` and by a `noqa` comment, all at once, and is still counted.
    """
    (tmp_path / "pyproject.toml").write_text(SUPPRESSING_CONFIG, encoding="utf-8")
    (tmp_path / ".gitignore").write_text("hidden.py\n", encoding="utf-8")
    (tmp_path / "hidden.py").write_text(UPPERCASE_LOCAL, encoding="utf-8")
    monkeypatch.setattr(budget, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(budget, "_python_files", lambda: ["hidden.py"])

    assert budget._measure() == {"hidden.py": 1}


def test_the_scan_refuses_a_file_it_could_not_parse(budget, monkeypatch, tmp_path):
    """A file ruff cannot read reports nothing, which must not pass as zero.

    The grammar is the one `pyproject.toml` names, so aiming the scan at an
    older `target-version` than the sources are written in is what makes ruff
    report a syntax error where the module holds a name to count.
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
    (tmp_path / "listed.py").write_text(UPPERCASE_LOCAL, encoding="utf-8")
    (tmp_path / "unlisted.py").write_text(UPPERCASE_LOCAL, encoding="utf-8")
    monkeypatch.setattr(budget, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(budget, "_python_files", lambda: ["listed.py"])

    assert budget._measure() == {"listed.py": 1}


def test_the_listed_files_are_the_ones_git_tracks(budget):
    """That list is the repository's Python files as git records them."""
    files = budget._python_files()

    assert "scripts/pep8_naming_budget.py" in files
    assert [name for name in files if not name.endswith(".py")] == []


def test_the_recorded_budget_names_files_that_exist(budget):
    """A moved or deleted file must not keep a budget nobody can spend."""
    recorded = json.loads(
        (REPO_ROOT / ".pep8-naming-budget.json").read_text(encoding="utf-8")
    )

    missing = sorted(name for name in recorded if not (REPO_ROOT / name).exists())

    assert not missing, f"budget recorded for files that are gone: {missing}"


def test_the_repository_stays_within_its_recorded_budget():
    """The committed counts hold against a real scan of the working tree."""
    assert _load_script().check() == 0
