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


def test_a_commit_too_small_to_score_is_neither_carried_nor_behind(lines):
    """A version bump has one marker, and one marker decides nothing."""
    script, line = lines
    manifest = '{\n  "domain": "better_thermostat",\n  "version": "%s"\n}\n'
    line.git("checkout", "-q", "maintenance")
    line.write("manifest.json", manifest % "1.9.1")
    line.commit("[TASK] add the manifest")
    line.write("manifest.json", manifest % "1.9.2")
    maintenance_commit = line.commit("[TASK] bump version")

    commit = _measure(script, line, maintenance_commit)

    assert commit.markers == 1
    assert not commit.scored


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
    too_small = line.commit("[TASK] bump version")

    assert script.show("maintenance", "develop") == 0

    printed = capsys.readouterr().out
    assert "against the tree of develop" in printed
    assert "not carried forward — under 50%:" in printed
    assert "carried forward — 50% or more:" in printed
    assert "too few markers to score — under 3:" in printed
    assert "by commit convention:" in printed
    assert gap[:8] in printed
    assert too_small[:8] in printed


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
