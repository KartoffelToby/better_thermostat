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
import subprocess
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

ZONED = "custom_components/better_thermostat/utils/calibration/notation.py"
MODEL_FIXES = "custom_components/better_thermostat/model_fixes"
OTHER_PACKAGE = "custom_components/better_thermostat/utils"
UNZONED = "custom_components/better_thermostat/climate_notation.py"

NOTATION_CONFIG = textwrap.dedent(
    """
    [tool.ruff]
    target-version = "py314"

    [tool.ruff.lint.pep8-naming]
    extend-ignore-names = ["[A-Z]", "[A-Z]_*[!C]", "*_[A-BD-Z]"]
    """
)

# One name per pattern: a lone capital, a subscripted capital and a capital
# symbol as a suffix, the last as a unit and as a matrix.
NOTATION_MODULE = textwrap.dedent(
    '''
    """A module in the control-theory notation."""


    def step(T_room: float, error_K: float, kalman_P: float) -> float:
        """Return the next state."""
        A = 0.5
        return A * T_room + error_K + kalman_P
    '''
)

CELSIUS_MODULE = textwrap.dedent(
    '''
    """A module that spells an absolute temperature with a Celsius suffix."""


    def step(T_room_C: float, target_C: float) -> float:
        """Return the gap."""
        return target_C - T_room_C
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


def test_check_fails_until_a_fallen_count_is_recorded(budget, monkeypatch, capsys):
    """A count that fell is held at its new level, not at the old one.

    Renaming is the direction the budget exists to allow, and the lower
    number is recorded with it, so the slack cannot be spent again later.
    """
    _record(budget, monkeypatch, **{FILE: 33, OTHER: 12})
    capsys.readouterr()

    _counts(budget, monkeypatch, **{FILE: 28, OTHER: 0})

    assert budget.check() == 1
    output = capsys.readouterr().out
    assert f"below budget: {FILE} at 28 of 33" in output
    assert f"below budget: {OTHER} at 0 of 12" in output

    budget.update(allow_raise=False)
    capsys.readouterr()
    assert budget.check() == 0


def test_check_fails_on_the_budget_of_a_deleted_file(budget, monkeypatch, capsys):
    """A file that is gone is not measured, so its recorded count is slack."""
    _record(budget, monkeypatch, **{FILE: 28, OTHER: 5})
    capsys.readouterr()

    _counts(budget, monkeypatch, **{FILE: 28})

    assert budget.check() == 1
    assert f"below budget: {OTHER} at 0 of 5" in capsys.readouterr().out


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


def _zoned_repository(root: Path, monkeypatch, budget, **files: str) -> None:
    """Point the scan at *root*, holding *files* and the notation patterns."""
    (root / "pyproject.toml").write_text(NOTATION_CONFIG, encoding="utf-8")
    for name, source in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(source, encoding="utf-8")
    monkeypatch.setattr(budget, "REPO_ROOT", root)
    monkeypatch.setattr(budget, "_python_files", lambda: sorted(files))


def test_the_notation_is_accepted_inside_a_notation_zone(budget, monkeypatch, tmp_path):
    """A name the patterns describe costs nothing where the notation lives."""
    _zoned_repository(tmp_path, monkeypatch, budget, **{ZONED: NOTATION_MODULE})

    assert budget._measure() == {}


def test_the_notation_counts_outside_the_notation_zones(budget, monkeypatch, tmp_path):
    """The patterns apply to every path for ruff, and to the zones alone here."""
    _zoned_repository(tmp_path, monkeypatch, budget, **{UNZONED: NOTATION_MODULE})

    assert budget._measure() == {UNZONED: 4}


def test_a_celsius_suffix_counts_inside_a_notation_zone(budget, monkeypatch, tmp_path):
    """`C` is no notation: an absolute temperature carries no suffix at all."""
    _zoned_repository(tmp_path, monkeypatch, budget, **{ZONED: CELSIUS_MODULE})

    assert budget._measure() == {ZONED: 2}


def test_a_model_string_module_name_is_accepted_only_under_model_fixes(
    budget, monkeypatch, tmp_path
):
    """N999 is the model string's due under `model_fixes` and a finding elsewhere.

    Ruff names an invalid module name only inside a package, so both
    directories carry an `__init__.py`.
    """
    _zoned_repository(
        tmp_path,
        monkeypatch,
        budget,
        **{
            f"{package}/{module}": source
            for package in (MODEL_FIXES, OTHER_PACKAGE)
            for module, source in (
                ("__init__.py", ""),
                ("BHT-002-GCLZB.py", "value = 1\n"),
            )
        },
    )

    assert budget._measure() == {f"{OTHER_PACKAGE}/BHT-002-GCLZB.py": 1}


def test_the_listed_files_are_the_ones_git_tracks(budget):
    """That list is the repository's Python files as git records them."""
    files = budget._python_files()

    assert "scripts/pep8_naming_budget.py" in files
    assert [name for name in files if not name.endswith(".py")] == []


def test_the_recorded_budget_names_files_that_exist(budget):
    """A moved or deleted file must not keep a budget nobody can spend."""
    recorded_file = REPO_ROOT / ".pep8-naming-budget.json"
    recorded = (
        json.loads(recorded_file.read_text(encoding="utf-8"))
        if recorded_file.exists()
        else {}
    )

    missing = sorted(name for name in recorded if not (REPO_ROOT / name).exists())

    assert not missing, f"budget recorded for files that are gone: {missing}"


def test_the_repository_stays_within_its_recorded_budget():
    """The committed counts hold against a real scan of the working tree."""
    assert _load_script().check() == 0


def _repository_with(root: Path, *, tracked: str, untracked: str, ignored: str):
    """Create a git repository holding one file of each standing."""
    root.mkdir(parents=True, exist_ok=True)
    for name in (tracked, untracked, ignored, ".gitignore"):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
    (root / ".gitignore").write_text(f"{ignored}\n", encoding="utf-8")
    for name in (tracked, untracked, ignored):
        (root / name).write_text("value = 1\n", encoding="utf-8")
    subprocess.run(("git", "init", "-q"), cwd=root, check=True)
    subprocess.run(("git", "add", tracked, ".gitignore"), cwd=root, check=True)


def test_a_file_git_does_not_track_yet_is_scanned(budget, monkeypatch, tmp_path):
    """A new file counts before it is added, and an ignored one is left out.

    A local check that skips a file nobody has run ``git add`` on passes on a
    tree CI later fails on. The ignore rules still decide what belongs to the
    repository.
    """
    _repository_with(
        tmp_path / "repo",
        tracked="test_tracked.py",
        untracked="test_new.py",
        ignored="test_ignored.py",
    )
    monkeypatch.setattr(budget, "REPO_ROOT", tmp_path / "repo")

    listed = set(budget._python_files())

    assert listed == {"test_tracked.py", "test_new.py"}
