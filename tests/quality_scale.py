"""The Integration Quality Scale rules the repository tracks, and their status.

Home Assistant grades integrations by the rules in its Integration Quality
Scale and records an integration's progress in ``quality_scale.yaml``. Hassfest
validates that file for core integrations only, so here the tests hold it: a
test carries ``@pytest.mark.quality_rule("<rule>")`` for the rule it checks,
a rule marked ``todo`` runs its tests as strict ``xfail``, and
``tests/gates/test_quality_scale.py`` keeps the file and the markers in step.

https://developers.home-assistant.io/docs/core/integration-quality-scale/
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
QUALITY_SCALE_FILE = (
    REPO_ROOT / "custom_components" / "better_thermostat" / "quality_scale.yaml"
)

# The Bronze tier as Home Assistant's hassfest lists it.
BRONZE_RULES = frozenset(
    {
        "action-setup",
        "appropriate-polling",
        "brands",
        "common-modules",
        "config-flow",
        "config-flow-test-coverage",
        "dependency-transparency",
        "docs-actions",
        "docs-conditions",
        "docs-high-level-description",
        "docs-installation-instructions",
        "docs-removal-instructions",
        "docs-triggers",
        "entity-event-setup",
        "entity-unique-id",
        "has-entity-name",
        "runtime-data",
        "test-before-configure",
        "test-before-setup",
        "unique-config-entry",
    }
)

STATUSES = frozenset({"done", "todo", "exempt"})


@cache
def recorded_rules() -> dict[str, dict[str, str]]:
    """Return each rule in ``quality_scale.yaml`` as its status and comment.

    A rule is written either as its bare status or as a mapping with
    ``status`` and ``comment``; both come back as the mapping.
    """
    document = yaml.safe_load(QUALITY_SCALE_FILE.read_text(encoding="utf-8"))
    return {
        rule: entry if isinstance(entry, dict) else {"status": entry}
        for rule, entry in document["rules"].items()
    }


def rule_status(rule: str) -> str | None:
    """Return the recorded status of ``rule``, or None for an unknown rule."""
    entry = recorded_rules().get(rule)
    return None if entry is None else entry.get("status")
