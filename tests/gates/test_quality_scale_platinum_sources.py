"""The Platinum rules of the Integration Quality Scale that the sources decide.

Home Assistant checks strict typing with mypy for the integrations listed in
its ``.strict-typing``; this repository checks types with pyrefly, so the
rule is held by pyrefly's configuration in ``pyproject.toml``.
"""

import tomllib

import pytest

from tests.quality_scale import REPO_ROOT

PYPROJECT = REPO_ROOT / "pyproject.toml"


@pytest.mark.quality_rule("strict-typing")
def test_every_module_is_checked_at_full_strictness():
    """No file outside the tests is exempt from a rule pyrefly declares.

    The block that lets a test leave its parameters unannotated is policy
    rather than backlog; ``tests/gates/test_type_strictness_exemptions.py``
    keeps it to that one rule and keeps the backlog from growing.
    """
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    pyrefly = config["tool"]["pyrefly"]
    assert "custom_components/better_thermostat" in pyrefly["project-includes"]
    exempt = {
        entry["matches"]: sorted(rule for rule, on in entry["errors"].items() if not on)
        for entry in pyrefly.get("sub-config", [])
        if not entry["matches"].startswith("tests/")
    }
    assert exempt == {}
