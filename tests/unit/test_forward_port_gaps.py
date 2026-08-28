"""Tests for the forward-port gap report between the two release lines.

The report is what notices that a change made on the maintenance line never
reached the development line, so it has to be right about the case it exists
for — a commit whose content is nowhere in the other tree — and about the two
ways a textual comparison lies. A line the commit only moved is in both trees
whatever the commit did, and prose inside a docstring is reworded on the way
across and matches nothing. Both are tested here, because both were measured
to flip a commit into the wrong group before the rules for them existed.

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


def test_a_commit_written_on_both_lines_is_carried_forward(lines):
    """Identical text on the other line is what "already there" looks like."""
    script, line = lines
    body = "".join(f"{DISTINCTIVE % index}\n" for index in range(5))

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
    line.write(
        "module.py",
        "shared = 1\n" + "".join(f"{DISTINCTIVE % index}\n" for index in range(5)),
    )
    maintenance_commit = line.commit("Fix support for a device")

    commit = _measure(script, line, maintenance_commit)

    assert commit.hits == 0
    assert not commit.carried_forward


def test_a_line_the_commit_only_moved_is_not_a_marker(lines):
    """A moved line is in both trees whatever the commit did.

    A block copied into a second section of the same file is the shape this
    rules out: every string in it already existed, so every one of them is
    found on the other line and the commit reads as carried forward while its
    one new string is nowhere.
    """
    script, line = lines
    existing = "".join(f"{DISTINCTIVE % index}\n" for index in range(5))
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
        "# a comment long enough to pass the length rule",
        "import homeassistant.helpers.entity_registry",
        "from homeassistant.helpers import entity_registry as er",
        "@pytest.mark.parametrize('a', [1, 2, 3, 4, 5])",
        "and the refusal did not name the device at all",
    ],
)
def test_a_line_that_cannot_carry_evidence_is_rejected(lines, line_of_code):
    """Short, commented, imported and prose lines are all rejected."""
    script, _ = lines

    assert not script._looks_distinctive(line_of_code)


def test_a_commit_too_small_to_score_is_neither_carried_nor_behind(lines):
    """A version bump has one marker, and one marker decides nothing."""
    script, line = lines
    line.git("checkout", "-q", "maintenance")
    line.write("module.py", "shared = 1\n" + f"{DISTINCTIVE % 0}\n")
    maintenance_commit = line.commit("[TASK] bump version")

    commit = _measure(script, line, maintenance_commit)

    assert commit.markers < script.MIN_MARKERS
    assert not commit.scored


def test_check_fails_on_an_unrecorded_gap(lines, capsys):
    """The gate is the whole point, so it has to close on a gap."""
    script, line = lines
    line.git("checkout", "-q", "maintenance")
    line.write(
        "module.py",
        "shared = 1\n" + "".join(f"{DISTINCTIVE % index}\n" for index in range(5)),
    )
    maintenance_commit = line.commit("Fix support for a device")

    assert script.check("maintenance", "develop") == 1
    assert maintenance_commit[:8] in capsys.readouterr().out


def test_check_passes_on_a_recorded_gap(lines, capsys):
    """A gap that stays behind on purpose passes once its reason is written."""
    script, line = lines
    line.git("checkout", "-q", "maintenance")
    line.write(
        "module.py",
        "shared = 1\n" + "".join(f"{DISTINCTIVE % index}\n" for index in range(5)),
    )
    maintenance_commit = line.commit("Fix support for a device")
    script.ACKNOWLEDGED_FILE.write_text(
        json.dumps({maintenance_commit: "fixed differently on the other line"}),
        encoding="utf-8",
    )

    assert script.check("maintenance", "develop") == 0
    assert "carried forward or recorded" in capsys.readouterr().out


def test_check_names_a_record_that_is_no_longer_behind(lines, capsys):
    """A reason for a gap that closed is scaffolding nobody removed."""
    script, line = lines
    script.ACKNOWLEDGED_FILE.write_text(
        json.dumps({"0" * 40: "fixed differently on the other line"}), encoding="utf-8"
    )

    assert script.check("maintenance", "develop") == 0
    assert "no longer behind: 00000000" in capsys.readouterr().out


def test_an_unfetched_ref_says_how_to_fetch_it(lines):
    """A missing maintenance line is the first thing a fresh clone hits."""
    script, _ = lines

    with pytest.raises(SystemExit) as failure:
        script._resolve("origin/nowhere")

    assert "git fetch" in str(failure.value)
