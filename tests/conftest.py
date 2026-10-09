from __future__ import annotations

from collections.abc import Mapping
from functools import cache
import gc
import json
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

# Ensure repository root is importable even when pytest runs in importlib mode.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DOMAIN = "better_thermostat"
ENGLISH_CATALOG = REPO_ROOT / "custom_components" / DOMAIN / "translations" / "en.json"


type Catalog = Mapping[str, str | Catalog]


def _flatten(obj: Catalog, prefix: str) -> dict[str, str]:
    """Flatten a catalog into Home Assistant's dotted translation keys."""
    flat: dict[str, str] = {}
    for key, value in obj.items():
        path = f"{prefix}.{key}"
        if isinstance(value, Mapping):
            flat.update(_flatten(value, path))
        else:
            flat[path] = value
    return flat


@cache
def _english_translations() -> dict[str, str]:
    """Return the English catalog keyed the way Home Assistant serves it."""
    catalog = json.loads(ENGLISH_CATALOG.read_text(encoding="utf-8"))
    return _flatten(catalog, f"component.{DOMAIN}")


@pytest.fixture(autouse=True)
def _collect_garbage_after_each_test():
    """Collect each test's garbage before the next test starts.

    An unawaited coroutine held in a reference cycle is only reported when
    the cycle is collected. Collected here, the error lands on the test that
    leaked it instead of on whichever later test triggers a collection.
    """
    yield
    gc.collect()


@pytest.fixture(autouse=True)
def mock_async_get_translations():
    """Serve Better Thermostat's own catalog instead of hitting the store.

    Entities name themselves from ``translation_key``, so the catalog has to
    be readable for a translated entity to get a name at all — and the name
    is what Home Assistant derives the entity_id from.
    """

    async def _get_translations(hass, language, category, integrations=None, *args):
        if integrations is not None and DOMAIN not in integrations:
            return dict[str, str]()
        prefix = f"component.{DOMAIN}.{category}."
        return {
            key: value
            for key, value in _english_translations().items()
            if key.startswith(prefix)
        }

    with patch(
        "homeassistant.helpers.translation.async_get_translations",
        side_effect=_get_translations,
    ) as mock_get_translations:
        yield mock_get_translations


def pytest_configure(config):
    """Register the marker that ties a test to a Quality Scale rule."""
    config.addinivalue_line(
        "markers",
        "quality_rule(name): the Integration Quality Scale rule the test holds",
    )


def pytest_collection_modifyitems(config, items):
    """Run the tests of a rule still marked ``todo`` as strict ``xfail``.

    ``quality_scale.yaml`` decides, so the file and the tests cannot disagree:
    a todo rule whose test starts to pass fails the run until the file says
    ``done``, and a done rule whose test fails fails it as any test does.
    """
    marked = {
        item: [marker.args[0] for marker in item.iter_markers("quality_rule")]
        for item in items
    }
    marked = {item: rules for item, rules in marked.items() if rules}
    if not marked:
        return
    # Imported only when a marked test was collected: the suite's own gates
    # run this conftest from a copy outside the repository.
    from tests.quality_scale import rule_status

    for item, rules in marked.items():
        todo = [rule for rule in rules if rule_status(rule) == "todo"]
        if not todo:
            continue
        # An xfail covers the whole test, so it would also swallow a failure
        # of a rule the file already records as done.
        if len(todo) < len(rules):
            raise pytest.UsageError(
                f"{item.nodeid} holds the todo rule {todo[0]} together with "
                "rules that are not todo; give each status its own test"
            )
        item.add_marker(
            pytest.mark.xfail(
                strict=True, reason=f"quality scale rule {todo[0]} is todo"
            )
        )
