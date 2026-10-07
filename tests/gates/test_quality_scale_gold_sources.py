"""The Gold rules of the Integration Quality Scale that the sources decide.

These rules are about what the repository ships, the strings, the icons, the
raised errors, the repair issues and the documentation, rather than about a
running instance; ``tests/integration/test_quality_scale_gold.py`` holds the
others.

The documentation rules can only be checked for a place to stand: a section
with the expected heading, a name the docs mention. Whether the text there is
any good stays a review question.
"""

import ast
import json
from pathlib import Path
import re

import pytest
import yaml

from custom_components.better_thermostat.utils.const import DOMAIN
from tests.quality_scale import REPO_ROOT

COMPONENT = REPO_ROOT / "custom_components" / "better_thermostat"
DOCS = REPO_ROOT / "docs"
DOCS_SITE = "https://better-thermostat.org/"

# Modules that define entities, where an icon could be set in code.
ENTITY_MODULES = ("climate.py", "number.py", "sensor.py", "switch.py", "entity.py")

HEADING = re.compile(r"^#{1,6}\s+(.*)$", re.MULTILINE)
PLACEHOLDER = re.compile(r"\{[^}]*\}")
CODE_BLOCK = re.compile(r"```ya?ml\n(.*?)```", re.DOTALL)

# What Home Assistant raises to tell a caller why an action or a setup failed.
TRANSLATABLE_ERRORS = frozenset(
    {
        "ConfigEntryAuthFailed",
        "ConfigEntryError",
        "ConfigEntryNotReady",
        "HomeAssistantError",
        "IntegrationError",
        "PlatformNotReady",
        "ServiceValidationError",
    }
)


def _strings() -> dict:
    return json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))


def _docs_pages() -> list[Path]:
    return sorted([*DOCS.rglob("*.md"), *DOCS.rglob("*.mdx")])


def _docs_text() -> str:
    """Return every page as one text, its whitespace collapsed, in one case."""
    text = "\n".join(page.read_text(encoding="utf-8") for page in _docs_pages())
    return re.sub(r"\s+", " ", text).casefold()


def _headings() -> list[str]:
    return [
        f"{page.relative_to(DOCS)}: {heading}"
        for page in _docs_pages()
        for heading in HEADING.findall(page.read_text(encoding="utf-8"))
    ]


def _has_heading(pattern: str) -> bool:
    return any(re.search(pattern, heading, re.IGNORECASE) for heading in _headings())


def _slugs() -> set[str]:
    """Return the path each page is published under.

    A page names its path in a ``slug`` front matter line; without one it is
    published under its file path.
    """
    slugs = set()
    for page in _docs_pages():
        text = page.read_text(encoding="utf-8")
        declared = re.search(r"^slug:\s*(\S+)\s*$", text, re.MULTILINE)
        slug = declared.group(1) if declared else str(page.relative_to(DOCS))
        slugs.add(re.sub(r"\.mdx?$", "", slug).strip("/").casefold())
    return slugs


