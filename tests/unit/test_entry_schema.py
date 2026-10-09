"""The parser that checks a config entry's stored settings.

It accepts every value a current reader tolerates, returns a new mapping and
leaves the stored one alone, and refuses a broken structure, or a container
where no reader can use one, with a message that names the key.
"""

import copy

from homeassistant.const import CONF_NAME
import pytest

from custom_components.better_thermostat.utils import const
from custom_components.better_thermostat.utils.advanced_flags import (
    advanced_flag,
    as_bool,
)
from custom_components.better_thermostat.utils.entry_schema import (
    BtSettings,
    InvalidSettingsError,
    TrvAdvanced,
    TrvSettings,
    parse_settings,
    target_temperature_bound,
    target_temperature_step,
)


def _minimal() -> dict[str, object]:
    """Return the fewest keys an entry can hold and still parse."""
    return {
        "name": "Living room",
        "thermostat": [{"trv": "climate.radiator", "integration": "mqtt"}],
    }


def _full() -> dict[str, object]:
    """Return an entry carrying every key the schema describes."""
    return {
        "name": "Living room",
        "thermostat": [
            {
                "trv": "climate.radiator",
                "integration": "mqtt",
                "model": "TRVZB",
                "advanced": {
                    "calibration": "target_temp_based",
                    "calibration_mode": "heating_power_calibration",
                    "mpc_v2_plant_preset": "auto",
                    "protect_overheating": True,
                    "no_off_system_mode": False,
                    "heat_auto_swapped": False,
                    "valve_maintenance": True,
                    "child_lock": False,
                    "homematicip": False,
                    "balance_mode": "none",
                },
            }
        ],
        "model": "TRVZB",
        "temperature_sensor": "sensor.room",
        "humidity_sensor": None,
        "cooler": None,
        "window_sensors": "binary_sensor.window",
        "door_sensors": None,
        "outdoor_sensor": "sensor.outdoor",
        "weather": None,
        "window_off_delay": 30,
        "window_off_delay_after": 0,
        "door_off_delay": 15.5,
        "door_off_delay_after": 0,
        "off_temperature": 20,
        "tolerance": 0.3,
        "target_temp_min": "5.0",
        "target_temp_max": "auto",
        "target_temp_step": "0.5",
        "presets": ["eco", "comfort"],
    }


def test_the_fewest_keys_parse_to_the_same_mapping():
    assert parse_settings(_minimal()) == _minimal()


def test_every_described_key_survives_parsing():
    parsed = parse_settings(_full())

    assert parsed == _full()
    assert set(parsed) == BtSettings.__required_keys__ | BtSettings.__optional_keys__


_NUMBER_KEYS = (
    "window_off_delay",
    "window_off_delay_after",
    "door_off_delay",
    "door_off_delay_after",
    "off_temperature",
    "tolerance",
    "target_temp_min",
    "target_temp_max",
    "target_temp_step",
)


def test_a_whole_number_where_a_float_is_read_becomes_that_float():
    raw = _minimal() | dict.fromkeys(_NUMBER_KEYS, 5)

    parsed = parse_settings(raw)

    for key in _NUMBER_KEYS:
        assert type(parsed[key]) is float, key
        assert parsed[key] == 5.0, key


@pytest.mark.parametrize(
    "stored",
    [
        # Older versions stored the bounds and the step as numbers.
        {"target_temp_min": 7.5, "target_temp_max": 28.0, "target_temp_step": 0.5},
        # Readers parse the outdoor threshold and the tolerance from text too.
        {"off_temperature": "20", "tolerance": "0.3"},
        # None is what a reader's own default for these keys is.
        dict.fromkeys(_NUMBER_KEYS + ("model", "presets", "temperature_sensor")),
    ],
    ids=["numbers", "text", "none"],
)
def test_every_shape_the_readers_take_parses_as_stored(stored):
    assert parse_settings(_minimal() | stored) == _minimal() | stored


@pytest.mark.parametrize(
    "advanced",
    [
        # Older entries stored numeric modes; 0 is the default mode.
        {"calibration_mode": 0},
        {"calibration": None},
        {"calibration_mode": 1.5, "balance_mode": None, "mpc_v2_plant_preset": 3},
        # A bool is an int to the readers that look at numeric modes.
        {"calibration_mode": False},
    ],
    ids=["mode_zero", "calibration_none", "numbers_and_none", "bool"],
)
def test_a_choice_is_kept_as_stored(advanced):
    raw = _with_advanced(**advanced)

    parsed = parse_settings(raw)

    assert parsed["thermostat"][0]["advanced"] == advanced


