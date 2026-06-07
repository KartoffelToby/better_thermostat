"""Tests for the pure startup-restore helpers in utils/restore.py."""

from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import State
import pytest

from custom_components.better_thermostat.utils.const import (
    MAX_HEAT_LOSS,
    MAX_HEATING_POWER,
    MIN_HEAT_LOSS,
    MIN_HEATING_POWER,
)
from custom_components.better_thermostat.utils.restore import (
    clamp_heat_loss,
    clamp_heating_power,
    mean_trv_target,
    restore_target_temperature,
)

DEV = "Test BT"


def _trv(temp, unit=None, entity_id="climate.trv"):
    """Build a TRV State carrying a target temperature (and optional unit)."""
    attrs: dict = {}
    if temp is not None:
        attrs[ATTR_TEMPERATURE] = temp
    if unit is not None:
        attrs["temperature_unit"] = unit
    return State(entity_id, "heat", attributes=attrs)


# ---------------------------------------------------------------------------
# mean_trv_target
# ---------------------------------------------------------------------------


class TestMeanTrvTarget:
    """mean_trv_target averages valid TRV targets, converting to Celsius."""

    def test_empty_list_returns_none(self):
        """No states → None."""
        assert mean_trv_target([], DEV) is None

    def test_single_trv(self):
        """One TRV target is returned unchanged."""
        assert mean_trv_target([_trv(21.0)], DEV) == 21.0

    def test_multiple_trvs_averaged(self):
        """Multiple targets are averaged."""
        assert mean_trv_target([_trv(20.0), _trv(24.0)], DEV) == 22.0

    def test_missing_target_attr_skipped(self):
        """A TRV without a target temperature is ignored."""
        assert mean_trv_target([_trv(20.0), _trv(None)], DEV) == 20.0

    def test_all_missing_returns_none(self):
        """No usable targets → None."""
        assert mean_trv_target([_trv(None), _trv(None)], DEV) is None

    def test_non_numeric_skipped(self):
        """A non-numeric target is skipped, not crashing."""
        assert mean_trv_target([_trv("bad"), _trv(21.0)], DEV) == 21.0

    def test_fahrenheit_converted_to_celsius(self):
        """A Fahrenheit target is converted before averaging."""
        result = mean_trv_target([_trv(68.0, unit=UnitOfTemperature.FAHRENHEIT)], DEV)
        assert result == pytest.approx(20.0)

    def test_unit_of_measurement_fallback_key(self):
        """The unit may also be supplied via unit_of_measurement."""
        s = State(
            "climate.trv",
            "heat",
            attributes={
                ATTR_TEMPERATURE: 68.0,
                "unit_of_measurement": UnitOfTemperature.FAHRENHEIT,
            },
        )
        assert mean_trv_target([s], DEV) == pytest.approx(20.0)

    def test_no_unit_attr_uses_system_unit(self):
        """Without a unit attribute the passed system unit decides the reading."""
        result = mean_trv_target(
            [_trv(68.0)], DEV, system_unit=UnitOfTemperature.FAHRENHEIT
        )
        assert result == pytest.approx(20.0)

    def test_no_unit_attr_celsius_system_unchanged(self):
        """A Celsius system leaves a unit-less reading as-is."""
        result = mean_trv_target(
            [_trv(20.0)], DEV, system_unit=UnitOfTemperature.CELSIUS
        )
        assert result == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# restore_target_temperature
# ---------------------------------------------------------------------------