def _trees() -> list[tuple[Path, ast.Module]]:
    return [
        (path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        for path in sorted(COMPONENT.rglob("*.py"))
    ]


def _calls(name: str) -> list[tuple[str, ast.Call]]:
    """Return every call of a function or method called ``name``, with its place."""
    found = []
    for path, tree in _trees():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = node.func
            called = (
                callee.attr
                if isinstance(callee, ast.Attribute)
                else getattr(callee, "id", "")
            )
            if called == name:
                found.append((f"{path.relative_to(REPO_ROOT)}:{node.lineno}", node))
    return found


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def _string_keywords(name: str) -> list[tuple[str, str]]:
    """Return every string literal passed as keyword ``name``, with its place."""
    found = []
    for path, tree in _trees():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and (value := _keyword(node, name)) is not None
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                found.append(
                    (f"{path.relative_to(REPO_ROOT)}:{node.lineno}", value.value)
                )
    return found


def _issue_calls() -> list[tuple[str, ast.Call]]:
    return _calls("async_create_issue")


@pytest.mark.quality_rule("docs-data-update")
def test_how_the_data_updates_is_documented():
    """A section says where the values come from and when they change."""
    assert _has_heading(r"\bdata updates?\b"), _headings()


@pytest.mark.quality_rule("docs-examples")
def test_an_automation_example_is_documented():
    """At least one YAML example in the docs is a whole automation."""
    automations = []
    for page in _docs_pages():
        for block in CODE_BLOCK.findall(page.read_text(encoding="utf-8")):
            try:
                document = yaml.safe_load(block)
            except yaml.YAMLError:
                continue
            for item in document if isinstance(document, list) else [document]:
                if (
                    isinstance(item, dict)
                    and {"trigger", "triggers"} & set(item)
                    and {"action", "actions"} & set(item)
                ):
                    automations.append(page.relative_to(DOCS))
    assert automations


@pytest.mark.quality_rule("docs-known-limitations")
def test_the_known_limitations_are_documented():
    assert _has_heading(r"\bknown limitations\b"), _headings()


@pytest.mark.quality_rule("docs-supported-devices")
def test_the_supported_devices_are_documented():
    assert _has_heading(r"\bdevices\b.*\bsupported\b|\bsupported devices\b")


@pytest.mark.quality_rule("docs-supported-functions")
def test_every_entity_is_documented():
    """Every entity the integration can create is named in the docs.

    An entity is named by the name the frontend shows, without the thermostat
    name some of them carry. A key without a name only translates states shown
    elsewhere, and is no entity.
    """
    docs = _docs_text()
    undocumented = []
    for platform, entities in _strings()["entity"].items():
        for key, entity in entities.items():
            if "name" not in entity:
                continue
            name = re.sub(r"\s+", " ", PLACEHOLDER.sub(" ", entity["name"])).strip()
            if name.casefold() not in docs:
                undocumented.append(f"{platform}.{key}: {name}")
    assert undocumented == []


@pytest.mark.quality_rule("docs-troubleshooting")
def test_every_repair_issue_points_to_a_page_that_explains_it():
    """A repair issue links to the page that says what to do about it.

    The link is passed as ``learn_more_url`` where the issue is raised, either
    written there or carried by the object the call reads it from. For
    everything no issue reports, the docs say how to turn on debug logging.
    """
    unlinked = [
        where
        for where, call in _issue_calls()
        if _keyword(call, "learn_more_url") is None
    ]
    assert unlinked == []

    slugs = _slugs()
    dangling = [
        f"{where} {url}"
        for where, url in _string_keywords("learn_more_url")
        if not url.startswith(DOCS_SITE)
        or url.removeprefix(DOCS_SITE).strip("/").casefold() not in slugs
    ]
    assert dangling == []
    assert _has_heading(r"\bdebug logging\b"), _headings()


@pytest.mark.quality_rule("docs-use-cases")
def test_the_use_cases_are_documented():
    assert _has_heading(r"\buse cases?\b"), _headings()


@pytest.mark.quality_rule("exception-translations")
def test_every_raised_error_is_translated():
    """An error a caller sees names a message in ``strings.json``.

    A key computed at the call site cannot be checked here and is refused, so
    that every message has a literal key to look up.
    """
    messages = _strings().get("exceptions", {})
    untranslated = []
    for name in TRANSLATABLE_ERRORS:
        for where, call in _calls(name):
            key = _keyword(call, "translation_key")
            domain = _keyword(call, "translation_domain")
            if not (
                isinstance(key, ast.Constant)
                and key.value in messages
                and (
                    (isinstance(domain, ast.Name) and domain.id == "DOMAIN")
                    or (isinstance(domain, ast.Constant) and domain.value == DOMAIN)
                )
            ):
                untranslated.append(f"{where} {name}")
    assert sorted(untranslated) == []


@pytest.mark.quality_rule("icon-translations")
def test_no_entity_sets_its_icon_in_code():
    """Icons come from ``icons.json``, keyed by the entity's translation key."""
    in_code = []
    for name in ENTITY_MODULES:
        path = COMPONENT / name
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and node.name == "icon"
            ) or (
                isinstance(node, ast.Assign | ast.AnnAssign)
                and any(
                    getattr(target, "id", getattr(target, "attr", None)) == "_attr_icon"
                    for target in (
                        node.targets if isinstance(node, ast.Assign) else [node.target]
                    )
                )
            ):
                in_code.append(f"{name}:{node.lineno}")
    assert in_code == []


@pytest.mark.quality_rule("icon-translations")
def test_every_icon_belongs_to_an_entity_or_action_that_exists():
    icons = json.loads((COMPONENT / "icons.json").read_text(encoding="utf-8"))
    entities = _strings()["entity"]
    services = yaml.safe_load((COMPONENT / "services.yaml").read_text(encoding="utf-8"))

    orphans = [
        f"{platform}.{key}"
        for platform, keyed in icons.get("entity", {}).items()
        for key in keyed
        if key not in entities.get(platform, {})
    ]
    orphans += [
        f"services.{name}" for name in icons.get("services", {}) if name not in services
    ]
    assert orphans == []


@pytest.mark.quality_rule("repair-issues")
def test_every_repair_issue_has_a_translated_message():
    """Each issue raised names a title and description in ``strings.json``.

    An issue whose key is read from an object is checked through the keys
    such objects are built with.
    """
    issues = _strings()["issues"]
    unknown = []
    for where, call in _issue_calls():
        key = _keyword(call, "translation_key")
        if isinstance(key, ast.Constant):
            if key.value not in issues:
                unknown.append(f"{where} {key.value}")
        elif not isinstance(key, ast.Attribute):
            unknown.append(f"{where} {ast.unparse(key) if key else None}")
    unknown += [
        f"{where} {key}"
        for where, key in _string_keywords("issue_translation_key")
        if key not in issues
    ]
    assert unknown == []
    # An issue the user can fix explains itself in the steps of its fix flow.
    unexplained = [
        key
        for key, issue in issues.items()
        if "title" not in issue or not {"description", "fix_flow"} & set(issue)
    ]
    assert unexplained == []
