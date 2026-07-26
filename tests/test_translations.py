"""Consistency checks for the Better Thermostat localization catalogs."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re

import pytest
import yaml

ROOT = Path(__file__).parents[1]
COMPONENT = ROOT / "custom_components" / "better_thermostat"
TRANSLATIONS = COMPONENT / "translations"
PROJECT_INLANG = ROOT / "project.inlang.json"

ALL_LANGUAGES = sorted(path.stem for path in TRANSLATIONS.glob("*.json"))
LANGUAGES = [language for language in ALL_LANGUAGES if language != "en"]

ENTITY_TRANSLATION_KEYS = {
    "sensor": {
        "external_temp_ema",
        "external_temp_ema_1h",
        "temp_slope",
        "heating_power",
        "heat_loss",
        "virtual_temp",
        "mpc_gain",
        "mpc_loss",
        "mpc_ka",
        "solar_intensity",
    },
    "number": {
        "preset_eco",
        "preset_away",
        "preset_boost",
        "preset_comfort",
        "preset_home",
        "preset_sleep",
        "preset_activity",
        "pid_kp",
        "pid_ki",
        "pid_kd",
        "pid_kp_no_trv",
        "pid_ki_no_trv",
        "pid_kd_no_trv",
        "valve_max_opening",
        "valve_max_opening_no_trv",
    },
    "switch": {
        "pid_auto_tune",
        "pid_auto_tune_no_trv",
        "child_lock",
        "child_lock_no_trv",
    },
}


def _flatten(obj: dict, prefix: str = "") -> dict[str, object]:
    """Flatten nested dictionary values into dotted-key paths."""
    flat: dict[str, object] = {}
    for key, value in obj.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(_flatten(value, path))
        else:
            flat[path] = value
    return flat


def _load_json(path: Path) -> dict:
    """Load a JSON object from path."""
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict), f"{path} must contain a JSON object"
    return value


def _load(path: Path) -> dict[str, object]:
    """Load and flatten a translation catalog."""
    return _flatten(_load_json(path))


def _placeholders(text: object) -> list[str]:
    """Return sorted placeholder tokens found in a translation string."""
    assert isinstance(text, str), f"translation value must be a string, got {text!r}"
    return sorted(re.findall(r"\{[^}]*\}", text))


def test_strings_json_matches_en_json():
    """The authoring source and runtime English catalog must be identical."""
    assert _load(COMPONENT / "strings.json") == _load(TRANSLATIONS / "en.json")


def test_inlang_languages_match_translation_catalogs():
    """Every catalog must be discoverable by the Inlang contributor tooling."""
    project = _load_json(PROJECT_INLANG)

    assert project["sourceLanguageTag"] == "en"
    assert sorted(project["languageTags"]) == ALL_LANGUAGES
    assert (
        project["plugin.inlang.i18next"]["pathPattern"]
        == "./custom_components/better_thermostat/translations/{languageTag}.json"
    )


@pytest.mark.parametrize("lang", ALL_LANGUAGES)
def test_translation_values_are_non_empty_strings(lang: str):
    """Catalog leaves must contain non-empty text rather than nulls or objects."""
    invalid = {
        key: value
        for key, value in _load(TRANSLATIONS / f"{lang}.json").items()
        if not isinstance(value, str) or not value.strip()
    }
    assert not invalid, f"{lang}.json has invalid translation values: {invalid}"


@pytest.mark.parametrize("lang", LANGUAGES)
def test_no_unknown_keys(lang: str):
    """Translation files must not contain keys absent from English."""
    english = _load(TRANSLATIONS / "en.json")
    translated = _load(TRANSLATIONS / f"{lang}.json")
    unknown = sorted(key for key in translated if key not in english)
    assert not unknown, f"{lang}.json has keys unknown to en.json: {unknown}"


@pytest.mark.parametrize("lang", LANGUAGES)
def test_all_keys_translated(lang: str):
    """Every language must cover every runtime English key."""
    english = _load(TRANSLATIONS / "en.json")
    translated = _load(TRANSLATIONS / f"{lang}.json")
    missing = sorted(key for key in english if key not in translated)
    assert not missing, f"{lang}.json is missing keys: {missing}"


@pytest.mark.parametrize("lang", LANGUAGES)
def test_placeholders_match(lang: str):
    """Shared keys must preserve exactly the English placeholders."""
    english = _load(TRANSLATIONS / "en.json")
    translated = _load(TRANSLATIONS / f"{lang}.json")
    mismatched = {
        key: (english[key], translated[key])
        for key in english.keys() & translated.keys()
        if _placeholders(english[key]) != _placeholders(translated[key])
    }
    assert not mismatched, f"{lang}.json placeholder mismatch: {mismatched}"


def test_entity_translation_catalog_covers_platform_keys():
    """All stable translation keys used by entity platforms must be cataloged."""
    english_entities = _load_json(TRANSLATIONS / "en.json").get("entity", {})

    for platform, expected_keys in ENTITY_TRANSLATION_KEYS.items():
        actual_keys = set(english_entities.get(platform, {}))
        missing = expected_keys - actual_keys
        assert not missing, f"entity.{platform} is missing translation keys: {missing}"


def _is_attr_name_target(target: ast.expr) -> bool:
    """Return whether an assignment target writes Home Assistant's name field."""
    return (isinstance(target, ast.Name) and target.id == "_attr_name") or (
        isinstance(target, ast.Attribute) and target.attr == "_attr_name"
    )


@pytest.mark.parametrize("platform", ["sensor.py", "number.py", "switch.py"])
def test_entity_platforms_do_not_hardcode_natural_language_names(platform: str):
    """Entity names must use translation keys instead of English Python text."""
    source_path = COMPONENT / platform
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    hardcoded: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value

        if value is None or not any(_is_attr_name_target(target) for target in targets):
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            hardcoded.append((node.lineno, value.value))
        elif isinstance(value, ast.JoinedStr):
            hardcoded.append((node.lineno, ast.unparse(value)))

    assert not hardcoded, (
        f"{platform} hardcodes user-facing entity names instead of "
        f"translation_key: {hardcoded}"
    )


def test_services_yaml_covered():
    """Every service and service field in services.yaml has an English string."""
    services = yaml.safe_load((COMPONENT / "services.yaml").read_text(encoding="utf-8"))
    english = _load_json(TRANSLATIONS / "en.json")
    translated_services = english.get("services", {})

    assert set(services) == set(translated_services)
    for name, spec in services.items():
        expected_fields = set(spec.get("fields", {}))
        translated_fields = set(translated_services[name].get("fields", {}))
        assert expected_fields == translated_fields, (
            f"service {name}: fields in services.yaml and en.json differ"
        )
