from __future__ import annotations

from functools import cache
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


def _flatten(obj: dict, prefix: str) -> dict[str, str]:
    """Flatten a catalog into Home Assistant's dotted translation keys."""
    flat: dict[str, str] = {}
    for key, value in obj.items():
        path = f"{prefix}.{key}"
        if isinstance(value, dict):
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
def mock_async_get_translations():
    """Serve Better Thermostat's own catalog instead of hitting the store.

    Entities name themselves from ``translation_key``, so the catalog has to
    be readable for a translated entity to get a name at all — and the name
    is what Home Assistant derives the entity_id from.
    """

    async def _get_translations(hass, language, category, integrations=None, *args):
        if integrations is not None and DOMAIN not in integrations:
            return {}
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
