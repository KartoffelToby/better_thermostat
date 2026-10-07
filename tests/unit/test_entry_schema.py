"""The parser that checks a config entry's stored settings.

It accepts every shape a writer stores, returns a new mapping and leaves the
stored one alone, and refuses a value no writer stores with a message that
names the key.
"""

import copy

from homeassistant.const import CONF_NAME
import pytest

from custom_components.better_thermostat.utils import const
from custom_components.better_thermostat.utils.entry_schema import (
    BtSettings,
    InvalidSettingsError,
    TrvAdvanced,
    TrvSettings,
    parse_settings,
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
            _with_advanced(calibration_mode=2),
            "thermostat[0].advanced.calibration_mode is int, not a string",
        ),
        (
            _with_advanced(child_lock="true"),
            "thermostat[0].advanced.child_lock is str, not a boolean",
        ),
        (_minimal() | {"model": 1}, "model is int, not a string"),
        (
            _minimal() | {"window_sensors": ["binary_sensor.window"]},
            "window_sensors is list, not a string",
        ),
        (
            _minimal() | {"window_off_delay": {"seconds": 30}},
            "window_off_delay is dict, not a number",
        ),
        (_minimal() | {"door_off_delay": True}, "door_off_delay is bool, not a number"),
        (
            _minimal() | {"off_temperature": [20]},
            "off_temperature is list, not a number or a string",
        ),
        (
            _minimal() | {"tolerance": True},
            "tolerance is bool, not a number or a string",
        ),
        (
            _minimal() | {"target_temp_min": {}},
            "target_temp_min is dict, not a number or a string",
        ),
        (_minimal() | {"presets": "eco"}, "presets is str, not a list"),
        (_minimal() | {"presets": ["eco", None]}, "presets[1] is null, not a string"),
    ],
)
def test_a_value_no_writer_stores_is_refused_by_name(raw, reason):
    with pytest.raises(InvalidSettingsError) as caught:
        parse_settings(raw)

    assert str(caught.value) == reason
