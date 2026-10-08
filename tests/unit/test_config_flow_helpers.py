"""The config flow's helpers read stored and submitted values of any age.

An entry written by an older version, or a form Home Assistant redisplays
after an error, hands these helpers values in shapes the current flow never
produces itself: strings for booleans, a single thermostat instead of a list,
a missing tolerance, a retired ``balance_mode``. Each one has to come out as
the value the current flow would have stored.
"""

from unittest.mock import AsyncMock, patch

from homeassistant.const import CONF_NAME
import pytest

from custom_components.better_thermostat.config_flow import (
    _USER_FIELD_DEFAULTS,
    OptionsFlowHandler,
    _as_bool,
    _build_advanced_fields,
    _build_user_fields,
    _duration_dict_to_seconds,
    _load_adapter_info,
    _normalize_advanced_submission,
    _normalize_user_submission,
    _quirk_valve_support,
    _seconds_to_duration_dict,
    _stored_thermostats,
    _trv_supports_auto,
    _TrvDraft,
)
from custom_components.better_thermostat.utils.const import (
    CONF_CALIBRATION_MODE,
    CONF_OFF_TEMPERATURE,
    CONF_TARGET_TEMP_MAX,
    CONF_TARGET_TEMP_MIN,
    CONF_TEMPERATURE_SENSOR,
    CONF_THERMOSTAT,
    CONF_TOLERANCE,
    CONF_WINDOW_OFF_DELAY,
    DEFAULT_CALIBRATION_MODE,
    TARGET_TEMP_BOUND_AUTO,
    CalibrationMode,
)
from custom_components.better_thermostat.utils.helpers import (
    configured_calibration_mode,
)

_MODULE = "custom_components.better_thermostat.config_flow"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("true", True),
        (" On ", True),
        ("1", True),
        ("false", False),
        ("off", False),
        ("0", False),
        ("", False),
        (1, True),
        (0, False),
    ],
)
def test_a_stored_flag_reads_as_the_boolean_it_spells(value, expected):
    assert _as_bool(value) is expected


def test_a_missing_flag_takes_the_default():
    assert _as_bool(None, default=True) is True


@pytest.mark.parametrize(
    ("duration", "seconds"),
    [
        (None, 0),
        (-5, 0),
        (90.7, 90),
        (float("nan"), 0),
        (float("inf"), 0),
        ({"minutes": 2, "seconds": 5}, 125),
        ({"minutes": "two"}, 0),
        ("ninety", 0),
    ],
)
def test_a_delay_is_stored_as_whole_seconds_never_below_zero(duration, seconds):
    assert _duration_dict_to_seconds(duration) == seconds


def _offered_calibration_mode(stored):
    fields = _build_advanced_fields(
        sources=[stored],
        default_calibration="target_temp_based",
        homematic=False,
        has_auto=False,
    )
    return next(
        marker.default() for marker in fields if marker == CONF_CALIBRATION_MODE
    )


def test_a_retired_pid_balance_mode_becomes_the_pid_calibration_mode():
    stored = {"balance_mode": "pid"}

    assert _offered_calibration_mode(stored) == CalibrationMode.PID_CALIBRATION.value
    assert stored == {"balance_mode": "pid"}


@pytest.mark.parametrize("balance_mode", ["heuristic", "none"])
def test_another_retired_balance_mode_falls_back_to_the_default_mode(balance_mode):
    stored = {"balance_mode": balance_mode}

    assert _offered_calibration_mode(stored) == DEFAULT_CALIBRATION_MODE.value
    assert stored == {"balance_mode": balance_mode}


def test_a_retired_balance_mode_leaves_a_chosen_calibration_mode_alone():
    stored = {"balance_mode": "heuristic", CONF_CALIBRATION_MODE: "tpi_calibration"}

    assert _offered_calibration_mode(stored) == "tpi_calibration"


def test_a_retired_pid_balance_mode_overrides_a_stored_calibration_mode():
    stored = {"balance_mode": "pid", CONF_CALIBRATION_MODE: "tpi_calibration"}

    assert _offered_calibration_mode(stored) == CalibrationMode.PID_CALIBRATION.value


def test_without_a_thermostat_there_is_no_auto_mode_to_offer():
    assert _trv_supports_auto(None, None) is False


async def test_a_model_that_cannot_be_read_does_not_claim_valve_support():
    with patch(
        f"{_MODULE}.get_device_model", AsyncMock(side_effect=RuntimeError("no model"))
    ):
        assert await _quirk_valve_support(None, "climate.trv") is False


async def test_a_thermostat_without_an_integration_reports_no_channels():
    with patch(f"{_MODULE}._quirk_valve_support", AsyncMock(return_value=False)):
        adapter, info = await _load_adapter_info(None, None, "climate.trv")

    assert (adapter, info) == (None, {})


