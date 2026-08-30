"""Tests for config_flow._normalize_user_submission.

Focus on the optional entity selectors, which must be removable: when the
user clears an optional entity in the options flow, the HA frontend omits
the key from the submitted ``user_input``. The normalized result must then
drop the previously stored value instead of silently keeping it.
"""

from homeassistant.const import CONF_NAME

from custom_components.better_thermostat.config_flow import (
    _build_user_fields,
    _normalize_user_submission,
)
from custom_components.better_thermostat.utils.const import (
    CONF_COOLER,
    CONF_DOOR_TIMEOUT,
    CONF_DOOR_TIMEOUT_AFTER,
    CONF_HEATER,
    CONF_SENSOR,
    CONF_SENSOR_DOOR,
)


def _base_with_cooler():
    return {
        CONF_NAME: "Living Room",
        CONF_HEATER: ["climate.trv"],
        CONF_COOLER: "climate.ac",
        CONF_SENSOR: "sensor.temp",
    }


def test_cooler_removed_when_key_absent_from_input():
    """Clearing the cooler omits the key; the stored value must be dropped."""
    user_input = {
        CONF_NAME: "Living Room",
        CONF_HEATER: ["climate.trv"],
        CONF_SENSOR: "sensor.temp",
    }

    normalized = _normalize_user_submission(
        user_input, mode="update", base=_base_with_cooler()
    )

    assert normalized[CONF_COOLER] is None


def test_cooler_removed_when_input_empty():
    """An explicit empty/None cooler value also clears the stored value."""
    for empty in ("", None):
        user_input = {
            CONF_NAME: "Living Room",
            CONF_HEATER: ["climate.trv"],
            CONF_COOLER: empty,
            CONF_SENSOR: "sensor.temp",
        }

        normalized = _normalize_user_submission(
            user_input, mode="update", base=_base_with_cooler()
        )

        assert normalized[CONF_COOLER] is None


def test_cooler_retained_when_present_in_input():
    """A submitted cooler entity is kept."""
    user_input = {
        CONF_NAME: "Living Room",
        CONF_HEATER: ["climate.trv"],
        CONF_COOLER: "climate.ac",
        CONF_SENSOR: "sensor.temp",
    }

    normalized = _normalize_user_submission(
        user_input, mode="update", base={CONF_COOLER: "climate.old_ac"}
    )

    assert normalized[CONF_COOLER] == "climate.ac"


def test_cooler_updated_to_different_entity():
    """A changed cooler entity replaces the stored one."""
    user_input = {
        CONF_NAME: "Living Room",
        CONF_HEATER: ["climate.trv"],
        CONF_COOLER: "climate.new_ac",
        CONF_SENSOR: "sensor.temp",
    }

    normalized = _normalize_user_submission(
        user_input, mode="update", base=_base_with_cooler()
    )

    assert normalized[CONF_COOLER] == "climate.new_ac"


def test_door_sensor_removed_when_key_absent_from_input():
    """Clearing the door sensor omits the key; the stored value must be dropped."""
    base = {
        CONF_NAME: "Living Room",
        CONF_HEATER: ["climate.trv"],
        CONF_SENSOR: "sensor.temp",
        CONF_SENSOR_DOOR: "binary_sensor.door",
    }
    user_input = {
        CONF_NAME: "Living Room",
        CONF_HEATER: ["climate.trv"],
        CONF_SENSOR: "sensor.temp",
    }
    normalized = _normalize_user_submission(user_input, mode="options", base=base)
    assert normalized[CONF_SENSOR_DOOR] is None


def test_door_timeouts_normalized_from_duration_dicts():
    """Door delays submitted as duration dicts are stored as seconds."""
    user_input = {
        CONF_NAME: "Living Room",
        CONF_HEATER: ["climate.trv"],
        CONF_SENSOR: "sensor.temp",
        CONF_DOOR_TIMEOUT: {"hours": 0, "minutes": 5, "seconds": 0},
        CONF_DOOR_TIMEOUT_AFTER: {"hours": 0, "minutes": 0, "seconds": 30},
    }
    normalized = _normalize_user_submission(user_input, mode="create")
    assert normalized[CONF_DOOR_TIMEOUT] == 300
    assert normalized[CONF_DOOR_TIMEOUT_AFTER] == 30


def test_door_timeouts_default_to_zero_on_create():
    """Omitted door delays default to no delay for new entries."""
    user_input = {
        CONF_NAME: "Living Room",
        CONF_HEATER: ["climate.trv"],
        CONF_SENSOR: "sensor.temp",
    }
    normalized = _normalize_user_submission(user_input, mode="create")
    assert normalized[CONF_DOOR_TIMEOUT] == 0
    assert normalized[CONF_DOOR_TIMEOUT_AFTER] == 0


def test_heaters_are_preserved_when_the_form_is_redisplayed():
    """A form rebuilt after a validation error keeps the thermostats picked.

    A stored entry holds a bundle per thermostat, but a redisplayed form is
    built from what the user submitted: plain entity ids. Reading only bundles
    empties the selector, and the user loses the selection along with the
    error message they were supposed to correct.
    """
    fields = _build_user_fields(
        mode="create", current={CONF_HEATER: ["climate.trv", "climate.trv_2"]}
    )

    heater_marker = next(marker for marker in fields if marker == CONF_HEATER)

    assert heater_marker.description["suggested_value"] == [
        "climate.trv",
        "climate.trv_2",
    ]
