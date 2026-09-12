"""Tests for the rule that decides whether a TRV report is a user press.

``accepts_user_setpoint`` in ``events/trv.py`` answers one question: does the
setpoint a TRV just published come from someone turning its knob, so that BT
adopts it as its own target? While BT drives a device it raises
``ignore_trv_states`` so the device's answer is not mistaken for a press, and a
child-locked device never speaks for the user at all. These tests drive that
function.
"""

from homeassistant.components.climate.const import HVACMode
import pytest

from custom_components.better_thermostat.events.trv import accepts_user_setpoint
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.const import (
    CalibrationMode,
    CalibrationType,
)


@pytest.fixture
def trv():
    """Build a TRV whose every flag permits adopting a reported setpoint."""
    return Trv.from_legacy_dict(
        "climate.test_trv",
        {
            "hvac_mode": HVACMode.HEAT,
            "hvac_modes": [HVACMode.OFF, HVACMode.HEAT],
            "min_temp": 5.0,
            "max_temp": 30.0,
            "current_temperature": 18.0,
            "last_temperature": 19.0,
            "target_temp_received": True,
            "system_mode_received": True,
            "ignore_trv_states": False,
            "advanced": {
                "calibration": CalibrationType.LOCAL_BASED,
                "calibration_mode": CalibrationMode.DEFAULT,
                "no_off_system_mode": False,
                "heat_auto_swapped": False,
                "child_lock": False,
            },
        },
    )


def _adopts(trv):
    """Ask the production rule about a live report from ``trv``."""
    return accepts_user_setpoint(
        trv,
        is_echo=False,
        child_lock=trv.advanced.get("child_lock"),
        contact_open=False,
    )


class TestIgnoreTrvStates:
    """The ignore_trv_states flag and the child lock as setpoint guards."""

    def test_setpoint_blocked_when_ignore_trv_states_true(self, trv):
        """A report is no user press while BT is driving the device."""
        trv.ignore_trv_states = True

        assert _adopts(trv) is False

    def test_setpoint_adopted_when_ignore_trv_states_false(self, trv):
        """A report is a user press once BT has stopped driving the device."""
        trv.ignore_trv_states = False

        assert _adopts(trv) is True

    def test_child_lock_blocks_the_setpoint_regardless(self, trv):
        """A child-locked device reports no user press, whatever the flags say."""
        trv.ignore_trv_states = False
        trv.advanced["child_lock"] = True

        assert _adopts(trv) is False

    def test_ignore_trv_states_default_is_false(self):
        """The ignore_trv_states flag defaults to False when not set."""
        trv = Trv.from_legacy_dict("climate.default_test", {})

        assert trv.ignore_trv_states is False
