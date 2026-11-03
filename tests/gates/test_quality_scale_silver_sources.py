"""The Silver rules of the Integration Quality Scale that the sources decide.

These rules are about what the repository ships, the manifest, the platform
modules, the strings, the coverage floors and the documentation, rather than
about a running instance; ``tests/integration/test_quality_scale_silver.py``
holds the others.
"""

import ast
import json
from pathlib import Path

import pytest

from custom_components.better_thermostat import PLATFORMS
from tests.quality_scale import REPO_ROOT

COMPONENT = REPO_ROOT / "custom_components" / "better_thermostat"
DOCS = REPO_ROOT / "docs"
FLOORS_FILE = REPO_ROOT / ".coverage-floors.json"

# The share of a module's branches and lines the tests have to run.
COVERAGE_TARGET = 95.0


def _docs_text() -> str:
    pages = sorted([*DOCS.rglob("*.md"), *DOCS.rglob("*.mdx")])
    return "\n".join(page.read_text(encoding="utf-8") for page in pages)


def _undocumented_fields(flow: str) -> dict[str, list[str]]:
    """Return, per step of ``flow``, the fields no documentation page names.

    A field counts as named when its label, the text the form shows, appears
    on a page; the docs speak to people filling in the form, so they use the
    label rather than the key.
    """
    strings = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
    docs = _docs_text().casefold()
    missing = {
        step_id: sorted(
            key
            for key, label in step.get("data", {}).items()
            if label.casefold() not in docs
        )
        for step_id, step in strings[flow]["step"].items()
    }
    return {step_id: keys for step_id, keys in missing.items() if keys}


@pytest.mark.quality_rule("integration-owner")
def test_the_manifest_names_a_code_owner():
    manifest = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))
    owners = manifest["codeowners"]
    assert owners
    assert all(owner.startswith("@") for owner in owners), owners


@pytest.mark.quality_rule("parallel-updates")
@pytest.mark.parametrize("platform", PLATFORMS, ids=str)
def test_every_platform_sets_its_parallel_updates(platform):
    """Each platform says how many updates and actions it runs at once.

    Home Assistant otherwise picks a default per platform, and nobody has
    decided whether that default fits.
    """
    path = COMPONENT / f"{platform.value}.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values = [
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign | ast.AnnAssign)
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name) and target.id == "PARALLEL_UPDATES"
    ]
    assert len(values) == 1, f"{path.name} sets PARALLEL_UPDATES {len(values)} times"
    (value,) = values
    assert isinstance(value, ast.Constant) and type(value.value) is int, (
        ast.unparse(value) if value is not None else None
    )


@pytest.mark.quality_rule("docs-installation-parameters")
def test_every_field_of_the_setup_is_documented():
    assert _undocumented_fields("config") == {}


@pytest.mark.quality_rule("docs-configuration-parameters")
def test_every_field_of_the_options_is_documented():
    assert _undocumented_fields("options") == {}


@pytest.mark.quality_rule("test-coverage")
def test_every_module_is_held_above_the_coverage_target():
    """The recorded floors are what CI holds, so each has to reach the target.

    A module's floor is the coverage it may not drop below; a floor under the
    target lets the module fall back there unnoticed, whatever it measures
    today, and a module without a floor is not held at all.
    """
    floors = json.loads(FLOORS_FILE.read_text(encoding="utf-8"))
    package = REPO_ROOT / "custom_components/better_thermostat"
    modules = {str(path.relative_to(REPO_ROOT)) for path in package.rglob("*.py")}
    assert sorted(modules - floors.keys()) == []
    below = {
        str(Path(module).relative_to("custom_components/better_thermostat")): floor
        for module, floor in floors.items()
        if floor < COVERAGE_TARGET
    }
    assert below == {}
