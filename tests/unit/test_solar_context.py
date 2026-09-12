"""The day/night solar gating exists exactly once.

Both the snapshot builder and the MPC balance consume the same rule:
below the horizon there is no solar gain, whatever the weather entity
claims.
"""

from unittest.mock import MagicMock, patch

from custom_components.better_thermostat.calibration import _get_solar_context


def _bt(sun_state):
    bt = MagicMock()
    state = MagicMock()
    state.state = sun_state
    bt.hass.states.get.return_value = state
    return bt


def test_daytime_reports_the_measured_intensity():
    """Above the horizon the measured intensity passes through."""
    bt = _bt("above_horizon")
    with patch(
        "custom_components.better_thermostat.calibration._get_current_solar_intensity",
        return_value=0.7,
    ):
        assert _get_solar_context(bt) == (True, 0.7)


def test_night_gates_the_intensity_to_zero():
    """Below the horizon the intensity is gated to zero."""
    bt = _bt("below_horizon")
    with patch(
        "custom_components.better_thermostat.calibration._get_current_solar_intensity",
        return_value=0.7,
    ):
        assert _get_solar_context(bt) == (False, 0.0)
