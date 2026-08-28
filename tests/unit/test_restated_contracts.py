"""Tests for the list of test docstrings that restate the code.

The list is a reading aid, so what has to be right about it is the reading:
which sentence it picks up and which it leaves alone. A scan that fired on
every "never" would bury the question it exists to ask, and one that read the
explanatory paragraphs under a summary would fire on preconditions rather than
on obligations.

Underneath that sits the budget, which is the part that keeps the list from
growing back while it is being worked off, and which therefore has to be right
about a file that stayed level, a file that gained one, a file that fell, and a
file nobody has budgeted.
"""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "restated_contracts.py"

FILE = "tests/unit/test_temperature_events.py"
OTHER = "tests/unit/test_trv_events.py"


def _load_script():
    """Import the candidate list script as a module."""
    spec = importlib.util.spec_from_file_location("restated_contracts", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves a field's module through sys.modules, so the module
    # has to be registered before its body runs.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def script(tmp_path, monkeypatch):
    """Point the script at a tree and a budget file of the test's own."""
    module = _load_script()
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(module, "BUDGET_FILE", tmp_path / "budget.json")
    return module


def _module(tmp_path, source: str, name: str = "test_sample.py") -> Path:
    """Write a test module into the temporary tree and return its path."""
    path = tmp_path / name
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return path


def _quotes(findings) -> list[str]:
    """Return the quoted summaries of a scan result."""
    return [finding.quote for finding in findings]


# ---------------------------------------------------------------------------
# What the scan picks up
# ---------------------------------------------------------------------------


def test_a_summary_repeating_a_condition_is_picked_up(script, tmp_path):
    """A summary shaped like the code's own condition is a candidate."""
    path = _module(
        tmp_path,
        '''
        def test_debounce():
            """If ANY TRV is HomematicIP, 600s debounce applies."""
        ''',
    )

    findings = script._scan_module(path)

    assert _quotes(findings) == ["If ANY TRV is HomematicIP, 600s debounce applies."]
    assert findings[0].marker == "shouted quantifier"


def test_a_summary_that_states_an_obligation_is_left_alone(script, tmp_path):
    """A requirement in plain words is not a candidate."""
    path = _module(
        tmp_path,
        '''
        def test_debounce():
            """Accept a room sensor reading once its own interval elapsed."""
        ''',
    )

    assert script._scan_module(path) == []


def test_only_the_summary_paragraph_is_read(script, tmp_path):
    """Wording below the summary describes the setup, not the contract.

    The paragraphs under a summary say what the fixture arranges, so a
    quantifier down there qualifies a precondition. Reading them would report
    the setup of a test as its contract.
    """
    path = _module(
        tmp_path,
        '''
        def test_pending():
            """Report a sensor that has not come up yet.

            The entry is built with a sensor that always stays unavailable.
            """
        ''',
    )

    assert script._scan_module(path) == []


def test_a_helper_is_not_a_contract(script, tmp_path):
    """Only functions named as tests carry a contract to ask about."""
    path = _module(
        tmp_path,
        '''
        def build_entry():
            """The entry is always built with one head."""
        ''',
    )

    assert script._scan_module(path) == []


def test_the_docstring_comes_from_the_tree_not_from_the_text(script, tmp_path):
    """A comment or a plain string carrying the wording is not a docstring.

    The scan parses the module, so what it reads is the docstring the function
    actually has. A text search would count the two lines below as well.
    """
    path = _module(
        tmp_path,
        '''
        def test_written_out():
            """Write the setpoint the user chose."""
            # If ANY TRV is HomematicIP the interval grows.
            note = "The write always happens."
            return note
        ''',
    )

    assert script._scan_module(path) == []


def test_one_question_per_test(script, tmp_path):
    """A summary matching twice is still one sentence to read."""
    path = _module(
        tmp_path,
        '''
        def test_two_wordings():
            """If ANY head is slow the write is always delayed."""
        ''',
    )

    assert len(script._scan_module(path)) == 1


def test_a_written_out_value_is_not_a_shouted_quantifier(script, tmp_path):
    """A summary naming the NONE preset raises no question about its contract.

    `NONE` is the spelling of a value here, not a quantifier somebody shouted,
    and it appears in capitals all over the suite. Counting it would fill the
    list with preset transitions.
    """
    path = _module(
        tmp_path,
        '''
        def test_preset_cleared():
            """Comfort to NONE restores the target the user had set."""
        ''',
    )

    assert script._scan_module(path) == []


def test_a_summary_spelling_the_source_is_marked_as_the_louder_half(script, tmp_path):
    """A summary reaching for an identifier is where reading starts."""
    path = _module(
        tmp_path,
        '''
        def test_flag():
            """Test that system_mode_received flag is always set to True."""
        ''',
    )

    findings = script._scan_module(path)

    assert [f.code_shaped for f in findings] == [True]


def test_a_summary_in_plain_words_is_not_marked_as_the_louder_half(script, tmp_path):
    """A candidate phrased in domain words is the quieter half of the list."""
    path = _module(
        tmp_path,
        '''
        def test_floor():
            """The heat target stays above the configured minimum, always."""
        ''',
    )

    findings = script._scan_module(path)

    assert [f.code_shaped for f in findings] == [False]


def test_an_interval_copied_from_the_source_is_a_candidate(script, tmp_path):
    """A summary carrying a bare count of seconds names the source's number.

    Unlike a quantifier, a number of seconds makes no claim of its own. It has
    no meaning away from the constant it was copied from, so it is a candidate
    on its own rather than only next to another wording.
    """
    path = _module(
        tmp_path,
        '''
        def test_debounce():
            """Reject a change inside the 600s debounce window."""
        ''',
    )

    findings = script._scan_module(path)

    assert [f.marker for f in findings] == ["copied interval"]


def test_a_summary_wrapped_across_lines_is_read_whole(script, tmp_path):
    """A summary too long for one line is still one sentence to read.

    The contract ends at the blank line, not at the first newline, so a
    wording carried onto the second line of the same paragraph belongs to the
    obligation and has to be picked up.
    """
    path = _module(
        tmp_path,
        '''
        def test_wrapped():
            """Report the head that came up late,
            whatever the group always did before it.
            """
        ''',
    )

    assert script._scan_module(path) != []


def test_a_module_that_cannot_be_parsed_stops_the_run(script, tmp_path):
    """A module counted as empty because it would not parse reads as clean."""
    path = _module(tmp_path, "def test_broken(:\n")

    with pytest.raises(SystemExit) as exit_info:
        script._scan_module(path)

    assert "nothing was counted" in str(exit_info.value)


# ---------------------------------------------------------------------------
# The symptom form
# ---------------------------------------------------------------------------


def test_a_summary_repeating_a_reported_symptom_is_picked_up(script, tmp_path):
    """A docstring quoting a reported symptom pins what a user complained of."""
    symptoms = tmp_path / "symptoms.md"
    symptoms.write_text(
        'The heads report "the valve stays fully open" after a restart.\n',
        encoding="utf-8",
    )
    path = _module(
        tmp_path,
        '''
        def test_restart():
            """After a restart the valve stays fully open."""
        ''',
    )

    phrases = script._symptom_phrases(symptoms)
    findings = script._scan_symptoms([path], phrases)

    assert _quotes(findings) == ["the valve stays fully open"]


def test_a_two_word_span_is_a_turn_of_phrase(script, tmp_path):
    """A span too short to identify a report is not read as one."""
    symptoms = tmp_path / "symptoms.md"
    symptoms.write_text(
        'The report says "the valve" over and over.\n', encoding="utf-8"
    )

    assert script._symptom_phrases(symptoms) == []


def test_a_three_word_span_identifies_a_report(script, tmp_path):
    """A span long enough to name a symptom is read as one.

    The lower bound exists to drop turns of phrase, so it has to keep the
    shortest span that still identifies what a user reported.
    """
    symptoms = tmp_path / "symptoms.md"
    symptoms.write_text(
        'The report says "the valve sticks" over and over.\n', encoding="utf-8"
    )

    assert script._symptom_phrases(symptoms) == ["the valve sticks"]


def test_a_module_that_cannot_be_parsed_stops_the_symptom_scan(script, tmp_path):
    """A module counted as empty because it would not parse reads as clean.

    A skipped module and a module with nothing to report give the same answer,
    and no caller can tell them apart. The symptom scan therefore ends the run
    on the same input the wording scan ends on.
    """
    path = _module(tmp_path, "def test_broken(:\n")

    with pytest.raises(SystemExit) as exit_info:
        script._scan_symptoms([path], ["the valve stays fully open"])

    assert "nothing was counted" in str(exit_info.value)


# ---------------------------------------------------------------------------
# Which files are scanned
# ---------------------------------------------------------------------------


def test_only_python_modules_are_scanned(script, monkeypatch):
    """Tracked files that are not Python carry no docstrings to parse.

    The tree holds documents next to the tests. Handing one to the parser
    ends the run on a file that never had a contract to state.
    """
    listing = subprocess.CompletedProcess(
        args=(), returncode=0, stdout="tests/test_a.py\0tests/DESIGN.md\0tests/x\0"
    )
    monkeypatch.setattr(script.subprocess, "run", lambda *a, **kw: listing)

    assert script._test_files(None) == [script.REPO_ROOT / "tests/test_a.py"]


# ---------------------------------------------------------------------------
# The budget
# ---------------------------------------------------------------------------


def _counts(script, monkeypatch, **files):
    """Make the script see the given per-file counts instead of scanning."""
    monkeypatch.setattr(script, "_measure", lambda: dict(files))


def _tree(script, monkeypatch, tmp_path):
    """Write a tree holding two candidates in one module and one in another."""
    first = _module(
        tmp_path,
        '''
        def test_one():
            """The write always happens."""

        def test_two():
            """The head never reports back."""
        ''',
        name="test_first.py",
    )
    second = _module(
        tmp_path,
        '''
        def test_three():
            """A stale reading is dropped regardless of the mode."""
        ''',
        name="test_second.py",
    )
    monkeypatch.setattr(script, "_test_files", lambda paths: [first, second])
    return "test_first.py", "test_second.py"


def test_every_candidate_in_a_file_is_counted(script, monkeypatch, tmp_path):
    """A file's number is how many candidates it holds, not whether it holds any.

    The budget can only fall towards zero if it counts each summary. A number
    that said "this file has some" would let a file work off every candidate
    but one and still read as unchanged.
    """
    first, second = _tree(script, monkeypatch, tmp_path)

    assert script._measure() == {first: 2, second: 1}


def test_check_measures_the_tree_it_is_run_against(script, monkeypatch, tmp_path):
    """The check compares the budget against today's tree, not a stored count.

    A budget checked against anything but a fresh scan would pass on a file
    that has gained a restatement since it was recorded.
    """
    first, second = _tree(script, monkeypatch, tmp_path)
    script.BUDGET_FILE.write_text(json.dumps({first: 1, second: 1}), encoding="utf-8")

    assert script.check() == 1


def test_update_records_the_counts_it_measured(script, monkeypatch):
    """The budget is today's count, so it is the number that has to hold."""
    _counts(script, monkeypatch, **{FILE: 3, OTHER: 1})

    assert script.update() == 0

    recorded = json.loads(script.BUDGET_FILE.read_text(encoding="utf-8"))
    assert recorded == {FILE: 3, OTHER: 1}


def test_check_passes_when_every_file_stays_within_its_budget(script, monkeypatch):
    """Counts at the recorded level are what the check is for."""
    _counts(script, monkeypatch, **{FILE: 3, OTHER: 1})
    script.update()

    assert script.check() == 0


def test_check_fails_when_a_file_gains_a_docstring(script, monkeypatch, capsys):
    """One more restating summary fails the check and is named."""
    _counts(script, monkeypatch, **{FILE: 3, OTHER: 1})
    script.update()
    capsys.readouterr()

    _counts(script, monkeypatch, **{FILE: 4, OTHER: 1})

    assert script.check() == 1
    output = capsys.readouterr().out
    assert f"{FILE}: 4 > 3" in output
    assert OTHER not in output


def test_check_fails_on_the_first_finding_in_an_unbudgeted_file(
    script, monkeypatch, capsys
):
    """A file with no budget has no allowance, so its first finding fails.

    The other reading — no entry means no limit — would let a file nobody has
    budgeted collect restatements until somebody thought to budget it.
    """
    _counts(script, monkeypatch, **{FILE: 3})
    script.update()
    capsys.readouterr()

    _counts(script, monkeypatch, **{FILE: 3, OTHER: 1})

    assert script.check() == 1
    output = capsys.readouterr().out
    assert "no budget" in output
    assert f"{OTHER}: 1" in output


def test_check_passes_when_a_count_falls(script, monkeypatch, capsys):
    """Rewording a docstring is the direction the budget exists to allow."""
    _counts(script, monkeypatch, **{FILE: 3, OTHER: 1})
    script.update()
    capsys.readouterr()

    _counts(script, monkeypatch, **{FILE: 1, OTHER: 0})

    assert script.check() == 0
    output = capsys.readouterr().out
    assert f"below budget: {FILE} at 1 of 3" in output
    assert f"below budget: {OTHER} at 0 of 1" in output


def test_update_names_the_budgets_it_raises(script, monkeypatch, capsys):
    """Re-recording a higher number says so, so it cannot pass unnoticed."""
    _counts(script, monkeypatch, **{FILE: 3})
    script.update()
    capsys.readouterr()

    _counts(script, monkeypatch, **{FILE: 5})
    script.update()

    assert f"raised: {FILE} 3 -> 5" in capsys.readouterr().out


def test_check_refuses_to_run_before_a_budget_exists(script, monkeypatch):
    """With no budget recorded there is nothing to compare against."""
    _counts(script, monkeypatch, **{FILE: 3})

    with pytest.raises(SystemExit) as exit_info:
        script.check()

    assert "no budget recorded" in str(exit_info.value)


# ---------------------------------------------------------------------------
# The modes
# ---------------------------------------------------------------------------


def test_list_prints_the_candidates_it_found(script, monkeypatch, tmp_path, capsys):
    """The list mode names each candidate, because reading them is its purpose.

    A count alone says how much is left but not which sentence to go and read,
    which is the whole of what this mode is for.
    """
    _tree(script, monkeypatch, tmp_path)

    assert script.show(None, None) == 0

    output = capsys.readouterr().out
    assert "test_first.py:2: test_one" in output
    assert "test_second.py:2: test_three" in output
    assert "3 docstrings to ask the question about" in output


def test_the_mode_named_on_the_command_line_is_the_one_that_runs(script, monkeypatch):
    """Each mode runs the work it names, so a run cannot rewrite what it checks.

    The check and the re-record differ in direction: one refuses a tree that
    gained a candidate, the other accepts today's tree as the new bar. A run
    that confused them would report success while raising the budget.
    """
    ran = []
    monkeypatch.setattr(script, "check", lambda: ran.append("check") or 0)
    monkeypatch.setattr(script, "update", lambda: ran.append("update") or 0)
    monkeypatch.setattr(script, "show", lambda paths, symptoms: ran.append("list") or 0)

    for mode in ("check", "update", "list"):
        monkeypatch.setattr(sys, "argv", ["restated_contracts.py", mode])
        assert script.main() == 0

    assert ran == ["check", "update", "list"]
