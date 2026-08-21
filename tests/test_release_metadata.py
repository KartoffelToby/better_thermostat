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

ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "custom_components" / "better_thermostat" / "manifest.json"
PYPROJECT = ROOT / "pyproject.toml"
HACS = ROOT / "hacs.json"


def _version_tuple(version: str) -> tuple[int, ...]:
    """Return the numeric release segment of a version as a comparable tuple."""
    return tuple(int(part) for part in re.findall(r"\d+", version)[:3])


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


def test_hacs_gate_is_not_below_the_declared_home_assistant_floor() -> None:
    """HACS must not offer the integration to installs that cannot run it.

    ``hacs.json`` is the only gate on the minimum Home Assistant version.
    Set below the floor declared in ``pyproject.toml``, HACS happily installs
    the integration onto an older core — and because the code uses Python
    3.14 syntax, that install fails at import rather than degrading.
    """
    hacs_gate = json.loads(HACS.read_text())["homeassistant"]
    declared_floor = _declared_homeassistant_floor()

    assert _version_tuple(hacs_gate) >= _version_tuple(declared_floor), (
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
