"""Consistency checks for strings.json and the translation files."""

import json
from pathlib import Path
import re

import pytest
import yaml

COMPONENT = Path(__file__).parents[1] / "custom_components" / "better_thermostat"
TRANSLATIONS = COMPONENT / "translations"

LANGUAGES = sorted(p.stem for p in TRANSLATIONS.glob("*.json") if p.stem != "en")


def _flatten(obj: dict, prefix: str = "") -> dict[str, str]:
    """Flatten nested dict values into dotted-key paths.

    Parameters
    ----------
    obj : dict
        Mapping to flatten.
    prefix : str
        Prefix for generated dotted paths.

    Returns
    -------
    dict[str, str]
        Flattened mapping where nested keys are represented as dotted paths.
    """
    flat: dict[str, str] = {}
    for key, value in obj.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(_flatten(value, path))
        else:
            flat[path] = value
    return flat


def _load(path: Path) -> dict[str, str]:
    """Load a JSON file and flatten it into dotted-key paths.

    Parameters
    ----------
    path : Path
        JSON file path to read.

    Returns
    -------
    dict[str, str]
        Flattened JSON mapping for translation key comparisons.
    """
    return _flatten(json.loads(path.read_text(encoding="utf-8")))


def _placeholders(text: str) -> list[str]:
    """Return sorted placeholder tokens found in a string.

    Parameters
    ----------
    text : str
        Input text that may contain placeholders such as ``{name}``.

    Returns
    -------
    list[str]
        Sorted placeholder tokens extracted from the text.
    """
    return sorted(re.findall(r"\{[^}]*\}", text))


def test_strings_json_matches_en_json():
    """strings.json (source) and translations/en.json must be identical."""
    assert _load(COMPONENT / "strings.json") == _load(TRANSLATIONS / "en.json")


@pytest.mark.parametrize("lang", LANGUAGES)
def test_no_unknown_keys(lang: str):
    """Translation files must not contain keys that do not exist in en.json."""
    en = _load(TRANSLATIONS / "en.json")
    translated = _load(TRANSLATIONS / f"{lang}.json")
    unknown = sorted(key for key in translated if key not in en)
    assert not unknown, f"{lang}.json has keys unknown to en.json: {unknown}"


@pytest.mark.parametrize("lang", LANGUAGES)
def test_all_keys_translated(lang: str):
    """Translation files must cover every key of en.json."""
    en = _load(TRANSLATIONS / "en.json")
    translated = _load(TRANSLATIONS / f"{lang}.json")
    missing = sorted(key for key in en if key not in translated)
    assert not missing, f"{lang}.json is missing keys: {missing}"


@pytest.mark.parametrize("lang", LANGUAGES)
def test_placeholders_match(lang: str):
    """Shared keys must use exactly the same placeholders as en.json."""
    en = _load(TRANSLATIONS / "en.json")
    translated = _load(TRANSLATIONS / f"{lang}.json")
    mismatched = {
        key: (en[key], translated[key])
        for key in en.keys() & translated.keys()
        if _placeholders(en[key]) != _placeholders(translated[key])
    }
    assert not mismatched, f"{lang}.json placeholder mismatch: {mismatched}"


def test_services_yaml_covered():
    """Every service and service field in services.yaml has a translation."""
    services = yaml.safe_load((COMPONENT / "services.yaml").read_text(encoding="utf-8"))
    en = json.loads((TRANSLATIONS / "en.json").read_text(encoding="utf-8"))
    translated_services = en.get("services", {})

    assert set(services) == set(translated_services)
    for name, spec in services.items():
        expected_fields = set(spec.get("fields", {}))
        translated_fields = set(translated_services[name].get("fields", {}))
        assert expected_fields == translated_fields, (
            f"service {name}: fields in services.yaml and en.json differ"
        )