async def test_an_adapter_without_capabilities_reports_no_channels():
    adapter = object()
    with patch(f"{_MODULE}._quirk_valve_support", AsyncMock(return_value=False)):
        loaded, info = await _load_adapter_info(
            None, "mqtt", "climate.trv", existing_adapter=adapter
        )

    assert (loaded, info) == (adapter, {})


async def test_an_adapter_whose_capability_query_fails_reports_no_channels():
    class _FailingAdapter:
        async def get_info(self, _flow, _entity_id):
            raise RuntimeError("device offline")

    with patch(f"{_MODULE}._quirk_valve_support", AsyncMock(return_value=False)):
        _adapter, info = await _load_adapter_info(
            None, "mqtt", "climate.trv", existing_adapter=_FailingAdapter()
        )

    assert info == {}


def _default_of(fields, key):
    return next(marker.default() for marker in fields if marker == key)


def test_a_stored_value_the_form_cannot_show_is_offered_as_its_default():
    """A tolerance or threshold that does not parse is replaced, not shown."""
    fields = _build_user_fields(
        mode="update",
        current={CONF_TOLERANCE: "warm", CONF_OFF_TEMPERATURE: "cold"},
        system_unit="°C",
    )

    assert _default_of(fields, CONF_TOLERANCE) == _USER_FIELD_DEFAULTS[CONF_TOLERANCE]
    assert _default_of(fields, CONF_OFF_TEMPERATURE) == 20


def test_a_delay_stored_as_a_duration_is_offered_unchanged():
    stored = {"hours": 0, "minutes": 1, "seconds": 0}

    fields = _build_user_fields(
        mode="update", current={CONF_WINDOW_OFF_DELAY: stored}, system_unit="°C"
    )

    assert _default_of(fields, CONF_WINDOW_OFF_DELAY) == stored


def _submission(**overrides):
    return {CONF_NAME: "Room", CONF_TEMPERATURE_SENSOR: "sensor.temp"} | overrides


@pytest.mark.parametrize(
    ("heaters", "expected"),
    [
        (None, []),
        ("climate.trv", ["climate.trv"]),
        (
            [{"trv": "climate.a"}, {"trv": ""}, {"trv": "climate.b"}],
            ["climate.a", "climate.b"],
        ),
    ],
)
def test_the_thermostats_come_out_as_a_list_of_entity_ids(heaters, expected):
    normalized = _normalize_user_submission(
        _submission(**{CONF_THERMOSTAT: heaters}), mode="create", base=None
    )

    assert normalized[CONF_THERMOSTAT] == expected


@pytest.mark.parametrize("value", [None, "warm"])
def test_a_cleared_threshold_and_tolerance_take_their_defaults(value):
    normalized = _normalize_user_submission(
        _submission(**{CONF_OFF_TEMPERATURE: value, CONF_TOLERANCE: value}),
        mode="create",
        base=None,
        system_unit="°C",
    )

    assert normalized[CONF_OFF_TEMPERATURE] == 20
    assert normalized[CONF_TOLERANCE] == _USER_FIELD_DEFAULTS[CONF_TOLERANCE]


@pytest.mark.parametrize("bound", [None, ""])
def test_a_missing_range_bound_is_stored_as_auto(bound):
    normalized = _normalize_user_submission(
        _submission(**{CONF_TARGET_TEMP_MIN: bound, CONF_TARGET_TEMP_MAX: bound}),
        mode="create",
        base=None,
    )

    assert normalized[CONF_TARGET_TEMP_MIN] == TARGET_TEMP_BOUND_AUTO
    assert normalized[CONF_TARGET_TEMP_MAX] == TARGET_TEMP_BOUND_AUTO


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({}, set()),
        ({CONF_THERMOSTAT: [{"advanced": {}}]}, {DEFAULT_CALIBRATION_MODE}),
        (
            {CONF_THERMOSTAT: [{"advanced": {CONF_CALIBRATION_MODE: None}}]},
            {DEFAULT_CALIBRATION_MODE},
        ),
        (
            {CONF_THERMOSTAT: [{"advanced": {CONF_CALIBRATION_MODE: 0}}]},
            {CalibrationMode.DEFAULT},
        ),
        (
            {
                CONF_THERMOSTAT: [
                    {"advanced": {CONF_CALIBRATION_MODE: " PID_Calibration "}}
                ]
            },
            {CalibrationMode.PID_CALIBRATION},
        ),
        ({CONF_THERMOSTAT: "climate.trv"}, set()),
        ({CONF_THERMOSTAT: ["climate.trv", {"advanced": None}]}, set()),
        ({CONF_THERMOSTAT: [{"advanced": {CONF_CALIBRATION_MODE: "retired"}}]}, set()),
        (
            {
                CONF_THERMOSTAT: [
                    {
                        "advanced": {
                            CONF_CALIBRATION_MODE: CalibrationMode.PID_CALIBRATION
                        }
                    }
                ]
            },
            {CalibrationMode.PID_CALIBRATION},
        ),
        (
            {
                CONF_THERMOSTAT: [
                    {"advanced": {CONF_CALIBRATION_MODE: "tpi_calibration"}}
                ]
            },
            {CalibrationMode.TPI_CALIBRATION},
        ),
    ],
)
def test_the_algorithms_in_use_are_read_from_any_stored_shape(config, expected):
    """A TRV counts with the mode it runs, the default when it names none."""
    assert OptionsFlowHandler._get_active_algorithms(config) == expected


