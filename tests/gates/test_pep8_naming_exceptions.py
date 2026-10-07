"""Tests for the check that holds PEP 8 naming to the declared exceptions.

A name either follows PEP 8 or is an exception `pyproject.toml` declares, so
the check has to be right about both halves: a declared exception costs
nothing where it is declared and counts everywhere else, and nothing else
hides a name. The scan is taken with the repository's lint settings ignored
and with `noqa` overridden, so neither a `per-file-ignores` glob nor an inline
directive passes a name the declarations do not cover.
"""

from collections import Counter
import importlib.util
from pathlib import Path
import subprocess
import textwrap

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "pep8_naming_exceptions.py"

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

    [tool.better_thermostat.pep8-naming]
    notation-paths = ["custom_components/better_thermostat/utils/calibration/**"]
    module-name-paths = ["custom_components/better_thermostat/model_fixes/**"]
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
    """Import the check as a module."""
    spec = importlib.util.spec_from_file_location("pep8_naming_exceptions", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def script():
    """Return a fresh copy of the check."""
    return _load_script()


def _repository(root: Path, monkeypatch, script, config: str, **files: str) -> None:
    """Point the check at *root*, holding *config* and *files*."""
    (root / "pyproject.toml").write_text(config, encoding="utf-8")
    for name, source in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(source, encoding="utf-8")
    monkeypatch.setattr(script, "REPO_ROOT", root)
    monkeypatch.setattr(script, "_python_files", lambda: sorted(files))


def _per_file(script) -> Counter[str]:
    """Return the number of uncovered findings per file."""
    return Counter(line.split(":", 1)[0] for line in script.findings())


def test_the_scan_counts_a_finding_the_lint_settings_hide(
    script, monkeypatch, tmp_path
):
    """No lint setting and no `noqa` passes a name: only a declaration does.

    The module below is silenced by `per-file-ignores`, by
    `extend-per-file-ignores`, by the `ignore` list, by `exclude`, by
    `extend-exclude` and by a `noqa` comment, all at once, and is still counted.
    """
    (tmp_path / ".gitignore").write_text("hidden.py\n", encoding="utf-8")
    _repository(
        tmp_path,
        monkeypatch,
        script,
        SUPPRESSING_CONFIG,
        **{"hidden.py": UPPERCASE_LOCAL},
    )

    assert _per_file(script) == {"hidden.py": 1}


def test_the_scan_refuses_a_file_it_could_not_parse(script, monkeypatch, tmp_path):
    """A file ruff cannot read reports nothing, which must not pass as clean.

    The grammar is the one `pyproject.toml` names, so aiming the scan at an
    older `target-version` than the sources are written in is what makes ruff
    report a syntax error where the module holds a name to report.
    """
    _repository(
        tmp_path,
        monkeypatch,
        script,
        '[tool.ruff]\ntarget-version = "py310"\n',
        **{"newer.py": NEWER_GRAMMAR},
    )

    with pytest.raises(SystemExit) as refused:
        script.findings()

    assert "newer.py" in str(refused.value)


def test_the_scan_reads_the_files_it_was_given(script, monkeypatch, tmp_path):
    """The scan reads a list, not a directory, so no walk decides what it sees."""
    (tmp_path / "unlisted.py").write_text(UPPERCASE_LOCAL, encoding="utf-8")
    _repository(
        tmp_path,
        monkeypatch,
        script,
        SUPPRESSING_CONFIG,
        **{"listed.py": UPPERCASE_LOCAL},
    )

    assert _per_file(script) == {"listed.py": 1}


def test_the_notation_is_accepted_on_a_notation_path(script, monkeypatch, tmp_path):
    """A name the patterns describe costs nothing where the notation is declared."""
    _repository(
        tmp_path, monkeypatch, script, NOTATION_CONFIG, **{ZONED: NOTATION_MODULE}
    )

    assert _per_file(script) == {}


def test_the_notation_counts_off_the_notation_paths(script, monkeypatch, tmp_path):
    """The patterns apply to every path for ruff, and to the declared ones here."""
    _repository(
        tmp_path, monkeypatch, script, NOTATION_CONFIG, **{UNZONED: NOTATION_MODULE}
    )

    assert _per_file(script) == {UNZONED: 4}


def test_a_celsius_suffix_counts_on_a_notation_path(script, monkeypatch, tmp_path):
    """`C` is no notation: an absolute temperature carries no suffix at all."""
    _repository(
        tmp_path, monkeypatch, script, NOTATION_CONFIG, **{ZONED: CELSIUS_MODULE}
    )

    assert _per_file(script) == {ZONED: 2}


def test_a_model_string_module_name_is_accepted_only_on_a_module_name_path(
    script, monkeypatch, tmp_path
):
    """N999 is the model string's due under `model_fixes` and a finding elsewhere.

    Ruff names an invalid module name only inside a package, so both
    directories carry an `__init__.py`.
    """
    _repository(
        tmp_path,
        monkeypatch,
        script,
        NOTATION_CONFIG,
        **{
            f"{package}/{module}": source
            for package in (MODEL_FIXES, OTHER_PACKAGE)
            for module, source in (
                ("__init__.py", ""),
                ("BHT-002-GCLZB.py", "value = 1\n"),
            )
        },
    )

    assert _per_file(script) == {f"{OTHER_PACKAGE}/BHT-002-GCLZB.py": 1}


def test_a_finding_names_its_file_line_and_rule(script, monkeypatch, tmp_path):
    """The report says where to look, so a name can be fixed without a rescan."""
    _repository(
        tmp_path, monkeypatch, script, NOTATION_CONFIG, **{UNZONED: NOTATION_MODULE}
    )

    assert f"{UNZONED}:5: N803 Argument name `T_room` should be lowercase" in (
        script.findings()
    )


def test_check_fails_on_a_single_uncovered_finding(
    script, monkeypatch, tmp_path, capsys
):
    """One name no exception covers is enough: there is no budget to spend."""
    _repository(
        tmp_path,
        monkeypatch,
        script,
        SUPPRESSING_CONFIG,
        **{"hidden.py": UPPERCASE_LOCAL},
    )

    assert script.check() == 1
    assert "hidden.py" in capsys.readouterr().out


def test_check_passes_when_every_name_is_covered(script, monkeypatch, tmp_path):
    """A tree whose only deviations are declared ones is clean."""
    _repository(
        tmp_path, monkeypatch, script, NOTATION_CONFIG, **{ZONED: NOTATION_MODULE}
    )

    assert script.check() == 0


def test_a_malformed_declaration_is_refused(script, monkeypatch, tmp_path):
    """A declaration the check cannot read must not read as no exception at all."""
    _repository(
        tmp_path,
        monkeypatch,
        script,
        '[tool.better_thermostat.pep8-naming]\nnotation-paths = "tests/**"\n',
        **{"clean.py": "value = 1\n"},
    )

    with pytest.raises(SystemExit) as refused:
        script.findings()

    assert "notation-paths" in str(refused.value)


def test_the_listed_files_are_the_ones_git_tracks(script):
    """That list is the repository's Python files as git records them."""
    files = script._python_files()

    assert "scripts/pep8_naming_exceptions.py" in files
    assert [name for name in files if not name.endswith(".py")] == []


def test_the_module_name_exception_matches_the_ruff_setting(script):
    """Ruff and the check accept N999 under the same paths, and only N999 there.

    Ruff needs a `per-file-ignores` entry to pass the model-string module names
    at all; the check reads `module-name-paths`. Two lists for one exception
    drift apart unless something compares them.
    """
    config = script._config()
    ignores = config["tool"]["ruff"]["lint"]["per-file-ignores"]
    ruff_paths = sorted(
        glob
        for glob, rules in ignores.items()
        if any(rule.startswith("N") for rule in rules)
    )

    assert ruff_paths == sorted(script._exception_paths(config, "module-name-paths"))
    assert all(ignores[glob] == ["N999"] for glob in ruff_paths)


def test_every_declared_path_matches_a_file(script):
    """A declared path no file lies on is an exception nobody can spend."""
    config = script._config()
    files = script._python_files()
    declared = [
        *script._exception_paths(config, "notation-paths"),
        *script._exception_paths(config, "module-name-paths"),
    ]

    unmatched = [
        glob for glob in declared if not any(script._in_any(f, [glob]) for f in files)
    ]

    assert not unmatched, f"declared paths no file lies on: {unmatched}"


def test_the_repository_has_no_uncovered_naming_finding():
    """The working tree holds against a real scan."""
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


def test_a_file_git_does_not_track_yet_is_scanned(script, monkeypatch, tmp_path):
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
    monkeypatch.setattr(script, "REPO_ROOT", tmp_path / "repo")

    listed = set(script._python_files())

    assert listed == {"test_tracked.py", "test_new.py"}