@pytest.mark.parametrize(
    ("stored", "read"),
    [
        ("false", False),
        (" On ", True),
        (1, True),
        (0, False),
        ("maybe", True),
        ([], False),
    ],
)
def test_a_flag_stored_as_another_type_reads_as_the_options_flow_saves_it(stored, read):
    parsed = parse_settings(_with_advanced(child_lock=stored, valve_maintenance=stored))

    advanced = parsed["thermostat"][0]["advanced"]
    assert advanced == {"child_lock": read, "valve_maintenance": read}
    assert advanced["child_lock"] is as_bool(stored)


def test_a_flag_stored_as_none_is_left_out_so_each_reader_keeps_its_default():
    raw = _with_advanced(child_lock=None, protect_overheating=None)

    advanced = parse_settings(raw)["thermostat"][0]["advanced"]

    assert advanced == {}
    stored = raw["thermostat"][0]["advanced"]
    for default in (False, True):
        for key in ("child_lock", "protect_overheating"):
            assert advanced_flag(advanced, key, default) == advanced_flag(
                stored, key, default
            )


@pytest.mark.parametrize(
    "stored",
    [
        # The options flow reads delays stored as text, and the contact
        # handling converts them with float().
        {"window_off_delay": "30", "door_off_delay_after": "0"},
        # The readers convert with float(), which takes a bool.
        {"tolerance": True, "door_off_delay": False},
    ],
    ids=["delays_as_text", "bools"],
)
def test_other_scalars_the_readers_convert_are_kept_as_stored(stored):
    assert parse_settings(_minimal() | stored) == _minimal() | stored


def test_a_thermostat_without_a_model_parses():
    raw = _with_trv(trv="climate.radiator", integration="mqtt", model=None)

    assert parse_settings(raw) == raw


def test_parsing_leaves_the_stored_settings_unchanged_and_shares_nothing():
    raw = _full()
    stored = copy.deepcopy(raw)

    parsed = parse_settings(raw)
    parsed["thermostat"][0].setdefault("advanced", {})["child_lock"] = True
    parsed["thermostat"].append({"trv": "climate.other", "integration": "zha"})
    parsed.setdefault("presets", []).append("boost")

    assert raw == stored


def test_keys_the_schema_does_not_describe_are_left_out():
    raw = _minimal() | {"min_cooler_resend_interval": 30}
    raw["thermostat"] = [
        {
            "trv": "climate.radiator",
            "integration": "mqtt",
            "adapter": None,
            "advanced": {"aggressive_calibration": False, "child_lock": True},
        }
    ]

    parsed = parse_settings(raw)

    assert parsed == {
        "name": "Living room",
        "thermostat": [
            {
                "trv": "climate.radiator",
                "integration": "mqtt",
                "advanced": {"child_lock": True},
            }
        ],
    }


def test_the_described_keys_are_the_ones_the_constants_hold():
    """Readers look the keys up through the constants; both spell the same."""
    assert BtSettings.__required_keys__ == {CONF_NAME, const.CONF_THERMOSTAT}
    assert BtSettings.__optional_keys__ == {
        const.CONF_MODEL,
        const.CONF_TEMPERATURE_SENSOR,
        const.CONF_HUMIDITY_SENSOR,
        const.CONF_COOLER,
        const.CONF_WINDOW_SENSORS,
        const.CONF_DOOR_SENSORS,
        const.CONF_OUTDOOR_SENSOR,
        const.CONF_WEATHER,
        const.CONF_WINDOW_OFF_DELAY,
        const.CONF_WINDOW_OFF_DELAY_AFTER,
        const.CONF_DOOR_OFF_DELAY,
        const.CONF_DOOR_OFF_DELAY_AFTER,
        const.CONF_OFF_TEMPERATURE,
        const.CONF_TOLERANCE,
        const.CONF_TARGET_TEMP_MIN,
        const.CONF_TARGET_TEMP_MAX,
        const.CONF_TARGET_TEMP_STEP,
        const.CONF_PRESETS,
    }
    assert TrvSettings.__required_keys__ == {"trv", const.CONF_INTEGRATION}
    assert TrvSettings.__optional_keys__ == {const.CONF_MODEL, "advanced"}
    assert TrvAdvanced.__required_keys__ == frozenset()
    assert TrvAdvanced.__optional_keys__ == {
        const.CONF_CALIBRATION,
        const.CONF_CALIBRATION_MODE,
        const.CONF_MPC_V2_PLANT_PRESET,
        const.CONF_PROTECT_OVERHEATING,
        const.CONF_NO_OFF_SYSTEM_MODE,
        const.CONF_HEAT_AUTO_SWAPPED,
        const.CONF_VALVE_MAINTENANCE,
        const.CONF_CHILD_LOCK,
        const.CONF_HOMEMATICIP,
        "balance_mode",
    }


