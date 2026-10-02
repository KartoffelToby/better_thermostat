"""Regression tests for localized config-flow selector values."""

from custom_components.better_thermostat.config_flow import (
    _build_user_fields,
    _normalize_user_submission,
)
from custom_components.better_thermostat.utils.const import (
    CONF_TARGET_TEMP_MAX,
    CONF_TARGET_TEMP_MIN,
    CONF_TARGET_TEMP_STEP,
    TARGET_TEMP_BOUND_AUTO,
)


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


def _bound_default(fields, key):
    """Return the schema default the form offers for a target-range bound."""
    for marker in fields:
        if marker == key:
            return marker.default()
    raise AssertionError(f"{key} missing from the form")


def test_unset_target_temperature_bounds_stay_on_auto():
    """An entry that names no bounds keeps deriving them from its devices."""
    normalized = _normalize_user_submission({}, mode="create")

    assert normalized[CONF_TARGET_TEMP_MIN] == TARGET_TEMP_BOUND_AUTO
    assert normalized[CONF_TARGET_TEMP_MAX] == TARGET_TEMP_BOUND_AUTO


def test_selected_target_temperature_bounds_are_stored_as_degrees():
    """A picked bound is persisted as the degree value, not as its token."""
    normalized = _normalize_user_submission(
        {CONF_TARGET_TEMP_MIN: "min_max_16", CONF_TARGET_TEMP_MAX: "min_max_24"},
        mode="create",
    )

    assert normalized[CONF_TARGET_TEMP_MIN] == "16.0"
    assert normalized[CONF_TARGET_TEMP_MAX] == "24.0"


def test_form_maps_stored_bounds_to_selector_tokens():
    """A form built from a stored entry offers the token of each stored bound."""
    fields = _build_user_fields(
        mode="update",
        current={
            CONF_TARGET_TEMP_MIN: "16.0",
            CONF_TARGET_TEMP_MAX: TARGET_TEMP_BOUND_AUTO,
        },
    )

    assert _bound_default(fields, CONF_TARGET_TEMP_MIN) == "min_max_16"
    assert _bound_default(fields, CONF_TARGET_TEMP_MAX) == "auto"


def test_redisplayed_form_keeps_the_submitted_bound_tokens():
    """A form rebuilt after a validation error keeps the bounds the user picked."""
    fields = _build_user_fields(
        mode="update",
        current={CONF_TARGET_TEMP_MIN: "5.0", CONF_TARGET_TEMP_MAX: "30.0"},
        user_input={
            CONF_TARGET_TEMP_MIN: "min_max_25",
            CONF_TARGET_TEMP_MAX: "min_max_20",
        },
    )

    assert _bound_default(fields, CONF_TARGET_TEMP_MIN) == "min_max_25"
    assert _bound_default(fields, CONF_TARGET_TEMP_MAX) == "min_max_20"


def test_a_minimum_above_the_maximum_is_reported_on_the_minimum():
    """An inverted range is rejected, and the message lands on the field to fix."""
    errors: dict[str, str] = {}

    _normalize_user_submission(
        {CONF_TARGET_TEMP_MIN: "min_max_25", CONF_TARGET_TEMP_MAX: "min_max_20"},
        mode="create",
        errors=errors,
    )

    assert errors == {CONF_TARGET_TEMP_MIN: "target_temp_min_above_max"}


def test_equal_target_temperature_bounds_are_accepted():
    """Pinning the setpoint to a single degree is a valid range, not an error."""
    errors: dict[str, str] = {}

    _normalize_user_submission(
        {CONF_TARGET_TEMP_MIN: "min_max_21", CONF_TARGET_TEMP_MAX: "min_max_21"},
        mode="create",
        errors=errors,
    )

    assert errors == {}


def test_a_bound_left_on_auto_never_conflicts_with_the_other():
    """Auto imposes no limit, so it cannot be the wrong side of the other bound."""
    for submission in (
        {CONF_TARGET_TEMP_MIN: "min_max_25", CONF_TARGET_TEMP_MAX: "auto"},
        {CONF_TARGET_TEMP_MIN: "auto", CONF_TARGET_TEMP_MAX: "min_max_5"},
    ):
        errors: dict[str, str] = {}

        _normalize_user_submission(submission, mode="create", errors=errors)

        assert errors == {}
