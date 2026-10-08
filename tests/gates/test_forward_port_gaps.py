"""Tests for the forward-port gap report between the two release lines.

The report is what notices that a change made on the maintenance line never
reached the development line, so it has to be right about the case it exists
for — a commit whose content is nowhere in the other tree — and about the two
ways a textual comparison lies. A line the commit only moved is in both trees
whatever the commit did, and prose inside a docstring is reworded on the way
across and matches nothing. Either one puts a commit in the wrong group as
soon as the rule that catches it stops applying, so both are pinned here.

The fixture builds a throwaway repository with the two lines in it rather than
reading this one: the numbers this repository produces change with every
commit, and a test that asserted them would be a test of the calendar.
"""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "forward_port_gaps.py"

# Long enough to be a marker, and unique enough that it cannot be met twice by
# accident. The rules under test reject a line on its shape, not its meaning.
DISTINCTIVE = 'value = compute_the_annunciation(entry_id, "%s")'
OTHER_DISTINCTIVE = 'result = resolve_the_selector(bundle, "external")'


def _load_script():
    """Import the gap report as a module."""
    spec = importlib.util.spec_from_file_location("forward_port_gaps", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves a field's module through sys.modules, so the module
    # has to be registered before its body runs.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _body(count: int, start: int = 0) -> str:
    """Return a file body of ``count`` distinctive lines."""
    return "".join(f"{DISTINCTIVE % index}\n" for index in range(start, start + count))


def _falling_length(index: int) -> str:
    """Return a distinctive line whose length falls as the index rises."""
    return f'value_{index:02d} = resolve("' + "x" * (20 - index) + '")'


class _Line:
    """A throwaway repository with a development and a maintenance line."""

    def __init__(self, root: Path):
        self.root = root

    def git(self, *arguments: str) -> str:
        """Run a git command in the repository."""
        return subprocess.run(
            ("git", "-C", str(self.root), *arguments),
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    def write(self, name: str, body: str) -> None:
        """Write a file, creating the directories above it."""
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def commit(self, subject: str) -> str:
        """Commit everything in the working tree and return the commit id."""
        self.git("add", "-A")
        self.git("commit", "-q", "-m", subject)
        return self.git("rev-parse", "HEAD").strip()


@pytest.fixture
def lines(tmp_path, monkeypatch):
    """Return the gap report pointed at a repository with two release lines."""
    root = tmp_path / "repo"
    root.mkdir()
    line = _Line(root)
    line.git("init", "-q", "-b", "develop")
    line.git("config", "user.email", "test@example.invalid")
    line.git("config", "user.name", "Test")
    line.write("module.py", "shared = 1\n")
    line.commit("initial")
    line.git("branch", "maintenance")

    script = _load_script()
    monkeypatch.setattr(script, "REPO_ROOT", root)
    monkeypatch.setattr(script, "ACKNOWLEDGED_FILE", tmp_path / "gaps.json")
    return script, line


def _measure(script, line, sha: str):
    """Return the measured commit with the given id."""
    measured = {c.sha: c for c in script.measure("maintenance", "develop")}
    return measured[line.git("rev-parse", sha).strip()]


def _partly_present(line, *, markers: int, found: int) -> str:
    """Commit ``markers`` lines on the maintenance line, ``found`` on both.

    Returns the maintenance commit, which scores ``found / markers``.
    """
    line.git("checkout", "-q", "develop")
    line.write("shared.py", _body(found))
    line.commit("fix: the half both lines got")
    line.git("checkout", "-q", "maintenance")
    line.write("module.py", f"shared = 1\n{_body(markers)}")
    return line.commit("Fix support for a device")


def test_a_commit_written_on_both_lines_is_carried_forward(lines):
    """Identical text on the other line is what "already there" looks like."""
    script, line = lines
    body = _body(5)

    line.git("checkout", "-q", "maintenance")
    line.write("module.py", f"shared = 1\n{body}")
    maintenance_commit = line.commit("fix: annunciate")

    line.git("checkout", "-q", "develop")
    line.write("module.py", f"shared = 1\n{body}")
    # The same subject and the same tree on the same parent would hash to the
    # same commit, and the two lines would be one branch.
    line.commit("fix: annunciate (development line)")

    commit = _measure(script, line, maintenance_commit)

    assert commit.markers == 5
    assert commit.hit_rate == 1.0
    assert commit.carried_forward


def test_a_commit_written_on_one_line_only_is_a_gap(lines):
    """A gap is a commit whose text is nowhere in the other tree."""
    script, line = lines
    line.git("checkout", "-q", "maintenance")
    line.write("module.py", f"shared = 1\n{_body(5)}")
    maintenance_commit = line.commit("Fix support for a device")

    commit = _measure(script, line, maintenance_commit)

    assert commit.hits == 0
    assert commit.hit_rate == 0.0
    assert not commit.carried_forward


def test_half_the_markers_found_counts_as_carried_forward(lines):
    """The threshold is met at half, not passed at half."""
    script, line = lines
    maintenance_commit = _partly_present(line, markers=4, found=2)

    commit = _measure(script, line, maintenance_commit)

    assert commit.markers == 4
    assert commit.hits == 2
    assert commit.hit_rate == 0.5
    assert commit.carried_forward


def test_a_quarter_of_the_markers_found_is_a_gap(lines):
    """A commit is carried forward at half its markers, not at any of them."""
    script, line = lines
    maintenance_commit = _partly_present(line, markers=4, found=1)

    commit = _measure(script, line, maintenance_commit)

    assert commit.hit_rate == 0.25
    assert not commit.carried_forward


@pytest.mark.parametrize(("count", "is_scored"), [(2, False), (3, True)])
def test_three_markers_are_the_least_that_scores(lines, count, is_scored):
    """Three markers is where a commit becomes worth judging."""
    script, line = lines
    line.git("checkout", "-q", "maintenance")
    line.write("module.py", f"shared = 1\n{_body(count)}")
    maintenance_commit = line.commit("Fix support for a device")

    commit = _measure(script, line, maintenance_commit)

    assert commit.markers == count
    assert commit.scored is is_scored


def test_a_line_the_commit_only_moved_is_not_a_marker(lines):
    """A moved line is in both trees whatever the commit did.

    A block copied into a second section of the same file is the shape this
    rules out: every string in it already existed, so every one of them is
    found on the other line and the commit reads as carried forward while its
    one new string is nowhere.
    """
    script, line = lines
    existing = _body(5)
    line.git("checkout", "-q", "develop")
    line.write("module.py", existing)
    line.commit("the block")
    line.git("checkout", "-q", "maintenance")
    line.git("merge", "-q", "develop")

    line.write("module.py", f"{existing}\nif True:\n{existing}{OTHER_DISTINCTIVE}\n")
    maintenance_commit = line.commit("Copy the block and add one line")

    commit = _measure(script, line, maintenance_commit)

    assert commit.markers == 1
    assert commit.hits == 0


def test_prose_inside_a_docstring_is_not_a_marker(lines):
    """Prose is reworded on the way across, so it cannot carry evidence."""
    script, line = lines
    line.git("checkout", "-q", "maintenance")
    line.write(
        "module.py",
        "shared = 1\n\n\ndef handler():\n"
        '    """Refuse a device that carries no thermostat.\n\n'
        "    Resolving the entity from the device is what makes the shape work,\n"
        "    and it has exactly one way to come up empty. Nothing else separates\n"
        "    an armed automation from one attached to nothing at all.\n"
        '    """\n'
        f"    {DISTINCTIVE % 0}\n",
    )
    maintenance_commit = line.commit("test: refuse the device")

    assert set(script.markers_of(maintenance_commit)) == {DISTINCTIVE % 0}


@pytest.mark.parametrize(
    "line_of_code",
    [
        "x = 1",
        "# _time_diff = 600 is applied when any head is HomematicIP",
        "import homeassistant.helpers.entity_registry  # noqa: E402",
        "from homeassistant.helpers.entity_registry import (",
        "@pytest.mark.parametrize('a', [1, 2, 3, 4, 5])",
        "and the refusal did not name the device at all",
    ],
)
def test_a_line_that_cannot_carry_evidence_is_rejected(lines, line_of_code):
    """Each rule is the only reason the line beside it is rejected.

    A line that two rules reject pins neither: it stays rejected with either
    one gone. So the commented and the imported lines here carry code
    punctuation, the prose line is long enough, and the short line is
    otherwise a marker.
    """
    script, _ = lines

    assert not script._looks_distinctive(line_of_code)


def test_a_file_of_another_kind_carries_no_markers(lines):
    """Only line-oriented text kinds are compared."""
    script, line = lines
    line.git("checkout", "-q", "maintenance")
    line.write("notes.rst", _body(5))
    maintenance_commit = line.commit("docs: write the notes down")

    assert script.markers_of(maintenance_commit) == []


def test_a_removed_line_shaped_like_a_file_header_keeps_its_file(lines):
    """A file header stands outside the hunks, and content inside one is content.

    At zero context a removed line reading ``--x`` renders as ``---x``, which
    is the shape of the header that opens a file. A YAML document separator
    meets it, and reading it as a header would drop every later addition to
    that file.
    """
    script, line = lines
    line.git("checkout", "-q", "maintenance")
    line.write("workflow.yaml", "---\nname: Nightly\n")
    line.commit("ci: add the workflow")
    line.write("workflow.yaml", f"name: Nightly\n{_body(4)}")
    maintenance_commit = line.commit("ci: drop the separator and add the job")

    assert set(script.markers_of(maintenance_commit)) == set(_body(4).splitlines())


def test_an_added_line_shaped_like_a_file_header_keeps_its_file(lines):
    """At zero context an added line reading ``++x`` renders as ``+++x``.

    A shell trace pasted into a document meets that shape, and reading it as
    a header would drop it and every addition after it.
    """
    script, line = lines
    trace = "++ export UV_CACHE_DIR=/tmp/uv-cache"
    line.git("checkout", "-q", "maintenance")
    line.write("notes.md", "shared note\n")
    line.commit("docs: start the notes")
    line.write("notes.md", f"shared note\n{trace}\n{_body(3)}")
    maintenance_commit = line.commit("docs: paste the trace and say what it means")

    assert set(script.markers_of(maintenance_commit)) == {trace, *_body(3).splitlines()}


def test_a_path_outside_ascii_is_read_on_both_lines(lines):
    """A path git would quote names its file on both sides of the comparison.

    Quoted in the commit its markers go missing, and quoted in the tree they
    are looked up in a tree that does not hold the file at all.
    """
    script, line = lines
    body = _body(3)
    line.git("checkout", "-q", "develop")
    line.write("docs/caché.md", body)
    line.commit("docs: the note on the development line")
    line.git("checkout", "-q", "maintenance")
    line.write("docs/caché.md", body)
    maintenance_commit = line.commit("docs: the note on the maintenance line")

    commit = _measure(script, line, maintenance_commit)

    assert commit.markers == 3
    assert commit.hit_rate == 1.0


def test_a_path_git_escapes_is_read_on_both_lines(lines):
    """A tab in a name is escaped whatever ``core.quotePath`` says.

    ``core.quotePath=false`` reaches bytes above ASCII and nothing else, so a
    control character, a quotation mark or a backslash still arrives wrapped
    in quotation marks. Read by prefix and suffix the file then drops out of
    both the tree and the commit, and the gap it holds is invisible — in the
    file least likely to be looked at by hand.
    """
    script, line = lines
    name = "docs/we\tird.md"
    body = _body(3)
    line.git("checkout", "-q", "develop")
    line.write(name, body)
    line.commit("docs: the note on the development line")
    line.git("checkout", "-q", "maintenance")
    line.write(name, body)
    maintenance_commit = line.commit("docs: the note on the maintenance line")

    commit = _measure(script, line, maintenance_commit)

    assert commit.markers == 3
    assert commit.hit_rate == 1.0


def test_one_marker_repeated_across_files_is_answered_once(lines):
    """Three files carrying the same line are three changes, not one.

    The development tree is searched as one set of lines, so a marker taken
    from every copy is answered by the single occurrence over there. Counting
    each copy would let a commit score full marks with two of its three files
    never carried forward.
    """
    script, line = lines
    shared = "CONSTANT_THE_THREE_FILES_SHARE = 'the same long line in each'"
    line.git("checkout", "-q", "develop")
    line.write("one.py", f"{shared}\n")
    line.commit("fix: the one file that was carried forward")
    line.git("checkout", "-q", "maintenance")
    for name in ("one.py", "two.py", "three.py"):
        line.write(name, f"{shared}\n")
    maintenance_commit = line.commit("Fix support for a device")

    commit = _measure(script, line, maintenance_commit)

    assert commit.markers == 1
    assert commit.hit_rate == 1.0


def test_a_commit_is_judged_on_at_most_the_marker_budget(lines):
    """Twelve markers are the most a commit is judged on, however large it is."""
    script, line = lines
    line.git("checkout", "-q", "maintenance")
    line.write("module.py", f"shared = 1\n{_body(20)}")
    maintenance_commit = line.commit("Rewrite the handler")

    assert len(script.markers_of(maintenance_commit)) == 12


def test_the_longest_added_lines_are_the_ones_kept(lines):
    """A budget spent on the longest lines is spent on the least repeatable."""
    script, line = lines
    candidates = [_falling_length(index) for index in range(14)]
    line.git("checkout", "-q", "maintenance")
    line.write("module.py", "shared = 1\n" + "".join(f"{c}\n" for c in candidates))
    maintenance_commit = line.commit("Rewrite the handler")

    assert script.markers_of(maintenance_commit) == candidates[:12]


def test_a_commit_spread_over_files_is_judged_on_all_of_them(lines, monkeypatch):
    """A budget too small for one file still buys a marker from the next."""
    script, line = lines
    monkeypatch.setattr(script, "MARKERS_PER_COMMIT", 2)
    line.git("checkout", "-q", "maintenance")
    line.write("a_module.py", _body(3))
    line.write("z_module.py", f"{OTHER_DISTINCTIVE}\n")
    maintenance_commit = line.commit("Touch two files")

    markers = script.markers_of(maintenance_commit)

    assert len(markers) == 2
    assert OTHER_DISTINCTIVE in markers


def test_a_merge_carries_no_content_of_its_own(lines):
    """A merge repeats what its parents say, so it is not a commit to judge."""
    script, line = lines
    line.git("checkout", "-q", "develop")
    line.write("other.py", _body(3, start=100))
    line.commit("fix: the development half")
    line.git("checkout", "-q", "maintenance")
    line.write("module.py", f"shared = 1\n{_body(5)}")
    maintenance_commit = line.commit("Fix support for a device")
    line.git("merge", "-q", "--no-ff", "-m", "Merge branch 'develop'", "develop")

    measured = {commit.sha for commit in script.measure("maintenance", "develop")}

    assert measured == {maintenance_commit}


@pytest.mark.parametrize(
    ("name", "template"),
    [
        (
            "manifest.json",
            '{\n  "domain": "better_thermostat",\n  "version": "%s"\n}\n',
        ),
        ("pyproject.toml", '[project]\nname = "better_thermostat"\nversion = "%s"\n'),
    ],
)
def test_a_version_bump_is_neither_carried_nor_behind(lines, name, template):
    """Each line carries its own version, so a bump has nothing to forward."""
    script, line = lines
    line.git("checkout", "-q", "maintenance")
    line.write(name, template % "1.9.1")
    line.commit("[TASK] add the metadata")
    line.write(name, template % "1.9.2")
    maintenance_commit = line.commit("[TASK] bump version")

    commit = _measure(script, line, maintenance_commit)

    assert commit.markers == 0
    assert commit.carried_forward


def _small_fix(line, *, lines_on_develop: int) -> str:
    """Commit two production lines and a test on the maintenance line.

    The development line gets the first ``lines_on_develop`` of the two
    production lines. Returns the maintenance commit.
    """
    production = _body(2)
    if lines_on_develop:
        line.git("checkout", "-q", "develop")
        line.write("module.py", f"shared = 1\n{_body(lines_on_develop)}")
        line.commit("fix: the development line's form")
    line.git("checkout", "-q", "maintenance")
    line.write("module.py", f"shared = 1\n{production}")
    line.write("tests/test_module.py", _body(6, start=20))
    return line.commit("fix: a two-line fix with its test")


@pytest.mark.parametrize(
    ("lines_on_develop", "is_carried"), [(0, False), (1, False), (2, True)]
)
def test_a_fix_too_small_to_score_is_carried_only_when_all_of_it_is_there(
    lines, lines_on_develop, is_carried
):
    """A small fix cannot be judged by a share, so every line of it counts.

    Two production lines are too few for a rate to mean anything, and passing
    such a fix unread would let a missing one-line fix through. It is carried
    forward when every production line is on the development line and behind
    when any one is missing.
    """
    script, line = lines
    maintenance_commit = _small_fix(line, lines_on_develop=lines_on_develop)

    commit = _measure(script, line, maintenance_commit)

    assert commit.markers == 2
    assert not commit.scored
    assert commit.carried_forward is is_carried


def test_a_small_fix_renamed_onto_the_glossary_is_carried(lines):
    """The complete-presence rule reads the glossary's renames like the rate."""
    script, line = lines
    line.git("checkout", "-q", "develop")
    line.write("glossary.toml", GLOSSARY)
    line.write(
        "module.py", "shared = 1\ntrv.remember_the_setpoint(entity_id, reported_1)\n"
    )
    line.commit("refactor: rename onto the glossary")
    line.git("checkout", "-q", "maintenance")
    line.write("module.py", f"shared = 1\n{OLD_SPELLING.format(i=1)}\n")
    maintenance_commit = line.commit("fix: remember the setpoint")

    commit = _measure(script, line, maintenance_commit)

    assert commit.markers == 1
    assert commit.carried_forward


def test_check_fails_on_a_missing_fix_too_small_to_score(lines, capsys):
    """A missing two-line fix closes the gate like a low rate does."""
    script, line = lines
    maintenance_commit = _small_fix(line, lines_on_develop=1)

    assert script.check("maintenance", "develop") == 1
    assert maintenance_commit[:8] in capsys.readouterr().out


def test_prose_inside_a_docstring_with_code_punctuation_is_not_a_marker(lines):
    """A docstring line that quotes code is prose, not a marker.

    The punctuation rule cannot tell a sentence that quotes
    ``int(value / step)`` from a statement; where the text sits can.
    """
    script, line = lines
    line.git("checkout", "-q", "maintenance")
    line.write(
        "module.py",
        "shared = 1\n\n\ndef tol(step):\n"
        '    """Return the window.\n\n'
        "    The platform truncates onto that grid (``int(value / step)``): a\n"
        "    written 6.3 becomes 62 counts = 6.2 {reported}.\n"
        '    """\n'
        f"    {DISTINCTIVE % 0}\n",
    )
    maintenance_commit = line.commit("fix: widen the window")

    assert script.markers_of(maintenance_commit) == [DISTINCTIVE % 0]


def _fix_with_tests(line, *, production_on_develop: bool, tests_on_develop: bool):
    """Commit a fix plus its tests on the maintenance line.

    The development line gets the production lines, the test lines, both or
    neither, each written by its own commit. Returns the maintenance commit.
    """
    production = _body(3)
    tests = "".join(f"assert check_the_reported_value({i}) == {i}\n" for i in range(9))
    line.git("checkout", "-q", "develop")
    if production_on_develop:
        line.write("module.py", f"shared = 1\n{production}")
    if tests_on_develop:
        line.write("tests/test_module.py", tests)
    if production_on_develop or tests_on_develop:
        line.commit("fix: the development line's form")
    line.git("checkout", "-q", "maintenance")
    line.write("module.py", f"shared = 1\n{production}")
    line.write("tests/test_module.py", tests)
    return line.commit("fix: the maintenance line's form")


@pytest.mark.parametrize(
    ("production_on_develop", "tests_on_develop", "is_carried"),
    [
        (True, False, True),
        (True, True, True),
        (False, True, False),
        (False, False, False),
    ],
)
def test_a_commit_is_judged_on_its_production_lines_alone(
    lines, production_on_develop, tests_on_develop, is_carried
):
    """Each line writes its own tests, so only production text can match.

    A fix whose production lines reached the development line is carried
    forward however differently the tests there were written, and tests that
    happen to match do not carry a production change that is missing.
    """
    script, line = lines
    maintenance_commit = _fix_with_tests(
        line,
        production_on_develop=production_on_develop,
        tests_on_develop=tests_on_develop,
    )

    commit = _measure(script, line, maintenance_commit)

    assert commit.markers == 3
    assert commit.carried_forward is is_carried


def test_a_commit_that_touches_tests_only_is_not_scored(lines):
    """A test-only commit has no production text to look for."""
    script, line = lines
    line.git("checkout", "-q", "maintenance")
    line.write("tests/test_module.py", _body(5))
    maintenance_commit = line.commit("test: pin the reported value")

    commit = _measure(script, line, maintenance_commit)

    assert commit.markers == 0
    assert not commit.scored


def test_a_production_line_found_only_in_tests_is_not_carried(lines):
    """A test quoting a production line does not put the line in place.

    The development line holds the maintenance line's production text only
    inside a test file, so the fix itself never arrived there.
    """
    script, line = lines
    production = _body(3)
    line.git("checkout", "-q", "develop")
    line.write("tests/test_module.py", production)
    line.commit("test: quote the fix the other line made")
    line.git("checkout", "-q", "maintenance")
    line.write("module.py", f"shared = 1\n{production}")
    maintenance_commit = line.commit("fix: the maintenance line's form")

    commit = _measure(script, line, maintenance_commit)

    assert commit.markers == 3
    assert not commit.carried_forward


def test_a_replaced_line_kept_in_a_test_is_not_a_marker(lines):
    """The maintenance line's production text decides what it still holds.

    A later commit replaced the lines in production and kept them in a test,
    so the development line is not asked to hold them.
    """
    script, line = lines
    line.git("checkout", "-q", "maintenance")
    line.write("module.py", f"shared = 1\n{_body(4)}")
    intermediate = line.commit("fix: the first attempt")
    line.write("module.py", f"shared = 1\n{_body(4, start=10)}")
    line.write("tests/test_module.py", _body(4))
    line.commit("fix: the final state")

    commit = _measure(script, line, intermediate)

    assert commit.markers == 0
    assert not commit.scored


@pytest.mark.parametrize("successor_on_develop", [True, False])
def test_a_line_the_maintenance_line_replaced_later_is_not_a_marker(
    lines, successor_on_develop
):
    """An intermediate state is judged by the commit that replaced it.

    The first commit's lines are gone from the maintenance line itself, so the
    development line cannot be asked to hold them. The commit that replaced
    them carries the evidence: a gap when its lines are missing on the
    development line, carried forward when they are there.
    """
    script, line = lines
    successor = _body(4, start=10)
    if successor_on_develop:
        line.git("checkout", "-q", "develop")
        line.write("module.py", f"shared = 1\n{successor}")
        line.commit("fix: the final state")
    line.git("checkout", "-q", "maintenance")
    line.write("module.py", f"shared = 1\n{_body(4)}")
    intermediate = line.commit("fix: the first attempt")
    line.write("module.py", f"shared = 1\n{successor}")
    final = line.commit("fix: the final state (maintenance line)")

    first = _measure(script, line, intermediate)
    second = _measure(script, line, final)

    assert first.markers == 0
    assert not first.scored
    assert second.markers == 4
    assert second.carried_forward is successor_on_develop


GLOSSARY = """\
[[term]]
name = "trv"
rejected = ["trv_data"]

[[term]]
name = "entity_id"
rejected = ["heater_entity_id"]

[[term]]
name = "trv.entity_id_of_the_head"
rejected = ["heater_entity_id"]
"""


OLD_SPELLING = "trv_data.remember_the_setpoint(heater_entity_id, reported_{i})"


def _renamed_fix(
    line,
    *,
    glossary: str | None,
    develop_spelling: str,
    maintenance_spelling: str = OLD_SPELLING,
) -> str:
    """Commit a fix spelled with rejected names on the maintenance line.

    Each line holds four statements in its spelling, a format string over the
    statement's index. Returns the maintenance commit.
    """
    line.git("checkout", "-q", "develop")
    if glossary is not None:
        line.write("glossary.toml", glossary)
    line.write(
        "module.py",
        "shared = 1\n" + "".join(develop_spelling.format(i=i) + "\n" for i in range(4)),
    )
    line.commit("refactor: rename onto the glossary")
    line.git("checkout", "-q", "maintenance")
    line.write(
        "module.py",
        "shared = 1\n"
        + "".join(maintenance_spelling.format(i=i) + "\n" for i in range(4)),
    )
    return line.commit("fix: remember the setpoint")


@pytest.mark.parametrize(
    "develop_spelling",
    [
        "trv.remember_the_setpoint(entity_id, reported_{i})",
        "trv.remember_the_setpoint(entity_id_of_the_head, reported_{i})",
        "trv_data.remember_the_setpoint(entity_id, reported_{i})",
        "trv_data.remember_the_setpoint(heater_entity_id, reported_{i})",
    ],
)
def test_a_line_renamed_onto_the_glossary_is_found(lines, develop_spelling):
    """The development line spells a rejected name the way the glossary says.

    A maintenance-line statement written with the old spelling is found in
    the development tree under any spelling the glossary lists for it, with
    one name renamed, all of them or none.
    """
    script, line = lines
    maintenance_commit = _renamed_fix(
        line, glossary=GLOSSARY, develop_spelling=develop_spelling
    )

    commit = _measure(script, line, maintenance_commit)

    assert commit.markers == 4
    assert commit.hits == 4


TWICE_SPELLING = "trv_data.compare_the_heads(heater_entity_id, heater_entity_id, {i})"


@pytest.mark.parametrize(
    "develop_spelling",
    [
        "trv.compare_the_heads(entity_id, entity_id_of_the_head, {i})",
        "trv.compare_the_heads(entity_id_of_the_head, entity_id, {i})",
        "trv.compare_the_heads(entity_id, heater_entity_id, {i})",
        "trv_data.compare_the_heads(heater_entity_id, entity_id_of_the_head, {i})",
    ],
    ids=["two-terms", "two-terms-swapped", "one-kept", "only-second-renamed"],
)
def test_each_occurrence_of_a_rejected_name_is_renamed_on_its_own(
    lines, develop_spelling
):
    """An alias written twice in one statement can end up spelled twice apart.

    The glossary gives one alias two replacements, and the development line
    may use both in one statement or keep the alias in one place only.
    """
    script, line = lines
    maintenance_commit = _renamed_fix(
        line,
        glossary=GLOSSARY,
        develop_spelling=develop_spelling,
        maintenance_spelling=TWICE_SPELLING,
    )

    commit = _measure(script, line, maintenance_commit)

    assert commit.markers == 4
    assert commit.hits == 4


@pytest.mark.parametrize(
    ("develop_spelling", "hits"),
    [
        ("self._trv.remember_the_setpoint(entity_id, reported_{i})", 4),
        ("self.trv.remember_the_setpoint(entity_id, reported_{i})", 0),
    ],
    ids=["underscore-kept", "underscore-dropped"],
)
def test_a_private_name_is_renamed_with_its_underscores(lines, develop_spelling, hits):
    """A private alias is renamed onto the term behind the same underscores.

    The naming gate reads ``_trv_data`` as the alias ``trv_data``; the
    development line that renamed it to ``_trv`` is found, one that also made
    it public is a different statement.
    """
    script, line = lines
    maintenance_commit = _renamed_fix(
        line,
        glossary=GLOSSARY,
        develop_spelling=develop_spelling,
        maintenance_spelling="self._trv_data.remember_the_setpoint(entity_id, reported_{i})",
    )

    commit = _measure(script, line, maintenance_commit)

    assert commit.markers == 4
    assert commit.hits == hits


@pytest.mark.parametrize(
    ("name", "renamed"),
    [
        ("_real_trv", ("trv",)),
        ("_trv_data", ("_trv",)),
        ("__trv_data", ("__trv",)),
        ("trv_data", ("trv",)),
        ("_trv_cache", ()),
        ("__", ()),
    ],
    ids=["private-alias", "private", "dunder", "public", "unlisted", "underscores"],
)
def test_a_name_is_renamed_as_the_naming_gate_reads_it(lines, name, renamed):
    """An alias listed with its underscore wins; otherwise they are kept.

    The naming gate reads a name as written first and then without its
    leading underscores, so the rename puts back exactly those it took off.
    """
    script, _ = lines
    renames = {"_real_trv": ("trv",), "trv_data": ("trv",)}

    assert script._renamed(name, renames) == renamed


@pytest.mark.parametrize(
    ("name", "renamed"),
    [
        ("preset_temp", ("preset_temperature",)),
        ("_temp_to_set", ("_temperature_to_set",)),
        ("delay_s", ("delay_seconds",)),
        ("_last_pct_s", ("_last_percent_seconds",)),
        ("rate_per_s", ()),
        ("s_count", ()),
        ("CONF_TEMP", ()),
    ],
    ids=[
        "word",
        "private-word",
        "suffix",
        "word-and-suffix",
        "rate",
        "no-suffix",
        "constant",
    ],
)
def test_a_name_is_renamed_by_the_glossarys_word_parts(lines, name, renamed):
    """A rejected word is replaced wherever it stands, a unit suffix at the end.

    A suffix after `per` is a rate's denominator and stays, and the match is
    case-sensitive as in the naming gate, so a constant keeps its spelling.
    """
    script, _ = lines
    renames = script.Renames(
        {}, {"temp": "temperature", "pct": "percent"}, {"s": "seconds"}
    )

    assert script._renamed(name, renames) == renamed


WORD_PART_GLOSSARY = """\
[[word]]
name = "temperature"
rejected = ["temp"]

[[modifier]]
name = "_seconds"
rejected = ["_s"]
"""


def test_a_line_renamed_by_word_parts_is_found(lines):
    """A fix written with `temp` and `_s` is found where develop spells them out."""
    script, line = lines
    maintenance_commit = _renamed_fix(
        line,
        glossary=WORD_PART_GLOSSARY,
        develop_spelling="hold_the_preset(preset_temperature, delay_seconds, {i})",
        maintenance_spelling="hold_the_preset(preset_temp, delay_s, {i})",
    )

    commit = _measure(script, line, maintenance_commit)

    assert commit.markers == 4
    assert commit.hits == 4


def test_a_private_alias_past_the_cap_is_still_renamed(lines):
    """Past the spelling cap, a private alias takes the term behind its underscore."""
    script, _ = lines
    marker = " + ".join(["_old"] * 12)

    spellings = script._spellings(marker, {"old": ("first", "second")})

    assert " + ".join(["_first"] * 12) in spellings
    assert " + ".join(["_second"] * 12) in spellings


def test_the_spellings_of_one_marker_are_bounded(lines):
    """A line full of aliases costs at most the cap, and still spells them all.

    Past the cap every occurrence of an alias takes the same spelling, so the
    consistent rename stays among the spellings tried.
    """
    script, _ = lines
    marker = " + ".join(["old"] * 12)
    renames = {"old": ("first", "second")}

    spellings = script._spellings(marker, renames)

    assert len(spellings) <= script.SPELLINGS_PER_MARKER
    assert spellings[0] == marker
    assert " + ".join(["first"] * 12) in spellings
    assert " + ".join(["second"] * 12) in spellings


@pytest.mark.parametrize(
    ("glossary", "maintenance_spelling", "develop_spelling"),
    [
        (None, OLD_SPELLING, "trv.remember_the_setpoint(entity_id, reported_{i})"),
        (GLOSSARY, OLD_SPELLING, "trv.remember_the_setpoint(entity_id, confirmed_{i})"),
        (
            GLOSSARY,
            OLD_SPELLING,
            "device.remember_the_setpoint(entity_id, reported_{i})",
        ),
        (
            GLOSSARY,
            "trv_data.remember_the_setpoint(trv_data_cache, reported_{i})",
            "trv.remember_the_setpoint(trv_cache, reported_{i})",
        ),
    ],
    ids=["no-glossary", "other-statement", "unlisted-name", "part-of-a-name"],
)
def test_a_rename_the_glossary_does_not_list_is_not_found(
    lines, glossary, maintenance_spelling, develop_spelling
):
    """Only the glossary's own renames are undone, and only whole names.

    Without a glossary on the development line the spellings stay apart, and
    a statement that differs in more than a listed name stays missing.
    """
    script, line = lines
    maintenance_commit = _renamed_fix(
        line,
        glossary=glossary,
        develop_spelling=develop_spelling,
        maintenance_spelling=maintenance_spelling,
    )

    commit = _measure(script, line, maintenance_commit)

    assert commit.markers == 4
    assert commit.hits == 0


@pytest.mark.parametrize(
    "glossary",
    ["[[term]\nname = ", '[[term]]\nname = "trv"\nrejected = "trv_data"\n'],
    ids=["unparsable", "rejected-not-a-list"],
)
def test_a_glossary_the_gate_cannot_read_stops_it(lines, glossary):
    """A broken glossary is an error, not an empty one that renames nothing."""
    script, line = lines
    line.git("checkout", "-q", "develop")
    line.write("glossary.toml", glossary)
    line.commit("docs: the glossary")

    with pytest.raises(SystemExit, match=r"glossary\.toml"):
        script.measure("maintenance", "develop")


@pytest.mark.parametrize(
    ("subject", "is_conventional"),
    [
        ("fix: reset an MQTT TRV onto its manual preset", True),
        ("fix(config_flow): keep the submitted step token", True),
        ("refactor!: drop the legacy calibration mode", True),
        ("[TASK] bump version", False),
        ("Fix support for Eurotronic Spirit Z-Wave", False),
    ],
)
def test_the_commit_convention_is_read_from_the_subject(
    lines, subject, is_conventional
):
    """The convention is the proxy for how a commit came to be written."""
    script, _ = lines

    commit = script.Commit(
        sha="0" * 40, subject=subject, author="Test", markers=3, hits=3
    )

    assert commit.conventional is is_conventional


def test_a_missing_object_leaves_the_batch_stream_in_step(lines):
    """A path git cannot resolve carries no content to skip over."""
    script, _ = lines
    stream = b"develop:gone.py missing\n3f2a blob 12\nkept = True\n\n"

    assert script._parse_batch(stream) == {"kept = True"}


def test_the_list_mode_names_every_group(lines, capsys):
    """The list is the whole report, so every group has to appear in it."""
    script, line = lines
    gap = _partly_present(line, markers=4, found=4)
    line.write("module.py", f"shared = 1\n{_body(4)}{OTHER_DISTINCTIVE}\n")
    too_small = line.commit("fix: one more line")
    line.write("tests/test_module.py", _body(3))
    unmarked = line.commit("test: pin the value")

    assert script.show("maintenance", "develop") == 0

    printed = capsys.readouterr().out
    assert "against the tree of develop" in printed
    assert "not carried forward — under 50%, or a marker missing" in printed
    assert "carried forward — 50% or more, or every marker" in printed
    assert "no production markers:" in printed
    assert "by commit convention:" in printed
    assert gap[:8] in printed
    assert too_small[:8] in printed
    assert unmarked[:8] in printed


def test_check_fails_on_an_unrecorded_gap(lines, capsys):
    """The gate is the whole point, so it has to close on a gap."""
    script, line = lines
    line.git("checkout", "-q", "maintenance")
    line.write("module.py", f"shared = 1\n{_body(5)}")
    maintenance_commit = line.commit("Fix support for a device")

    assert script.check("maintenance", "develop") == 1
    assert maintenance_commit[:8] in capsys.readouterr().out


def test_check_passes_on_a_recorded_gap(lines, capsys):
    """A gap that stays behind on purpose passes once its reason is written."""
    script, line = lines
    line.git("checkout", "-q", "maintenance")
    line.write("module.py", f"shared = 1\n{_body(5)}")
    maintenance_commit = line.commit("Fix support for a device")
    script.ACKNOWLEDGED_FILE.write_text(
        json.dumps({maintenance_commit: "fixed differently on the other line"}),
        encoding="utf-8",
    )

    assert script.check("maintenance", "develop") == 0
    assert "carried forward or recorded" in capsys.readouterr().out


def test_check_counts_the_recorded_gaps_apart_from_the_rest(lines, capsys):
    """Both numbers are reported: what is behind, and what is unexplained."""
    script, line = lines
    line.git("checkout", "-q", "maintenance")
    line.write("module.py", f"shared = 1\n{_body(5)}")
    recorded = line.commit("Fix support for a device")
    line.write("other.py", _body(5, start=50))
    line.commit("Fix support for another device")
    script.ACKNOWLEDGED_FILE.write_text(
        json.dumps({recorded: "fixed differently on the other line"}), encoding="utf-8"
    )

    assert script.check("maintenance", "develop") == 1

    printed = capsys.readouterr().out
    assert "2 commits are not carried forward, 1 of them recorded" in printed
    assert "The remaining 1:" in printed


def test_check_names_a_record_that_is_no_longer_behind(lines, capsys):
    """A reason for a gap that closed is scaffolding nobody removed."""
    script, line = lines
    script.ACKNOWLEDGED_FILE.write_text(
        json.dumps({"0" * 40: "fixed differently on the other line"}), encoding="utf-8"
    )

    assert script.check("maintenance", "develop") == 0
    assert "no longer behind: 00000000" in capsys.readouterr().out


def test_main_runs_the_mode_it_is_given(lines, monkeypatch, capsys):
    """The two modes answer different questions, and only one of them gates."""
    script, line = lines
    line.git("checkout", "-q", "maintenance")
    line.write("module.py", f"shared = 1\n{_body(5)}")
    line.commit("Fix support for a device")
    arguments = ["--maintenance", "maintenance", "--development", "develop"]

    monkeypatch.setattr(sys, "argv", ["forward_port_gaps.py", "list", *arguments])
    assert script.main() == 0
    assert "by commit convention:" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["forward_port_gaps.py", "check", *arguments])
    assert script.main() == 1
    assert "not carried forward" in capsys.readouterr().out


def test_an_unfetched_ref_says_how_to_fetch_it(lines):
    """A missing maintenance line is the first thing a fresh clone hits."""
    script, _ = lines

    with pytest.raises(SystemExit) as failure:
        script._resolve("origin/nowhere")

    assert "git fetch" in str(failure.value)
    assert "nowhere:refs/remotes/origin/nowhere" in str(failure.value)


def test_a_name_inside_a_quoted_string_keeps_its_spelling(lines):
    """A quoted key is text a rename leaves alone, so only the code is respelled."""
    script, _ = lines
    renames = script.Renames({}, {}, {"s": "seconds"})

    spellings = script._spellings('config["delay_s"] = delay_s', renames)

    assert spellings == [
        'config["delay_s"] = delay_s',
        'config["delay_s"] = delay_seconds',
    ]


def test_the_spelling_with_every_alias_renamed_survives_the_cap(lines):
    """Twelve distinct aliases spell 4096 ways; the full rename is still tried.

    It comes right after the marker, so the cap cannot cut it off.
    """
    script, _ = lines
    aliases = [f"old{i}" for i in range(12)]
    renames = {alias: (f"new{i}",) for i, alias in enumerate(aliases)}
    marker = " + ".join(aliases)

    spellings = script._spellings(marker, renames)

    assert len(spellings) == script.SPELLINGS_PER_MARKER
    assert spellings[1] == " + ".join(f"new{i}" for i in range(12))


def test_a_name_in_an_f_string_field_is_code_and_respelled(lines):
    """Inside an f-string only the literal text keeps its spelling.

    A replacement field holds code, so its names are renamed like any other;
    the text around it, and a doubled brace, stay as written.
    """
    script, _ = lines
    renames = script.Renames({}, {}, {"s": "seconds"})

    spellings = script._spellings('log(f"delay_s={delay_s} {{delay_s}}")', renames)

    assert spellings == [
        'log(f"delay_s={delay_s} {{delay_s}}")',
        'log(f"delay_s={delay_seconds} {{delay_s}}")',
    ]
