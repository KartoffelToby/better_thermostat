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
every file. The fourth way, adding an entry or swapping one for another, is
visible in the diff and also has to change the recorded list.

One block stands outside that list: the one that covers ``tests/``. It is
policy rather than backlog, and lets a test leave its parameters unannotated,
since pytest hands fixtures in by name. It may relax that rule and no other.
"""

from pathlib import Path
import tomllib

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"

# The (file, rule) exemptions the list holds. Remove a pair once its file is
# annotated; adding one is the only way the backlog grows, and says so here.
RECORDED_EXEMPTIONS = frozenset(
    {
        (
            "custom_components/better_thermostat/number.py",
            "bad-override-mutable-attribute",
        )
    }
)

GLOB_CHARACTERS = "*?["

TESTS_BLOCK = {"matches": "tests/**", "errors": {"implicit-any-parameter": False}}


def _sub_configs():
    """Return the pyrefly sub-config blocks as parsed from pyproject.toml."""
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return config["tool"]["pyrefly"]["sub-config"]


def _exemptions():
    """Return the sub-config blocks that exempt a file of the integration."""
    return [
        entry for entry in _sub_configs() if not entry["matches"].startswith("tests/")
    ]


def test_the_tests_relax_only_their_parameter_annotations():
    blocks = [
        entry for entry in _sub_configs() if entry["matches"].startswith("tests/")
    ]

    assert blocks == [TESTS_BLOCK]


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


def _pairs(entries) -> set[tuple[str, str]]:
    """Return the (file, rule) pairs the given sub-config blocks exempt."""
    return {(entry["matches"], rule) for entry in entries for rule in entry["errors"]}


def test_the_backlog_is_the_one_it_records():
    held = _pairs(_exemptions())

    assert held == RECORDED_EXEMPTIONS, (
        f"exempted but not recorded: {sorted(held - RECORDED_EXEMPTIONS)}; "
        f"recorded but no longer exempted: {sorted(RECORDED_EXEMPTIONS - held)}. "
        "Remove a pair from RECORDED_EXEMPTIONS once its file is annotated; "
        "adding one means a file was exempted instead of annotated."
    )


def test_a_swapped_exemption_does_not_match_the_record():
    swapped = [
        {
            "matches": "custom_components/better_thermostat/sensor.py",
            "errors": {"bad-override-mutable-attribute": False},
        }
    ]

    assert len(_pairs(swapped)) == len(RECORDED_EXEMPTIONS)
    assert _pairs(swapped) != RECORDED_EXEMPTIONS
