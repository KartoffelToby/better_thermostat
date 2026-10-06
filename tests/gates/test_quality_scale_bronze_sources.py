"""The Bronze rules of the Integration Quality Scale that the sources decide.

These rules are about what the repository ships, the manifest, the strings,
the coverage floors and the documentation, rather than about a running
instance; ``tests/integration/test_quality_scale_bronze.py`` holds the others.

The documentation rules can only be checked for a place to stand: a section
for each action, a page that says how to remove the integration. Whether the
text there is any good stays a review question.
"""

import ast
import json
from pathlib import Path
import re
import tomllib

from packaging.requirements import Requirement
import pytest
import yaml

from custom_components.better_thermostat.device_condition import CONDITION_TYPES
from custom_components.better_thermostat.device_trigger import TRIGGER_TYPES
from tests.quality_scale import REPO_ROOT

COMPONENT = REPO_ROOT / "custom_components" / "better_thermostat"
DOCS = REPO_ROOT / "docs"
LANDING_PAGE = DOCS / "index.mdx"
INSTALL_PAGE = DOCS / "setup" / "getting-started.md"

# Modules whose entities subscribe to events; entity.py holds their base.
ENTITY_MODULES = ("climate.py", "number.py", "sensor.py", "switch.py", "entity.py")
SUBSCRIPTIONS = re.compile(
    r"^async_track_|^async_listen(_once)?$|^async_dispatcher_connect$"
)

HEADING = re.compile(r"^#{1,6}\s+(.*)$", re.MULTILINE)


def _docs_pages() -> list[Path]:
    return sorted([*DOCS.rglob("*.md"), *DOCS.rglob("*.mdx")])


def _docs_text() -> str:
    return "\n".join(page.read_text(encoding="utf-8") for page in _docs_pages())


def _body(page: Path) -> str:
    """Return ``page`` without its front matter."""
    text = page.read_text(encoding="utf-8")
    if text.startswith("---"):
        text = text.split("---", 2)[2]
    return text


def _sections(page: Path) -> dict[str, str]:
    """Return each heading of ``page`` with the text up to the next heading."""
    body = _body(page)
    marks = list(HEADING.finditer(body))
    return {
        mark.group(1).strip(): body[mark.end() : nxt.start() if nxt else len(body)]
        for mark, nxt in zip(marks, [*marks[1:], None], strict=True)
    }


@pytest.mark.parametrize("flow", ["config", "options"])
def test_every_flow_field_is_described(flow):
    """Each field a step shows carries a ``data_description`` under it.

    This is half of the config-flow rule; the other half, settings kept in
    ``entry.options``, is held in ``tests/integration/test_config_flow.py``.
    """
    strings = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
    undescribed = {
        step_id: sorted(
            set(step.get("data", {})) - set(step.get("data_description", {}))
        )
        for step_id, step in strings[flow]["step"].items()
    }
    assert {step: fields for step, fields in undescribed.items() if fields} == {}


@pytest.mark.quality_rule("config-flow-test-coverage")
def test_the_config_flow_is_held_to_full_coverage():
    """The recorded floor is what CI holds, so the floor has to say 100."""
    floors = json.loads(
        (REPO_ROOT / ".coverage-floors.json").read_text(encoding="utf-8")
    )
    assert floors["custom_components/better_thermostat/config_flow.py"] == 100.0


@pytest.mark.quality_rule("dependency-transparency")
def test_every_requirement_comes_from_pypi():
    """Every requirement names a package published on PyPI, by name and version.

    The lock file records where uv resolved each package from, so the check
    needs no network.
    """
    manifest = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    sources = {
        package["name"]: package.get("source", {}) for package in lock["package"]
    }

    assert manifest["requirements"]
    for line in manifest["requirements"]:
        requirement = Requirement(line)
        assert requirement.url is None, line
        assert sources[requirement.name] == {"registry": "https://pypi.org/simple"}, (
            line
        )


@pytest.mark.quality_rule("entity-event-setup")
def test_no_entity_subscribes_in_its_constructor():
    """Subscriptions start in ``async_added_to_hass``, where they can be undone.

    One opened in ``__init__`` runs before the entity is added and is never
    removed when an entity that failed to add is dropped.
    """
    found = []
    for name in ENTITY_MODULES:
        path = COMPONENT / name
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for function in ast.walk(tree):
            if not (
                isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef)
                and function.name == "__init__"
            ):
                continue
            for call in ast.walk(function):
                if not isinstance(call, ast.Call):
                    continue
                callee = call.func
                called = (
                    callee.attr
                    if isinstance(callee, ast.Attribute)
                    else getattr(callee, "id", "")
                )
                if SUBSCRIPTIONS.search(called):
                    found.append(f"{name}:{call.lineno} {called}")
    assert found == []


@pytest.mark.quality_rule("docs-high-level-description")
def test_the_landing_page_says_what_better_thermostat_is():
    """The page the manifest links to opens with prose, not only links.

    A line of prose is any line that is not markup: no component, badge, link
    or heading.
    """
    markup = ("<", "[", "!", "#", "|", "import ", "{", "-", "*")
    prose = [
        line
        for line in _body(LANDING_PAGE).splitlines()
        if (stripped := line.strip()) and not stripped.startswith(markup)
    ]
    assert any("Better Thermostat" in line for line in prose), prose


@pytest.mark.quality_rule("docs-installation-instructions")
def test_the_install_steps_are_documented():
    sections = _sections(INSTALL_PAGE)
    install = [text for heading, text in sections.items() if "Install" in heading]
    assert install, list(sections)
    assert "HACS" in install[0]
    assert "restart Home Assistant" in install[0]


@pytest.mark.quality_rule("docs-removal-instructions")
def test_removing_the_integration_is_documented():
    removal = [
        f"{page.relative_to(DOCS)}: {heading}"
        for page in _docs_pages()
        for heading in _sections(page)
        if re.search(
            r"\b(remov|uninstall)\w* (the integration|Better Thermostat)\b",
            heading,
            re.IGNORECASE,
        )
    ]
    assert removal


@pytest.mark.quality_rule("docs-actions")
def test_every_action_has_a_section_listing_its_fields():
    services = yaml.safe_load((COMPONENT / "services.yaml").read_text(encoding="utf-8"))
    sections = {
        heading: text
        for page in _docs_pages()
        for heading, text in _sections(page).items()
    }
    undocumented = {}
    for name, spec in services.items():
        documented = [
            text
            for heading, text in sections.items()
            if f"better_thermostat.{name}" in heading
        ]
        fields = set((spec or {}).get("fields", {}))
        if not documented:
            undocumented[name] = "no section"
        elif missing := sorted(f for f in fields if f not in documented[0]):
            undocumented[name] = missing
    assert undocumented == {}


@pytest.mark.quality_rule("docs-conditions")
def test_every_device_condition_is_documented():
    docs = _docs_text()
    assert sorted(c for c in CONDITION_TYPES if f"`{c}`" not in docs) == []


@pytest.mark.quality_rule("docs-triggers")
def test_every_device_trigger_is_documented():
    docs = _docs_text()
    assert sorted(t for t in TRIGGER_TYPES if f"`{t}`" not in docs) == []
