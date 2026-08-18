"""Regression tests for localized config-flow selector values."""

from custom_components.better_thermostat.config_flow import _normalize_user_submission
from custom_components.better_thermostat.utils.const import CONF_TARGET_TEMP_STEP


def test_new_auto_target_temperature_step_is_preserved():
    """The translated Auto selector must persist the new empty-string value."""
    normalized = _normalize_user_submission(
        {CONF_TARGET_TEMP_STEP: "auto"}, mode="create"
    )

    assert normalized[CONF_TARGET_TEMP_STEP] == ""


def test_legacy_auto_target_temperature_step_is_preserved():
    """The legacy Auto selector must continue to persist 0.0."""
    normalized = _normalize_user_submission(
        {CONF_TARGET_TEMP_STEP: "auto_legacy"}, mode="create"
    )

    assert normalized[CONF_TARGET_TEMP_STEP] == "0.0"


def test_missing_target_temperature_step_uses_legacy_default():
    """A genuinely missing selector value must retain the existing fallback."""
    normalized = _normalize_user_submission({}, mode="create")

    assert normalized[CONF_TARGET_TEMP_STEP] == "0.0"


def test_empty_target_temperature_step_uses_legacy_default():
    """An explicit empty selector value must use the legacy fallback."""
    normalized = _normalize_user_submission({CONF_TARGET_TEMP_STEP: ""}, mode="create")

    assert normalized[CONF_TARGET_TEMP_STEP] == "0.0"
