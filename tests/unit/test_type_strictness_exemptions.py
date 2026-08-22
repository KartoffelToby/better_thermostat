"""Tests for the pyrefly type-strictness exemption list in pyproject.toml.

``[tool.pyrefly]`` declares the strictness every file under
``project-includes`` is checked at. Each ``[[tool.pyrefly.sub-config]]`` block
names one file that does not meet it yet, together with the rules that file is
exempt from. The list is the remaining backlog: it shrinks as files get
annotated, and a file nobody listed is strict from the start.

Three ways of widening it leave a reviewer nothing to see. An entry whose file
is gone still matches a file created at that path later, which would start out
exempt. A wildcard covers files nobody measured, including files that are
strict today. A sub-config that sets a rule to ``true`` makes the list an
enumeration of the clean files instead of the backlog, and settles strictness
per entry rather than on ``[tool.pyrefly]``, where one declaration reaches
every file. The fourth way — appending an entry — is visible in the diff but
still has to raise the recorded ceiling.
"""

from pathlib import Path
import tomllib

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"

# How many (file, rule) exemptions the list may hold. Lower it as entries
# leave; raising it is the one way the backlog grows, and says so in the diff.
EXEMPTION_CEILING = 58

GLOB_CHARACTERS = "*?["


def _exemptions():
    """Return the pyrefly sub-config blocks as parsed from pyproject.toml."""
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return config["tool"]["pyrefly"]["sub-config"]


def test_every_exemption_names_a_file_that_exists():
    gone = sorted(
        entry["matches"]
        for entry in _exemptions()
        if not (REPO_ROOT / entry["matches"]).is_file()
    )

    assert not gone, f"exempted files that are no longer in the repository: {gone}"


def test_every_exemption_names_one_exact_file():
    wildcards = sorted(
        entry["matches"]
        for entry in _exemptions()
        if any(char in entry["matches"] for char in GLOB_CHARACTERS)
    )

    assert not wildcards, f"exemptions that match more than one file: {wildcards}"


def test_no_file_is_exempted_twice():
    paths = [entry["matches"] for entry in _exemptions()]

    repeated = sorted({path for path in paths if paths.count(path) > 1})

    assert not repeated, f"files with more than one exemption block: {repeated}"


def test_no_entry_tightens():
    tightenings = sorted(
        f"{entry['matches']} ({rule})"
        for entry in _exemptions()
        for rule, enabled in entry["errors"].items()
        if enabled
    )

    assert not tightenings, (
        f"sub-configs that tighten rather than exempt: {tightenings}. "
        "Strictness belongs on [tool.pyrefly], where it reaches every file."
    )


def test_the_backlog_is_the_size_it_records():
    recorded = sum(len(entry["errors"]) for entry in _exemptions())

    assert recorded == EXEMPTION_CEILING, (
        f"the list holds {recorded} exemptions, EXEMPTION_CEILING says "
        f"{EXEMPTION_CEILING}. Lower it once entries leave; raising it means a "
        "file was exempted instead of annotated."
    )
