"""Tests for the pure startup-restore helpers in utils/restore.py."""

from homeassistant.components.climate.const import (
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
)
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
    restore_cooling_target,
    restore_target_temperature,
    saved_cooling_target,
    saved_heating_target,
)

DEV = "Test BT"


def _trv(temperature, unit=None, entity_id="climate.trv"):
    """Build a TRV State carrying a target temperature (and optional unit)."""
    attrs: dict[str, object] = {}
    if temperature is not None:
        attrs[ATTR_TEMPERATURE] = temperature
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

    def test_range_trv_contributes_its_low_setpoint(self):
        """A range TRV's heating setpoint counts as its target."""
        ranged = State(
            "climate.trv",
            "heat_cool",
            attributes={ATTR_TARGET_TEMP_LOW: 22.0, ATTR_TARGET_TEMP_HIGH: 26.0},
        )
        assert mean_trv_target([ranged, _trv(20.0)], DEV) == pytest.approx(21.0)

    def test_range_trv_low_setpoint_converted_to_celsius(self):
        """A range TRV's Fahrenheit heating setpoint is read in Celsius."""
        ranged = State(
            "climate.trv",
            "heat_cool",
            attributes={
                ATTR_TARGET_TEMP_LOW: 68.0,
                ATTR_TARGET_TEMP_HIGH: 77.0,
                "temperature_unit": UnitOfTemperature.FAHRENHEIT,
            },
        )
        assert mean_trv_target([ranged], DEV) == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# saved_heating_target / saved_cooling_target
# ---------------------------------------------------------------------------


class TestSavedHeatingTarget:
    """saved_heating_target reads the key the thermostat published it under."""

    def test_single_target_thermostat_publishes_temperature(self):
        """A thermostat offering one target saves it as `temperature`."""
        assert saved_heating_target({ATTR_TEMPERATURE: 21.5}) == 21.5

    def test_range_publishing_thermostat_publishes_the_lower_bound(self):
        """A thermostat offering a range saves the heating target as the lower bound.

        Home Assistant writes `temperature` only for an entity that supports a
        single target, so the attributes of a range-publishing one hold the
        heating target under `target_temp_low` and no `temperature` at all.
        """
        assert (
            saved_heating_target(
                {ATTR_TARGET_TEMP_LOW: 20.0, ATTR_TARGET_TEMP_HIGH: 23.0}
            )
            == 20.0
        )

    def test_the_single_target_wins_over_a_lower_bound(self):
        """With both keys present the single target is the one that was published."""
        assert (
            saved_heating_target({ATTR_TEMPERATURE: 21.5, ATTR_TARGET_TEMP_LOW: 18.0})
            == 21.5
        )

    def test_a_zero_target_is_a_value(self):
        """A saved 0.0 is read as a target rather than as a missing one."""
        assert (
            saved_heating_target({ATTR_TEMPERATURE: 0.0, ATTR_TARGET_TEMP_LOW: 18.0})
            == 0.0
        )

    def test_an_empty_temperature_falls_through_to_the_lower_bound(self):
        """A `temperature` present but unset leaves the lower bound to answer."""
        assert (
            saved_heating_target({ATTR_TEMPERATURE: None, ATTR_TARGET_TEMP_LOW: 20.0})
            == 20.0
        )

    def test_neither_key_yields_none(self):
        """Attributes carrying no target at all yield None."""
        assert saved_heating_target({}) is None


class TestSavedCoolingTarget:
    """saved_cooling_target reads the upper bound of a published range."""

    def test_range_publishing_thermostat_publishes_the_upper_bound(self):
        """A thermostat offering a range saves the cooling target as the upper bound."""
        assert (
            saved_cooling_target(
                {ATTR_TARGET_TEMP_LOW: 20.0, ATTR_TARGET_TEMP_HIGH: 23.0}
            )
            == 23.0
        )

    def test_a_single_target_thermostat_has_no_cooling_target(self):
        """A thermostat offering one target publishes no upper bound."""
        assert saved_cooling_target({ATTR_TEMPERATURE: 21.5}) is None

    def test_a_zero_target_is_a_value(self):
        """A saved 0.0 is read as a target rather than as a missing one."""
        assert saved_cooling_target({ATTR_TARGET_TEMP_HIGH: 0.0}) == 0.0


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


