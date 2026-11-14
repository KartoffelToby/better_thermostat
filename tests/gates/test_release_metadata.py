"""Consistency checks for the metadata that ships a release.

These files are edited by hand at release time and are not exercised by any
other test, so drift between them stays invisible until it reaches a user:
a stale HACS gate offers the integration to installs whose Python cannot
even parse it, and a version that disagrees with the manifest is what Home
Assistant ends up displaying.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import tomllib

import pytest

ROOT = Path(__file__).parents[2]
MANIFEST = ROOT / "custom_components" / "better_thermostat" / "manifest.json"
PYPROJECT = ROOT / "pyproject.toml"
HACS = ROOT / "hacs.json"
DOCS = ROOT / "docs"
# Matched against prose with its Markdown links reduced to their text and its
# emphasis and code markers removed; whitespace includes line breaks, because
# the Markdown sources wrap.
REQUIREMENT_IN_DOCS = re.compile(
    r"Home\s+Assistant\s+(\d{4}\.\d+(?:\.\d+)?)\s+or\s+(?:newer|later)"
)
MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
MARKDOWN_MARKUP = re.compile(r"[`*_]")


def _requirements(markdown: str) -> list[re.Match[str]]:
    """Return every "Home Assistant X or newer" the Markdown text states."""
    prose = MARKDOWN_MARKUP.sub("", MARKDOWN_LINK.sub(r"\1", markdown))
    return list(REQUIREMENT_IN_DOCS.finditer(prose))


def _version_tuple(version: str) -> tuple[int, ...]:
    """Return year, month and patch of a version as a comparable tuple.

    An omitted patch counts as 0, so ``2026.11`` names the same release as
    ``2026.11.0``.
    """
    parts = [int(part) for part in re.findall(r"\d+", version)[:3]]
    return tuple(parts + [0] * (3 - len(parts)))


def _declared_homeassistant_floor() -> str:
    """Return the Home Assistant version pinned in ``pyproject.toml``."""
    data = tomllib.loads(PYPROJECT.read_text())
    for requirement in data["project"]["dependencies"]:
        match = re.fullmatch(r"homeassistant>=([0-9.]+)", requirement.strip())
        if match:
            return match.group(1)
    raise AssertionError("pyproject.toml declares no homeassistant floor")


def test_manifest_and_pyproject_versions_match() -> None:
    """The manifest version is the shipped one; pyproject must not drift."""
    manifest_version = json.loads(MANIFEST.read_text())["version"]
    pyproject_version = tomllib.loads(PYPROJECT.read_text())["project"]["version"]

    assert manifest_version == pyproject_version, (
        f"manifest.json says {manifest_version!r} but pyproject.toml says "
        f"{pyproject_version!r} — pyproject carries a 'keep in sync' comment, "
        "so these two must be edited together"
    )


def test_hacs_gate_is_the_declared_home_assistant_floor() -> None:
    """HACS must offer the integration to exactly the installs that can run it.

    ``hacs.json`` is the only gate on the minimum Home Assistant version.
    Set below the floor declared in ``pyproject.toml``, HACS happily installs
    the integration onto an older core — and because the code uses Python
    3.14 syntax, that install fails at import rather than degrading. Set
    above it, HACS refuses installs that the README and the docs invite.
    """
    hacs_gate = json.loads(HACS.read_text())["homeassistant"]
    declared_floor = _declared_homeassistant_floor()

    assert hacs_gate == declared_floor, (
        f"hacs.json gates on Home Assistant {hacs_gate} but the project "
        f"requires >={declared_floor}"
    )


def test_readme_states_the_declared_home_assistant_floor() -> None:
    """The README requirement line is what users read before installing."""
    readme = (ROOT / "README.md").read_text()
    declared_floor = _declared_homeassistant_floor()

    match = re.search(r"Minimum required Home Assistant version: `([0-9.]+)`", readme)
    assert match, "README.md no longer states a minimum Home Assistant version"
    assert match.group(1) == declared_floor, (
        f"README.md advertises {match.group(1)} but the project requires "
        f">={declared_floor}"
    )


def test_docs_state_the_declared_home_assistant_floor() -> None:
    """Every "Home Assistant X or newer" in the docs names the declared floor.

    The docs are published as the website and restate the requirement where a
    user installs or upgrades. A mention of an older version that is not
    phrased as a requirement, such as the floor of an earlier release, is left
    alone.
    """
    declared_floor = _version_tuple(_declared_homeassistant_floor())
    stale = [
        f"{page.relative_to(ROOT)}: {match.group(0)}"
        for page in sorted(DOCS.rglob("*.md"))
        for match in _requirements(page.read_text())
        if _version_tuple(match.group(1)) != declared_floor
    ]

    assert not stale, (
        "the docs state a Home Assistant requirement other than the floor in "
        "pyproject.toml:\n" + "\n".join(stale)
    )


@pytest.mark.parametrize(
    ("text", "version"),
    [
        ("requires **Home Assistant `2026.10.0` or newer**.", "2026.10.0"),
        ("requires Home Assistant 2026.10 or later.", "2026.10"),
        ("requires Home Assistant _2026.10.0_ or newer.", "2026.10.0"),
        ("requires Home Assistant 2026.10.0 or **newer**.", "2026.10.0"),
        ("requires **Home** Assistant 2026.10.0 or newer.", "2026.10.0"),
        (
            "requires [Home Assistant](https://www.home-assistant.io/) "
            "2026.10.0 or newer.",
            "2026.10.0",
        ),
        ("- **Home Assistant\n  2026.10.0 or newer is required.**", "2026.10.0"),
        ("requires **Home Assistant `2026.10.0`\nor\nnewer**.", "2026.10.0"),
    ],
    ids=[
        "code-span",
        "or-later",
        "underscore-emphasis",
        "emphasis-after-or",
        "emphasis-inside-name",
        "linked-name",
        "wrapped-before-version",
        "wrapped-inside-phrase",
    ],
)
def test_a_requirement_is_found_however_the_source_formats_it(
    text: str, version: str
) -> None:
    assert [match.group(1) for match in _requirements(text)] == [version]


def test_a_version_that_is_not_a_requirement_is_not_matched() -> None:
    text = "1.9.3 runs on\n  2026.9.0, 1.9.2 and older on 2026.7.2."

    assert _requirements(text) == []


def test_a_requirement_without_a_patch_names_the_release_with_patch_0() -> None:
    assert _version_tuple("2026.11") == _version_tuple("2026.11.0") == (2026, 11, 0)
    assert _version_tuple("2026.11") != _version_tuple("2026.11.1")