@pytest.mark.parametrize(
    "stored",
    [{}, {CONF_CALIBRATION_MODE: 0}, {CONF_CALIBRATION_MODE: " TPI_Calibration "}],
)
def test_resaving_an_entry_in_the_same_mode_changes_no_algorithm(stored):
    """The form saves the mode the TRV already runs, so no sensor set changes.

    The dynamic sensors follow ``configured_calibration_mode``; comparing the
    stored entry the same way keeps a resave that writes the mode out in its
    canonical spelling from announcing a change.
    """
    mode = configured_calibration_mode(stored)
    assert mode is not None
    old = {CONF_THERMOSTAT: [{"advanced": stored}]}
    new = {CONF_THERMOSTAT: [{"advanced": {CONF_CALIBRATION_MODE: mode.value}}]}

    assert OptionsFlowHandler._get_active_algorithms(
        old
    ) == OptionsFlowHandler._get_active_algorithms(new)


def test_a_delay_stored_as_text_is_offered_as_no_delay():
    assert _seconds_to_duration_dict("soon") == {"hours": 0, "minutes": 0, "seconds": 0}


def test_a_range_bound_that_is_not_a_number_raises_no_range_error():
    """Only two numeric bounds can contradict each other."""
    errors: dict[str, str] = {}

    _normalize_user_submission(
        _submission(**{CONF_TARGET_TEMP_MIN: "cold", CONF_TARGET_TEMP_MAX: "15"}),
        mode="create",
        base=None,
        errors=errors,
    )

    assert errors == {}


def test_a_choice_that_is_not_a_scalar_takes_its_default():
    advanced = _normalize_advanced_submission(
        {"calibration": ["local_calibration_based"], "calibration_mode": None},
        default_calibration="target_temp_based",
        homematic=False,
        has_auto=False,
    )

    assert advanced["calibration"] == "target_temp_based"
    assert advanced["calibration_mode"] is None
    assert "mpc_v2_plant_preset" not in advanced
    draft = _TrvDraft(
        entity_id="climate.trv", integration=None, adapter=None, stored={}
    )
    draft.advanced = advanced
    assert "mpc_v2_plant_preset" not in draft.to_stored()["advanced"]


def test_a_preset_list_keeps_only_preset_names():
    normalized = _normalize_user_submission(
        _submission(presets=["eco", 3]), mode="update", base=None
    )
    assert normalized["presets"] == ["eco"]

    normalized = _normalize_user_submission(
        _submission(presets="eco"), mode="update", base=None
    )
    assert normalized["presets"] is None


def test_a_thermostat_not_yet_through_its_advanced_step_is_stored_as_it_was():
    stored = {"trv": "climate.trv", "integration": "mqtt", "advanced": {"x": 1}}
    draft = _TrvDraft(
        entity_id="climate.trv", integration="mqtt", adapter=None, stored=stored
    )

    assert draft.to_stored() == stored | {"adapter": None}
    assert stored == {"trv": "climate.trv", "integration": "mqtt", "advanced": {"x": 1}}


def test_the_stored_advanced_options_of_a_draft_must_be_a_mapping():
    draft = _TrvDraft(
        entity_id="climate.trv",
        integration=None,
        adapter=None,
        stored={"advanced": "broken"},
    )

    assert draft.stored_advanced() is None


@pytest.mark.parametrize("value", [None, "climate.trv", {"trv": "climate.trv"}])
def test_an_entry_without_a_thermostat_list_has_no_stored_thermostats(value):
    assert _stored_thermostats(value) == {}


def test_only_stored_thermostats_with_an_entity_id_are_found():
    found = {"trv": "climate.a", "advanced": {}}

    assert _stored_thermostats([found, {"trv": ""}, {"trv": 5}, "climate.b"]) == {
        "climate.a": found
    }
