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
    """No file is exempt from any rule of the strictness pyrefly declares.

    ``tests/gates/test_type_strictness_exemptions.py`` keeps the exemption
    list from growing; this rule is met once it is empty.
    """
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    pyrefly = config["tool"]["pyrefly"]
    assert pyrefly["project-includes"] == ["custom_components/better_thermostat"]
    exempt = {
        entry["matches"]: sorted(rule for rule, on in entry["errors"].items() if not on)
        for entry in pyrefly.get("sub-config", [])
    }
    assert exempt == {}
