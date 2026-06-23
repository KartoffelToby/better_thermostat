"""Tests for config_flow._normalize_user_submission.

Focus on the optional entity selectors, which must be removable: when the
user clears an optional entity in the options flow, the HA frontend omits
the key from the submitted ``user_input``. The normalized result must then
drop the previously stored value instead of silently keeping it.
"""

from homeassistant.const import CONF_NAME

from custom_components.better_thermostat.config_flow import _normalize_user_submission
from custom_components.better_thermostat.utils.const import (
    CONF_COOLER,
    CONF_HEATER,
    CONF_SENSOR,
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
