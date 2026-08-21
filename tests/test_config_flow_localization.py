"""Regression tests for localized config-flow selector values."""

from custom_components.better_thermostat.config_flow import (
    _build_user_fields,
    _normalize_user_submission,
)
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


def _target_step_default(fields):
    """Return the schema default the form offers for the target temperature step."""
    for marker in fields:
        if marker == CONF_TARGET_TEMP_STEP:
            return marker.default()
    raise AssertionError("target temperature step field missing from the form")


def test_redisplayed_form_keeps_the_submitted_selector_token():
    """A form rebuilt after a validation error keeps the token the user picked."""
    for token in ("auto", "auto_legacy", "step_0_5"):
        fields = _build_user_fields(
            mode="create",
            current={CONF_TARGET_TEMP_STEP: "0.1"},
            user_input={CONF_TARGET_TEMP_STEP: token},
        )

        assert _target_step_default(fields) == token


def test_form_maps_stored_values_to_selector_tokens():
    """A freshly built form translates the stored value into its selector token."""
    for stored, token in (("", "auto"), ("0.0", "auto_legacy"), ("0.5", "step_0_5")):
        fields = _build_user_fields(
            mode="update", current={CONF_TARGET_TEMP_STEP: stored}
        )

        assert _target_step_default(fields) == token
