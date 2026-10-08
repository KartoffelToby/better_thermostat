"""Tests for the per-file budget of rejected names.

The budget is the thing that notices when a change reintroduces a spelling the
glossary replaced, so the check has to be right about a file that stayed level,
a file that gained one, a file that fell, and a file nobody has budgeted, which
fails on its first finding rather than on its second.

The count underneath those cases has to be right about two more things. It is
taken from identifiers only: a rejected spelling inside a string, a comment or
a docstring is a persisted key or a piece of prose, not a rename that is due,
and counting it would make the budget demand edits that must not happen. And a
test that names the production attribute it asserts on carries no rename of its
own, so it is charged for that spelling only once production has stopped using
it.
"""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_naming.py"

GLOSSARY = textwrap.dedent(
    """
    [[term]]
    name = "config"
    zone = "A"
    definition = "A configuration mapping."
    rejected = ["cfg", "conf"]

    [[term]]
    name = "value"
    zone = "A"
    definition = "A generic value."
    rejected = ["val"]

    [[term]]
    name = "trv.current_temp"
    zone = "A"
    definition = "Room temperature as the TRV reports it."
    rejected = ["current_temperature"]

    [[exception]]
    alias = "current_temperature"
    paths = ["custom_components/climate.py"]
    reason = "The HA property this integration implements."
    """
)

# A word rejected wherever it stands and unit suffixes rejected at the end of a
# name.
PARTS = textwrap.dedent(
    """
    [[word]]
    name = "temperature"
    definition = "Never `temp`."
    rejected = ["temp"]

    [[modifier]]
    name = "_seconds"
    definition = "A duration in seconds."
    rejected = ["_s"]

    [[modifier]]
    name = "_minutes"
    definition = "A duration in minutes."
    rejected = ["_min"]
    """
)

NOTATION_PYPROJECT = textwrap.dedent(
    """
    [tool.better_thermostat.pep8-naming]
    notation-paths = ["custom_components/calibration/**"]
    """
)

# Six spellings of `cfg`, two of them identifiers. The module docstring, the
# function docstring, the comment and the dict key are the other four, and a
# rename may touch none of them.
ONE_IDENTIFIER = textwrap.dedent(
    '''
    """Reads cfg from the entry."""


    def load(cfg):
        """Return the stored cfg."""
        # the cfg key is persisted, so it stays
        return cfg["cfg"]
    '''
)


