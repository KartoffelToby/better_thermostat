"""Consistency checks between the documentation and the shipped service list.

The published site is built straight from ``docs/``, so a service name that only
exists in prose is a broken instruction for every reader. These checks tie the
documentation to ``services.yaml``: a ``better_thermostat.<name>`` token in a doc
page or a bundled blueprint has to name a service the integration registers.

``RETIRED`` holds the names the integration does not register any more. They are
allowed in exactly one file, the migration guide that tells readers what to call
instead, and nowhere in anything the integration ships.
"""

from pathlib import Path
import re

import yaml

REPO = Path(__file__).resolve().parents[2]
COMPONENT = REPO / "custom_components" / "better_thermostat"
SERVICES_YAML = COMPONENT / "services.yaml"
MIGRATION_DOC = REPO / "docs" / "deep-explanations" / "schedule-and-night-mode.md"
SCHEDULE_DOC = REPO / "docs" / "schedule.md"
ASTRO_CONFIG = REPO / "website" / "astro.config.mjs"

RETIRED = {
    "save_current_target_temperature",
    "restore_saved_target_temperature",
    "set_temp_target_temperature",
}

SERVICE_REFERENCE = re.compile(r"better_thermostat\.([a-z0-9_]+)")


def _documentation_files() -> list[Path]:
    docs = REPO / "docs"
    files = [*docs.rglob("*.md"), *docs.rglob("*.mdx")]
    files += (REPO / "blueprints").glob("*.yaml")
    return sorted(files)


def _shipped_files() -> list[Path]:
    files = [SERVICES_YAML, COMPONENT / "strings.json"]
    files += (COMPONENT / "translations").glob("*.json")
    files += (REPO / "blueprints").glob("*.yaml")
    files += COMPONENT.rglob("*.py")
    return sorted(files)


def _declared_services() -> set[str]:
    return set(yaml.safe_load(SERVICES_YAML.read_text(encoding="utf-8")))


def _referenced_services(path: Path) -> set[str]:
    return set(SERVICE_REFERENCE.findall(path.read_text(encoding="utf-8")))


def _rel(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


def test_docs_reference_only_declared_services():
    allowed = _declared_services() | RETIRED
    unknown = {
        _rel(path): sorted(_referenced_services(path) - allowed)
        for path in _documentation_files()
        if _referenced_services(path) - allowed
    }
    assert not unknown, (
        f"documentation refers to services that are not in services.yaml: {unknown}"
    )


def test_retired_service_names_appear_only_in_the_migration_doc():
    offenders = {}
    for path in _documentation_files():
        if path == MIGRATION_DOC:
            continue
        text = path.read_text(encoding="utf-8")
        stale = sorted(name for name in RETIRED if name in text)
        if stale:
            offenders[_rel(path)] = stale
    assert not offenders, (
        "retired service names belong only in "
        f"{_rel(MIGRATION_DOC)}, but were found in: {offenders}"
    )


def test_retired_service_names_are_not_shipped():
    offenders = {}
    for path in _shipped_files():
        text = path.read_text(encoding="utf-8")
        stale = sorted(name for name in RETIRED if name in text)
        if stale:
            offenders[_rel(path)] = stale
    assert not offenders, f"retired service names are still shipped in: {offenders}"


def test_scanner_finds_service_references():
    found: set[str] = set()
    for path in _documentation_files():
        found |= _referenced_services(path)
    assert found, (
        "the scan matched no service reference at all, so the other checks in "
        "this module would pass vacuously"
    )


def test_schedule_page_is_gone_and_redirected():
    assert not SCHEDULE_DOC.exists(), (
        f"{_rel(SCHEDULE_DOC)} duplicates {_rel(MIGRATION_DOC)} and must stay deleted"
    )
    config = ASTRO_CONFIG.read_text(encoding="utf-8")
    assert '"/schedule": "/deep-explanations/schedule-and-night-mode"' in config, (
        f"{_rel(ASTRO_CONFIG)} must redirect the retired /schedule URL"
    )