# ---------------------------------------------------------------------------
# Restored values that are not scalars
# ---------------------------------------------------------------------------

# A restored attribute is whatever JSON value the saved state held, so the
# consumers take it as an untyped object and narrow it themselves.
_NON_SCALARS = [
    pytest.param([21.0], id="list"),
    pytest.param({"value": 21.0}, id="dict"),
]


class TestRestoredValuesThatAreNotScalars:
    """A list or an object degrades like any other non-numeric saved value."""

    @pytest.mark.parametrize("saved", _NON_SCALARS)
    def test_target_falls_back_to_the_trv_mean(self, saved, caplog):
        """A non-scalar saved target warns and takes the TRV mean."""
        result = restore_target_temperature(
            saved, [_trv(20.0), _trv(22.0)], 5.0, 30.0, DEV
        )

        assert result == 21.0
        assert "is not numeric" in caplog.text

    @pytest.mark.parametrize("saved", _NON_SCALARS)
    def test_target_without_a_trv_is_none(self, saved):
        """A non-scalar saved target with no TRV target yields None."""
        assert restore_target_temperature(saved, [], 5.0, 30.0, DEV) is None

    @pytest.mark.parametrize(("saved", "expected"), [(True, 5.0), (False, 5.0)])
    def test_target_bool_is_read_as_its_number(self, saved, expected):
        """A bool saved target reads as 1 or 0 and is clamped to the minimum."""
        assert restore_target_temperature(saved, [], 5.0, 30.0, DEV) == expected

    @pytest.mark.parametrize("saved", _NON_SCALARS)
    def test_trv_mean_skips_a_non_scalar_target(self, saved):
        """A TRV publishing a non-scalar target contributes nothing."""
        assert mean_trv_target([_trv(saved), _trv(22.0)], DEV) == 22.0

    @pytest.mark.parametrize("raw", _NON_SCALARS)
    def test_heating_power_falls_back_to_the_default(self, raw):
        """A non-scalar heating power falls back to 0.01 before clamping."""
        assert clamp_heating_power(raw, DEV) == 0.01

    def test_heating_power_bool_is_read_as_its_number(self):
        """A bool heating power reads as 1.0 and is clamped to the maximum."""
        assert clamp_heating_power(True, DEV) == MAX_HEATING_POWER
        assert clamp_heating_power(False, DEV) == MIN_HEATING_POWER

    @pytest.mark.parametrize("raw", _NON_SCALARS)
    def test_heat_loss_is_none(self, raw):
        """A non-scalar heat loss yields None."""
        assert clamp_heat_loss(raw) is None

    def test_heat_loss_bool_is_read_as_its_number(self):
        """A bool heat loss reads as 1.0 or 0.0 and is clamped."""
        assert clamp_heat_loss(True) == MAX_HEAT_LOSS
        assert clamp_heat_loss(False) == MIN_HEAT_LOSS

    @pytest.mark.parametrize("raw", _NON_SCALARS)
    def test_saved_targets_are_passed_through(self, raw):
        """The readers hand a non-scalar on unchanged for the consumer to judge."""
        assert saved_heating_target({ATTR_TEMPERATURE: raw}) is raw
        assert saved_heating_target({ATTR_TARGET_TEMP_LOW: raw}) is raw
        assert saved_cooling_target({ATTR_TARGET_TEMP_HIGH: raw}) is raw


class TestRestoreCoolingTarget:
    """restore_cooling_target reads a saved cooling target as Celsius."""

    @pytest.mark.parametrize(
        ("saved", "expected"),
        [(23.0, 23.0), ("23.5", 23.5), (None, None), ("n/a", None), ([23.0], None)],
        ids=["float", "string", "none", "malformed", "list"],
    )
    def test_saved_value(self, saved, expected):
        """A number or numeric string is read; anything else yields None."""
        assert restore_cooling_target(saved, DEV) == expected

    def test_fahrenheit_converted_to_celsius(self):
        """A saved value in Fahrenheit comes back as Celsius."""
        assert restore_cooling_target(
            77.0, DEV, UnitOfTemperature.FAHRENHEIT
        ) == pytest.approx(25.0)
