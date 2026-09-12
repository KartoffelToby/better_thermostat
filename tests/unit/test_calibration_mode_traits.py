"""The per-mode calibration behavior is data, not branches.

MODE_TRAITS carries everything mode-specific of the calibration
cascade; this table test pins each mode's traits so a change to one
mode's behavior is an explicit table edit.
"""

from custom_components.better_thermostat.calibration import _PASSIVE_TRAITS, MODE_TRAITS
from custom_components.better_thermostat.utils.const import CalibrationMode


def test_every_mode_has_traits():
    """All calibration modes are covered by the table."""
    assert set(MODE_TRAITS) == set(CalibrationMode)


def test_default_is_a_pure_offset_mode():
    """DEFAULT: no controller, no tolerance band, no post adjustments."""
    traits = MODE_TRAITS[CalibrationMode.DEFAULT]
    assert traits.balance is None
    assert traits.needs_target is False
    assert traits.uses_tolerance_band is False
    assert traits.skip_post_adjustments is True
    assert traits.adjust is None


def test_controller_modes_carry_their_balance_strategy():
    """MPC/TPI/PID: balance strategy present, no post adjustments."""
    for mode in (
        CalibrationMode.MPC_CALIBRATION,
        CalibrationMode.TPI_CALIBRATION,
        CalibrationMode.PID_CALIBRATION,
    ):
        traits = MODE_TRAITS[mode]
        assert traits.balance is not None
        assert traits.balance.mode == mode
        assert traits.uses_tolerance_band is True
        assert traits.skip_post_adjustments is True
        assert traits.adjust is None


def test_aggressive_boosts_but_skips_the_tolerance_delay():
    """AGGRESIVE: adjustment hook, post adjustments without the delay."""
    traits = MODE_TRAITS[CalibrationMode.AGGRESIVE_CALIBRATION]
    assert traits.balance is None
    assert traits.skip_post_adjustments is False
    assert traits.tolerance_delay is False
    assert traits.adjust is not None


def test_heating_power_adjusts_with_post_adjustments():
    """HEATING_POWER: adjustment hook decides the skip flag itself."""
    traits = MODE_TRAITS[CalibrationMode.HEATING_POWER_CALIBRATION]
    assert traits.balance is None
    assert traits.skip_post_adjustments is False
    assert traits.tolerance_delay is True
    assert traits.adjust is not None


def test_no_calibration_matches_the_passive_fallback():
    """NO_CALIBRATION behaves like any unknown mode: passive cascade."""
    assert MODE_TRAITS[CalibrationMode.NO_CALIBRATION] == _PASSIVE_TRAITS
    assert _PASSIVE_TRAITS.balance is None
    assert _PASSIVE_TRAITS.uses_tolerance_band is True
    assert _PASSIVE_TRAITS.skip_post_adjustments is False
    assert _PASSIVE_TRAITS.tolerance_delay is True