class TestRestoreTargetTemperature:
    """restore_target_temperature clamps a saved value or falls back to TRVs."""

    def test_saved_in_range_passthrough(self):
        """An in-range saved value is returned unchanged."""
        assert restore_target_temperature(22.0, [], 5.0, 30.0, DEV) == 22.0

    def test_saved_below_min_clamped(self):
        """A saved value below min is clamped up."""
        assert restore_target_temperature(2.0, [], 5.0, 30.0, DEV) == 5.0

    def test_saved_above_max_clamped(self):
        """A saved value above max is clamped down."""
        assert restore_target_temperature(35.0, [], 5.0, 30.0, DEV) == 30.0

    def test_saved_at_boundaries_unchanged(self):
        """Values exactly on the bounds are not altered."""
        assert restore_target_temperature(5.0, [], 5.0, 30.0, DEV) == 5.0
        assert restore_target_temperature(30.0, [], 5.0, 30.0, DEV) == 30.0

    def test_none_bounds_use_defaults(self):
        """None bounds fall back to 5.0 / 30.0."""
        assert restore_target_temperature(2.0, [], None, None, DEV) == 5.0
        assert restore_target_temperature(99.0, [], None, None, DEV) == 30.0

    def test_saved_string_parsed(self):
        """A numeric string saved value is parsed."""
        assert restore_target_temperature("21.5", [], 5.0, 30.0, DEV) == 21.5

    def test_no_saved_falls_back_to_trv_mean(self):
        """Without a saved value the TRV mean is used."""
        assert (
            restore_target_temperature(None, [_trv(20.0), _trv(22.0)], 5.0, 30.0, DEV)
            == 21.0
        )

    def test_no_saved_no_trv_returns_none(self):
        """No saved value and no TRV target → None."""
        assert restore_target_temperature(None, [], 5.0, 30.0, DEV) is None

    def test_malformed_saved_falls_back_to_trv_mean(self):
        """A non-numeric saved value falls back to the TRV mean instead of raising."""
        assert (
            restore_target_temperature(
                "unknown", [_trv(20.0), _trv(22.0)], 5.0, 30.0, DEV
            )
            == 21.0
        )

    def test_malformed_saved_no_trv_returns_none(self):
        """A non-numeric saved value with no TRV target → None, not a crash."""
        assert restore_target_temperature("n/a", [], 5.0, 30.0, DEV) is None

    def test_saved_fahrenheit_converted_to_celsius(self):
        """A saved value in Fahrenheit is converted to Celsius on restoration."""
        assert restore_target_temperature(
            68.0, [], 5.0, 30.0, DEV, UnitOfTemperature.FAHRENHEIT
        ) == pytest.approx(20.0)

    def test_saved_fahrenheit_string_converted_to_celsius(self):
        """A saved string value in Fahrenheit is converted to Celsius on restoration."""
        assert restore_target_temperature(
            "68.0", [], 5.0, 30.0, DEV, UnitOfTemperature.FAHRENHEIT
        ) == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# clamp_heating_power
# ---------------------------------------------------------------------------


class TestClampHeatingPower:
    """clamp_heating_power parses and bounds the restored heating power."""

    def test_in_range_passthrough(self):
        """A value inside the range is returned unchanged."""
        mid = (MIN_HEATING_POWER + MAX_HEATING_POWER) / 2
        assert clamp_heating_power(mid, DEV) == mid

    def test_above_max_clamped(self):
        """A value above the max is clamped down."""
        assert clamp_heating_power(999.0, DEV) == MAX_HEATING_POWER

    def test_below_min_clamped(self):
        """A value below the min is clamped up."""
        assert clamp_heating_power(-1.0, DEV) == MIN_HEATING_POWER

    def test_string_value_parsed(self):
        """A numeric string is parsed then clamped."""
        assert clamp_heating_power("999.0", DEV) == MAX_HEATING_POWER

    def test_non_numeric_falls_back_to_default(self):
        """A non-numeric value falls back to 0.01 before clamping (stays in range)."""
        result = clamp_heating_power("bad", DEV)
        assert MIN_HEATING_POWER <= result <= MAX_HEATING_POWER


# ---------------------------------------------------------------------------
# clamp_heat_loss
# ---------------------------------------------------------------------------


class TestClampHeatLoss:
    """clamp_heat_loss parses and bounds the restored heat-loss rate."""

    def test_in_range_passthrough(self):
        """A value inside the range is returned unchanged."""
        mid = (MIN_HEAT_LOSS + MAX_HEAT_LOSS) / 2
        assert clamp_heat_loss(mid) == mid

    def test_above_max_clamped(self):
        """A value above the max is clamped down."""
        assert clamp_heat_loss(1.0) == MAX_HEAT_LOSS

    def test_below_min_clamped(self):
        """A value below the min is clamped up."""
        assert clamp_heat_loss(-1.0) == MIN_HEAT_LOSS

    def test_string_value_parsed(self):
        """A numeric string is parsed then clamped."""
        assert clamp_heat_loss("1.0") == MAX_HEAT_LOSS

    def test_non_numeric_returns_none(self):
        """A non-numeric value returns None (caller keeps the existing value)."""
        assert clamp_heat_loss("bad") is None

    def test_none_returns_none(self):
        """None returns None."""
        assert clamp_heat_loss(None) is None