def _load_script():
    """Import the checker as a module."""
    spec = importlib.util.spec_from_file_location("check_naming", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves a field's module through sys.modules, so the module
    # has to be registered before its body runs.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def checker(tmp_path, monkeypatch):
    """Point the checker at a glossary and a tree inside the test's directory."""
    script = _load_script()
    glossary = tmp_path / "glossary.toml"
    glossary.write_text(GLOSSARY, encoding="utf-8")
    # Both scanned roots exist in the repository, and a root that does not is
    # handed to the parser as if it were a file.
    (tmp_path / "custom_components").mkdir()
    (tmp_path / "tests").mkdir()
    monkeypatch.setattr(script, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(script, "GLOSSARY_FILE", glossary)
    monkeypatch.setattr(script, "BUDGET_FILE", tmp_path / "budget.json")
    monkeypatch.setattr(script, "PYPROJECT_FILE", tmp_path / "pyproject.toml")
    monkeypatch.setattr(script, "SCANNED", ("custom_components", "tests"))
    return script


def _write(script, relative, source):
    """Write a module into the tree the checker scans."""
    path = script.REPO_ROOT / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def _budget(script, **files):
    """Record a budget without measuring."""
    script.BUDGET_FILE.write_text(json.dumps(files), encoding="utf-8")


def test_only_identifiers_are_counted(checker):
    """A rejected spelling in a string, comment or docstring is not a finding."""
    _write(checker, "custom_components/loader.py", ONE_IDENTIFIER)
    findings = checker._findings(None, checker._load_glossary())
    assert ONE_IDENTIFIER.count("cfg") == 6
    assert [(f.alias, f.line) for f in findings] == [("cfg", 5), ("cfg", 8)]


def test_a_leading_underscore_does_not_hide_a_rejected_spelling(checker):
    """A private `_cfg` carries the word `cfg` and is a finding like it."""
    _write(checker, "custom_components/loader.py", "_cfg = 1\n__val = 2\nvalue = 3\n")
    findings = checker._findings(None, checker._load_glossary())
    assert [(f.alias, f.line) for f in findings] == [("_cfg", 1), ("__val", 2)]


def test_an_alias_listed_with_its_underscore_matches_as_written(checker):
    """`_v` is rejected as written; the bare `v` is not."""
    checker.GLOSSARY_FILE.write_text(
        GLOSSARY.replace('rejected = ["val"]', 'rejected = ["val", "_v"]'),
        encoding="utf-8",
    )
    _write(checker, "custom_components/loader.py", "_v = 1\nv = 2\n")
    findings = checker._findings(None, checker._load_glossary())
    assert [(f.alias, f.line) for f in findings] == [("_v", 1)]


def test_check_passes_when_a_file_stays_within_its_budget(checker):
    """A file at its recorded count is not a regression."""
    _write(checker, "custom_components/loader.py", ONE_IDENTIFIER)
    _budget(checker, **{"custom_components/loader.py": 2})
    assert checker.check(None) == 0


def test_check_fails_when_a_file_gains_a_rejected_name(checker, capsys):
    """One more than the budget fails, and the finding is named."""
    _write(checker, "custom_components/loader.py", ONE_IDENTIFIER)
    _budget(checker, **{"custom_components/loader.py": 0})
    assert checker.check(None) == 1
    assert "`cfg` is rejected, use `config`" in capsys.readouterr().out


def test_check_fails_on_the_first_name_in_an_unbudgeted_file(checker, capsys):
    """A file nobody budgeted may not carry a single rejected spelling."""
    _write(checker, "custom_components/loader.py", ONE_IDENTIFIER)
    _budget(checker)
    assert checker.check(None) == 1
    assert "none allowed" in capsys.readouterr().out


# A field production spells the way the glossary rejects, and a test that has
# to say that spelling out loud to assert on the field.
PRODUCTION_FIELD = textwrap.dedent(
    '''
    """A module whose field carries a rejected spelling."""


    class Entry:
        """Hold the field a test has to name in order to read it."""

        def __init__(self, cfg):
            self.cfg = cfg
    '''
)

COVERS_THE_FIELD = textwrap.dedent(
    '''
    """A test that reads the field under the name production gives it."""


    def test_entry_keeps_its_field(entry):
        """Read the field by its production name."""
        assert entry.cfg == {"mode": "heat"}
    '''
)


def test_a_new_test_file_may_name_a_spelling_production_still_carries(checker):
    """A test file nobody budgeted may name the attribute it asserts on."""
    _write(checker, "custom_components/entry.py", PRODUCTION_FIELD)
    _write(checker, "tests/unit/test_entry.py", COVERS_THE_FIELD)
    _budget(checker, **{"custom_components/entry.py": 2})
    assert checker.check(None) == 0


def test_a_test_file_is_charged_for_a_spelling_it_invents(checker, capsys):
    """A name no production site carries is the test's own naming decision."""
    _write(checker, "custom_components/entry.py", PRODUCTION_FIELD)
    _write(checker, "tests/unit/test_entry.py", "def test_read(val):\n    return val\n")
    _budget(checker, **{"custom_components/entry.py": 2})
    assert checker.check(None) == 1
    assert "`val` is rejected, use `value`" in capsys.readouterr().out


def test_a_test_file_is_charged_once_production_drops_the_spelling(checker, capsys):
    """Renaming the last production site is what makes its readers due."""
    _write(checker, "custom_components/entry.py", '"""Renamed already."""\n')
    _write(checker, "tests/unit/test_entry.py", COVERS_THE_FIELD)
    _budget(checker)
    assert checker.check(None) == 1
    assert "tests/unit/test_entry.py:7: `cfg` is rejected" in capsys.readouterr().out


def test_production_is_charged_for_the_spelling_it_carries(checker):
    """The allowance is for readers of a name, not for the name's own tree."""
    _write(checker, "custom_components/entry.py", PRODUCTION_FIELD)
    _budget(checker)
    assert checker.check(None) == 1


def test_update_records_no_entry_for_a_test_file_that_only_mirrors(checker):
    """Nothing is due in it, so it carries no budget to hold."""
    _write(checker, "custom_components/entry.py", PRODUCTION_FIELD)
    _write(checker, "tests/unit/test_entry.py", COVERS_THE_FIELD)
    _budget(checker, **{"custom_components/entry.py": 2})
    assert checker.update(allow_raise=False) == 0
    assert json.loads(checker.BUDGET_FILE.read_text()) == {
        "custom_components/entry.py": 2
    }


def test_an_exception_clears_the_alias_only_where_it_is_listed(checker):
    """The excepted path is silent; another path with the same spelling is not."""
    source = "def read(current_temperature):\n    return current_temperature\n"
    _write(checker, "custom_components/climate.py", f'"""Doc."""\n\n\n{source}')
    _write(checker, "custom_components/other.py", f'"""Doc."""\n\n\n{source}')
    paths = {f.path for f in checker._findings(None, checker._load_glossary())}
    assert paths == {"custom_components/other.py"}


def test_update_refuses_to_record_a_count_that_grew(checker, capsys):
    """A rejected spelling has no legitimate way back into a file."""
    _write(checker, "custom_components/loader.py", ONE_IDENTIFIER)
    _budget(checker, **{"custom_components/loader.py": 0})
    assert checker.update(allow_raise=False) == 1
    assert "refusing to record" in capsys.readouterr().out
    assert json.loads(checker.BUDGET_FILE.read_text()) == {
        "custom_components/loader.py": 0
    }


def test_update_records_a_count_that_fell(checker):
    """After a rename the lower number is the one that has to be held."""
    _write(checker, "custom_components/loader.py", ONE_IDENTIFIER)
    _budget(checker, **{"custom_components/loader.py": 5})
    assert checker.update(allow_raise=False) == 0
    assert json.loads(checker.BUDGET_FILE.read_text()) == {
        "custom_components/loader.py": 2
    }


def test_update_removes_the_budget_once_nothing_is_left(checker, capsys):
    """The budget is scaffolding: at zero it deletes itself."""
    _write(checker, "custom_components/clean.py", '"""Doc."""\n')
    _budget(checker, **{"custom_components/clean.py": 3})
    assert checker.update(allow_raise=False) == 0
    assert not checker.BUDGET_FILE.exists()
    assert "nothing left to hold" in capsys.readouterr().out


def test_update_refuses_a_partial_scan(checker, monkeypatch):
    """A budget written from some of the tree would drop the rest of it."""
    _write(checker, "custom_components/loader.py", ONE_IDENTIFIER)
    monkeypatch.setattr(
        "sys.argv", ["check_naming.py", "update", "custom_components/loader.py"]
    )
    with pytest.raises(SystemExit, match="whole project"):
        checker.main()


def test_a_glossary_that_contradicts_itself_is_refused(checker):
    """A spelling cannot be both an approved term and a rejected one."""
    checker.GLOSSARY_FILE.write_text(
        textwrap.dedent(
            """
            [[term]]
            name = "config"
            zone = "A"
            definition = "A configuration mapping."
            rejected = ["cfg"]

            [[term]]
            name = "cfg"
            zone = "A"
            definition = "The same spelling, approved."
            rejected = []
            """
        ),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="both a term and an alias"):
        checker._load_glossary()


def test_an_exception_for_a_spelling_no_term_rejects_is_refused(checker):
    """An exception that excuses nothing reads as if the checker relied on it."""
    checker.GLOSSARY_FILE.write_text(
        GLOSSARY.replace('rejected = ["current_temperature"]', "rejected = []"),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="no term rejects current_temperature"):
        checker._load_glossary()


def test_an_exception_without_a_reason_is_refused(checker):
    """An unexplained exception makes every other finding less trustworthy."""
    checker.GLOSSARY_FILE.write_text(
        GLOSSARY.replace('reason = "The HA property this integration implements."', ""),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="has no reason"):
        checker._load_glossary()


def test_the_recorded_budget_names_files_that_exist():
    """A moved or deleted file must not keep a budget nobody can spend."""
    script = _load_script()
    if not script.BUDGET_FILE.exists():
        pytest.skip("the backlog is gone and the budget with it")
    budget = json.loads(script.BUDGET_FILE.read_text(encoding="utf-8"))
    missing = [name for name in budget if not (REPO_ROOT / name).exists()]
    assert missing == []


def test_the_repository_stays_within_its_recorded_budget():
    """The committed budget describes the tree it was committed with."""
    script = _load_script()
    assert script.check(None) == 0


def test_check_fails_on_a_budget_that_has_gone_slack(checker, capsys):
    """A rename records the lower number with it, so the slack cannot be spent."""
    _write(checker, "custom_components/loader.py", ONE_IDENTIFIER)
    _budget(checker, **{"custom_components/loader.py": 5})
    assert checker.check(None) == 1
    assert "1 file(s) below budget" in capsys.readouterr().out


def test_a_partial_check_judges_only_the_files_it_scanned(checker, capsys):
    """Checking one file says nothing about the budget of the files left out."""
    scanned = _write(checker, "custom_components/loader.py", ONE_IDENTIFIER)
    _write(checker, "custom_components/other.py", ONE_IDENTIFIER)
    _budget(
        checker, **{"custom_components/loader.py": 2, "custom_components/other.py": 5}
    )

    assert checker.check([scanned]) == 0
    assert "below budget" not in capsys.readouterr().out


def test_a_full_check_fails_on_the_budget_of_a_deleted_file(checker, capsys):
    """A file that is gone holds no names, so its recorded count is slack."""
    _write(checker, "custom_components/loader.py", ONE_IDENTIFIER)
    _budget(
        checker, **{"custom_components/loader.py": 2, "custom_components/gone.py": 3}
    )

    assert checker.check(None) == 1
    assert "1 file(s) below budget" in capsys.readouterr().out


def test_a_partial_check_leaves_the_budget_of_a_deleted_file_alone(checker, capsys):
    """A partial check does not judge a recorded file it did not scan."""
    scanned = _write(checker, "custom_components/loader.py", ONE_IDENTIFIER)
    _budget(
        checker, **{"custom_components/loader.py": 2, "custom_components/gone.py": 3}
    )

    assert checker.check([scanned]) == 0
    assert "below budget" not in capsys.readouterr().out


BINDINGS = textwrap.dedent(
    '''
    """A module that binds rejected spellings without ever reading one."""

    from package import item as cfg
    import package.cfg


    def handle(subject):
        """Bind rejected spellings in every position that is not a read."""
        try:
            pass
        except ValueError as val:
            pass
        match subject:
            case {"item": item, **conf}:
                pass
            case [*current_temperature]:
                pass
    '''
)


@pytest.mark.parametrize(
    ("alias", "position"),
    [
        ("cfg", "an import alias"),
        ("val", "an except clause"),
        ("conf", "a match mapping rest"),
        ("current_temperature", "a match star"),
    ],
)
def test_a_name_bound_without_being_read_is_counted(checker, alias, position):
    """A binding is a rename site even where the name is never read."""
    _write(checker, "custom_components/binder.py", BINDINGS)
    found = {f.alias for f in checker._findings(None, checker._load_glossary())}
    assert alias in found, f"{alias} bound by {position} was not counted"


def test_every_python_source_root_is_scanned():
    """A new file under any tracked source root starts at a budget of zero.

    The roots come from `git ls-files`. A directory walk inside a worktree also
    reaches `.venv` and the checkout's siblings, and filtering those out by path
    component empties the set and leaves the assertion vacuous.
    """
    script = _load_script()
    tracked = subprocess.run(
        ["git", "ls-files", "*.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert tracked, "git reported no Python files"
    roots = {Path(name).parts[0] for name in tracked}
    assert roots <= set(script.SCANNED), sorted(roots - set(script.SCANNED))


def test_the_scan_refuses_a_file_it_could_not_parse(checker):
    """A file the parser cannot read is a stop, not a count of zero."""
    _write(checker, "custom_components/broken.py", "def load(cfg:\n")
    with pytest.raises(SystemExit, match="broken.py"):
        checker._findings(None, checker._load_glossary())


DECLARATIONS = textwrap.dedent(
    '''
    """A module that declares rejected spellings without binding a value."""


    def outer():
        """Declare a rejected spelling that lives at module scope."""
        global cfg


    def load[val]():
        """Take a rejected spelling as a type parameter."""
    '''
)


@pytest.mark.parametrize(
    ("alias", "position"),
    [("cfg", "a global declaration"), ("val", "a type parameter")],
)
def test_a_name_only_declared_is_counted(checker, alias, position):
    """A declaration is a rename site even where no value is bound."""
    _write(checker, "custom_components/declarer.py", DECLARATIONS)
    found = {f.alias for f in checker._findings(None, checker._load_glossary())}
    assert alias in found, f"{alias} in {position} was not counted"


def test_overlapping_roots_scan_each_file_once(checker):
    """A file two roots both name is one file, or its count doubles."""
    _write(checker, "custom_components/loader.py", ONE_IDENTIFIER)
    root = checker.REPO_ROOT / "custom_components"
    overlapping = [root, root / "loader.py"]
    assert len(checker._findings(overlapping, checker._load_glossary())) == 2


def _with_parts(checker, extra: str = "") -> None:
    """Add the word and suffix rules, and *extra*, to the test glossary."""
    checker.GLOSSARY_FILE.write_text(GLOSSARY + PARTS + extra, encoding="utf-8")


def _spelled(checker) -> list[tuple[str, str, str | None]]:
    """Return each finding as its path, its name and the part it spells."""
    return [
        (f.path, f.alias, f.part)
        for f in checker._findings(None, checker._load_glossary())
    ]


def test_a_rejected_word_counts_wherever_it_stands(checker):
    """`temp` is a finding as the first, a middle and the last word of a name."""
    _with_parts(checker)
    _write(
        checker,
        "custom_components/trv.py",
        "temp_to_send = 1\nlast_temp_seen = 2\ntrv_temp = 3\ntemperature = 4\n",
    )
    assert [(name, part) for _, name, part in _spelled(checker)] == [
        ("temp_to_send", "temp"),
        ("last_temp_seen", "temp"),
        ("trv_temp", "temp"),
    ]


def test_a_rejected_suffix_counts_only_at_the_end_of_a_name(checker):
    """`_s` ends `delay_s`; it is no suffix in `s_count` or in a bare `s`."""
    _with_parts(checker)
    _write(
        checker,
        "custom_components/delays.py",
        "delay_s = 1\ns_count = 2\ns = 3\nlocal_calibration_min = 4\n",
    )
    assert [(name, part) for _, name, part in _spelled(checker)] == [
        ("delay_s", "_s"),
        ("local_calibration_min", "_min"),
    ]


def test_a_suffix_after_per_is_the_denominator_of_a_rate(checker):
    """`_kelvin_per_min` spells a rate; its `min` is no duration."""
    _with_parts(checker)
    _write(checker, "custom_components/slope.py", "slope_kelvin_per_min = 1\n")
    assert _spelled(checker) == []


def test_an_upper_case_constant_follows_its_persisted_string(checker):
    """A constant named after a stored key spells that key, not a naming choice."""
    _with_parts(checker)
    _write(checker, "custom_components/const.py", "CONF_MIN_TEMP = 'min_temp'\n")
    assert _spelled(checker) == []


def test_a_finding_names_the_part_and_its_replacement(checker):
    """The report says which word to change and what to write instead."""
    _with_parts(checker)
    _write(checker, "custom_components/trv.py", "trv_temp = 1\n")
    [finding] = checker._findings(None, checker._load_glossary())
    assert str(finding) == (
        "custom_components/trv.py:1: `trv_temp` spells `temp`, use `temperature`"
    )


def test_a_notation_path_keeps_its_unit_suffixes_but_not_its_words(checker):
    """`_s` is an SI symbol the notation writes; `temp` is vocabulary there too."""
    _with_parts(checker)
    (checker.REPO_ROOT / "pyproject.toml").write_text(
        NOTATION_PYPROJECT, encoding="utf-8"
    )
    _write(checker, "custom_components/calibration/pid.py", "dt_s = 1\ninp_temp = 2\n")
    assert [(name, part) for _, name, part in _spelled(checker)] == [
        ("inp_temp", "temp")
    ]


def test_a_reader_of_a_notation_suffix_is_not_charged(checker):
    """A caller naming a notation parameter carries no naming decision of its own.

    The same suffix on a name the notation does not spell is still a finding.
    """
    _with_parts(checker)
    (checker.REPO_ROOT / "pyproject.toml").write_text(
        NOTATION_PYPROJECT, encoding="utf-8"
    )
    _write(checker, "custom_components/calibration/reid.py", "tau_room_min = 1\n")
    _write(
        checker,
        "custom_components/sensor.py",
        "fit(tau_room_min=1)\nduration_min = 2\n",
    )
    assert _spelled(checker) == [
        ("custom_components/sensor.py", "duration_min", "_min")
    ]


def test_a_test_reading_a_production_name_with_a_rejected_part_is_not_charged(checker):
    """The test spells the name production chose, until production renames it."""
    _with_parts(checker)
    _write(checker, "custom_components/trv.py", "trv_temp = 1\n")
    _write(checker, "tests/test_trv.py", "assert trv_temp\nown_temp = 2\n")
    assert [(path, name) for path, name, _ in _spelled(checker)] == [
        ("custom_components/trv.py", "trv_temp"),
        ("tests/test_trv.py", "own_temp"),
    ]


def test_an_exception_clears_a_name_with_a_rejected_part(checker):
    """A name Home Assistant dictates stays where the exception lists it."""
    _with_parts(
        checker,
        textwrap.dedent(
            """
            [[exception]]
            alias = "min_temp"
            paths = ["custom_components/climate.py"]
            reason = "Home Assistant's ClimateEntity property."
            """
        ),
    )
    _write(checker, "custom_components/climate.py", "min_temp = 1\n")
    _write(checker, "custom_components/other.py", "min_temp = 1\n")
    assert [path for path, _, _ in _spelled(checker)] == ["custom_components/other.py"]


def test_an_exception_for_a_name_without_a_rejected_part_is_refused(checker):
    """`minimum` spells no rejected part, so excusing it excuses nothing."""
    _with_parts(
        checker,
        textwrap.dedent(
            """
            [[exception]]
            alias = "minimum"
            paths = ["custom_components/"]
            reason = "Nothing to excuse."
            """
        ),
    )
    with pytest.raises(SystemExit, match="no term rejects minimum"):
        checker._load_glossary()


def test_a_modifier_rejecting_a_spelling_without_underscore_is_refused(checker):
    """A suffix is written with its underscore; anything else is no suffix."""
    checker.GLOSSARY_FILE.write_text(
        GLOSSARY + PARTS.replace('rejected = ["_s"]', 'rejected = ["s"]'),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="which is no suffix"):
        checker._load_glossary()


def test_a_test_binding_a_production_name_with_a_rejected_part_is_charged(checker):
    """A variable, parameter or helper the test names itself is its own decision.

    Only reading the production name is free; binding the same spelling in the
    test counts while production still carries it.
    """
    _with_parts(checker)
    _write(checker, "custom_components/trv.py", "trv_temp = 1\n")
    _write(
        checker,
        "tests/test_trv.py",
        "assert trv_temp\ntrv_temp = 2\n\n\ndef check(trv_temp):\n    return trv_temp\n",
    )
    assert [
        (name, line)
        for f in checker._findings(None, checker._load_glossary())
        if f.path == "tests/test_trv.py"
        for name, line in [(f.alias, f.line)]
    ] == [("trv_temp", 2), ("trv_temp", 5)]


def test_a_declaration_outside_the_notation_of_a_notation_suffix_is_charged(checker):
    """Reading a notation name is free; declaring it again outside is a choice.

    A field of its own named like a notation parameter is judged like any
    other name outside the notation paths.
    """
    _with_parts(checker)
    (checker.REPO_ROOT / "pyproject.toml").write_text(
        NOTATION_PYPROJECT, encoding="utf-8"
    )
    _write(checker, "custom_components/calibration/reid.py", "tau_room_min = 1\n")
    _write(
        checker,
        "custom_components/state.py",
        "class Stored:\n    tau_room_min: float = 0.0\n\n\nprint(Stored().tau_room_min)\n",
    )
    assert _spelled(checker) == [("custom_components/state.py", "tau_room_min", "_min")]