def test_the_error_is_a_value_error():
    assert issubclass(InvalidSettingsError, ValueError)


def _without(key: str) -> dict[str, object]:
    raw = _minimal()
    del raw[key]
    return raw


def _with_trv(**trv_config: object) -> dict[str, object]:
    return _minimal() | {"thermostat": [trv_config]}


def _with_advanced(**advanced: object) -> dict[str, object]:
    return _with_trv(trv="climate.radiator", integration="mqtt", advanced=advanced)


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (_without("name"), "name is missing"),
        (_minimal() | {"name": 3}, "name is int, not a string"),
        (_without("thermostat"), "thermostat is missing"),
        (
            _minimal() | {"thermostat": "climate.radiator"},
            "thermostat is str, not a list",
        ),
        (_minimal() | {"thermostat": []}, "thermostat is empty"),
        (
            _minimal() | {"thermostat": ["climate.radiator"]},
            "thermostat[0] is str, not a mapping",
        ),
        (_with_trv(integration="mqtt"), "thermostat[0].trv is missing"),
        (
            _with_trv(trv=None, integration="mqtt"),
            "thermostat[0].trv is null, not a string",
        ),
        (_with_trv(trv="climate.radiator"), "thermostat[0].integration is missing"),
        (
            _with_trv(trv="climate.radiator", integration=1),
            "thermostat[0].integration is int, not a string",
        ),
        (
            _with_trv(trv="climate.radiator", integration="mqtt", model=1),
            "thermostat[0].model is int, not a string",
        ),
        (
            _with_trv(trv="climate.radiator", integration="mqtt", advanced=[]),
            "thermostat[0].advanced is list, not a mapping",
        ),
        (
            _with_advanced(calibration_mode=["mpc_calibration"]),
            "thermostat[0].advanced.calibration_mode is list, "
            "not a string, a number or null",
        ),
        (_minimal() | {"model": 1}, "model is int, not a string"),
        (
            _minimal() | {"window_sensors": ["binary_sensor.window"]},
            "window_sensors is list, not a string",
        ),
        (
            _minimal() | {"window_off_delay": {"seconds": 30}},
            "window_off_delay is dict, not a number or a string",
        ),
        (
            _minimal() | {"off_temperature": [20]},
            "off_temperature is list, not a number or a string",
        ),
        (
            _minimal() | {"target_temp_min": {}},
            "target_temp_min is dict, not a number or a string",
        ),
        (_minimal() | {"presets": "eco"}, "presets is str, not a list"),
        (_minimal() | {"presets": ["eco", None]}, "presets[1] is null, not a string"),
    ],
)
def test_a_broken_structure_is_refused_by_name(raw, reason):
    with pytest.raises(InvalidSettingsError) as caught:
        parse_settings(raw)

    assert str(caught.value) == reason


@pytest.mark.parametrize("stored", [None, "", "0.0", "0", "0.00", 0, 0.0, "-0.5", -0.5])
def test_an_automatic_step_reads_as_none(stored):
    """Every spelling of no step, and of a step of zero or less, is automatic.

    The flows store ``""`` and, in older entries, ``"0.0"`` for automatic. A
    hand-edited ``"0"`` or ``"0.00"`` is the same step and must not reach the
    entity as a step of zero.
    """
    assert target_temperature_step(stored) is None


@pytest.mark.parametrize(("stored", "step"), [("0.5", 0.5), (0.25, 0.25), ("1", 1.0)])
def test_a_configured_step_reads_as_a_number(stored, step):
    assert target_temperature_step(stored) == step


@pytest.mark.parametrize("stored", ["fine", "nan", float("inf")])
def test_a_step_that_is_no_finite_number_is_refused(stored):
    with pytest.raises(ValueError):
        target_temperature_step(stored)


@pytest.mark.parametrize("stored", [None, "", const.TARGET_TEMP_BOUND_AUTO, -1.0, "-1"])
def test_an_automatic_bound_reads_as_none(stored):
    """No bound and the value the flows store for automatic both read as None."""
    assert target_temperature_bound(stored) is None


@pytest.mark.parametrize(
    ("stored", "bound"), [("16.0", 16.0), (0.0, 0.0), ("-2", -2.0)]
)
def test_a_configured_bound_reads_as_a_number(stored, bound):
    """A bound is a number of degrees, 0 °C and below included."""
    assert target_temperature_bound(stored) == bound


@pytest.mark.parametrize("stored", ["warm", "nan", float("-inf")])
def test_a_bound_that_is_no_finite_number_is_refused(stored):
    with pytest.raises(ValueError):
        target_temperature_bound(stored)
