"""Branch coverage for BetterThermostat._resolve_temperature_range.

Derives the working min/max/step from the child TRV states: the most
restrictive bounds across TRVs, Fahrenheit conversion (with step treated as a
delta), the non-overlapping-range warning, and the step-already-set guard.
"""

import logging
from unittest.mock import MagicMock

from homeassistant.components.climate.const import (
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_TARGET_TEMP_STEP,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import State
import pytest

from custom_components.better_thermostat.climate import (
    BetterThermostat,
    _configured_temperature_bound,
)
from custom_components.better_thermostat.utils.const import (
    CONF_TARGET_TEMP_MIN,
    TARGET_TEMP_BOUND_AUTO,
)

HELPERS_LOGGER = "custom_components.better_thermostat.utils.helpers"


@pytest.fixture
def bt():
    """Minimal BetterThermostat mock for range resolution."""
    mock = MagicMock()
    mock.device_name = "Test BT"
    mock.bt_min_temp = None
    mock.bt_max_temp = None
    mock.bt_target_temp_min = None
    mock.bt_target_temp_max = None
    mock.bt_target_temp_step = None
    return mock


def _trv(min_t=None, max_t=None, step=None, unit=None, eid="climate.trv"):
    attrs: dict = {}
    if min_t is not None:
        attrs[ATTR_MIN_TEMP] = min_t
    if max_t is not None:
        attrs[ATTR_MAX_TEMP] = max_t
    if step is not None:
        attrs[ATTR_TARGET_TEMP_STEP] = step
    if unit is not None:
        attrs["temperature_unit"] = unit
    return State(eid, "heat", attributes=attrs)


def test_intersection_of_bounds_across_trvs(bt):
    """Min is the highest child min, max the lowest child max (intersection)."""
    states = [
        _trv(min_t=5.0, max_t=28.0, eid="climate.a"),
        _trv(min_t=7.0, max_t=30.0, eid="climate.b"),
    ]
    BetterThermostat._resolve_temperature_range(bt, states)
    assert bt.bt_min_temp == 7.0
    assert bt.bt_max_temp == 28.0


def test_configured_minimum_overrides_the_child_intersection(bt):
    """A configured lower bound wins over what the devices report.

    A device that accepts 5 °C does not mean the user wants the room offered
    down to 5 °C, so the configured bound replaces the derived one instead of
    being intersected with it.
    """
    bt.bt_target_temp_min = 10.0
    states = [_trv(min_t=5.0, max_t=30.0)]

    BetterThermostat._resolve_temperature_range(bt, states)

    assert bt.bt_min_temp == 10.0
    assert bt.bt_max_temp == 30.0


def test_configured_maximum_overrides_the_child_intersection(bt):
    """A configured upper bound wins over what the devices report."""
    bt.bt_target_temp_max = 25.0
    states = [_trv(min_t=5.0, max_t=30.0)]

    BetterThermostat._resolve_temperature_range(bt, states)

    assert bt.bt_min_temp == 5.0
    assert bt.bt_max_temp == 25.0


def test_configured_bounds_widen_the_range_the_children_allow(bt):
    """The configured bound replaces the derived one in both directions.

    Intersecting instead of replacing would silently ignore a bound the user
    set outside what a device reports, which is the case a bound is usually
    configured for.
    """
    bt.bt_target_temp_min = 4.0
    bt.bt_target_temp_max = 32.0
    states = [_trv(min_t=7.0, max_t=28.0)]

    BetterThermostat._resolve_temperature_range(bt, states)

    assert bt.bt_min_temp == 4.0
    assert bt.bt_max_temp == 32.0


def test_configured_bounds_apply_without_any_child_state(bt):
    """A configured range holds even when no device reported bounds."""
    bt.bt_target_temp_min = 16.0
    bt.bt_target_temp_max = 24.0

    BetterThermostat._resolve_temperature_range(bt, [])

    assert bt.bt_min_temp == 16.0
    assert bt.bt_max_temp == 24.0


def test_inverted_configured_bounds_are_kept_and_warned_about(bt, caplog):
    """An inverted configured range is applied as given and reported."""
    bt.bt_target_temp_min = 25.0
    bt.bt_target_temp_max = 20.0
    states = [_trv(min_t=5.0, max_t=30.0)]

    with caplog.at_level(logging.WARNING):
        BetterThermostat._resolve_temperature_range(bt, states)

    assert bt.bt_min_temp == 25.0
    assert bt.bt_max_temp == 20.0
    assert "min temp" in caplog.text


def test_fahrenheit_bounds_and_step_converted(bt):
    """Fahrenheit bounds convert to Celsius; the step converts as a delta."""
    states = [_trv(min_t=41.0, max_t=86.0, step=1.0, unit=UnitOfTemperature.FAHRENHEIT)]
    BetterThermostat._resolve_temperature_range(bt, states)
    assert bt.bt_min_temp == pytest.approx(5.0)
    assert bt.bt_max_temp == pytest.approx(30.0)
    # 1 °F delta -> 1 * 5/9 °C
    assert bt.bt_target_temp_step == pytest.approx(round(1.0 * 5.0 / 9.0, 4))


def test_fahrenheit_bounds_without_unit_attr_use_system_unit(bt):
    """A climate child reports no unit attribute; the system unit decides.

    HA climate entities never expose ``temperature_unit`` /
    ``unit_of_measurement`` in their state attributes and always report in the
    configured system unit. With a Fahrenheit system the raw 41/86 bounds must
    therefore be read as °F and converted to 5/30 °C — otherwise BT would treat
    41 °F as 41 °C and clamp every setpoint far too high.
    """
    bt.hass.config.units.temperature_unit = UnitOfTemperature.FAHRENHEIT
    states = [_trv(min_t=41.0, max_t=86.0, step=1.0)]
    BetterThermostat._resolve_temperature_range(bt, states)
    assert bt.bt_min_temp == pytest.approx(5.0)
    assert bt.bt_max_temp == pytest.approx(30.0)
    assert bt.bt_target_temp_step == pytest.approx(round(1.0 * 5.0 / 9.0, 4))


def test_celsius_bounds_without_unit_attr_unchanged(bt):
    """With a Celsius system the raw bounds stay as-is (no spurious conversion)."""
    bt.hass.config.units.temperature_unit = UnitOfTemperature.CELSIUS
    states = [_trv(min_t=5.0, max_t=30.0, step=0.5)]
    BetterThermostat._resolve_temperature_range(bt, states)
    assert bt.bt_min_temp == pytest.approx(5.0)
    assert bt.bt_max_temp == pytest.approx(30.0)
    assert bt.bt_target_temp_step == pytest.approx(0.5)


def test_step_picks_coarsest(bt):
    """When several steps are present the coarsest is chosen."""
    states = [_trv(step=0.1, eid="climate.a"), _trv(step=0.5, eid="climate.b")]
    BetterThermostat._resolve_temperature_range(bt, states)
    assert bt.bt_target_temp_step == 0.5


def test_existing_step_not_overwritten(bt):
    """A pre-configured step is kept."""
    bt.bt_target_temp_step = 0.25
    states = [_trv(step=1.0)]
    BetterThermostat._resolve_temperature_range(bt, states)
    assert bt.bt_target_temp_step == 0.25


def test_children_without_a_step_leave_the_aggregate_unset(bt):
    """A child that publishes no step contributes nothing to the aggregate.

    ``None`` here means "no child told us anything", which the startup path
    reads differently from a step that was aggregated from the children, so a
    default step must not be invented at this point.
    """
    states = [_trv(min_t=5.0, max_t=30.0, eid="climate.a")]
    BetterThermostat._resolve_temperature_range(bt, states)
    assert bt.bt_target_temp_step is None


def test_unconvertible_child_step_is_logged_against_the_reader(bt, caplog):
    """An unreadable step yields no aggregate and names the reading site."""
    with caplog.at_level(logging.DEBUG, logger=HELPERS_LOGGER):
        BetterThermostat._resolve_temperature_range(bt, [_trv(step="abc")])
    assert bt.bt_target_temp_step is None
    assert "_target_temp_step_celsius" in caplog.text


def test_empty_states_yield_none(bt):
    """No states leave the bounds and step unset."""
    BetterThermostat._resolve_temperature_range(bt, [])
    assert bt.bt_min_temp is None
    assert bt.bt_max_temp is None
    assert bt.bt_target_temp_step is None


def test_non_overlapping_ranges_still_assigned(bt):
    """Non-overlapping child ranges (min > max) are assigned and warned about."""
    states = [
        _trv(min_t=25.0, max_t=30.0, eid="climate.a"),  # heater
        _trv(min_t=16.0, max_t=22.0, eid="climate.b"),  # cooler
    ]
    BetterThermostat._resolve_temperature_range(bt, states)
    assert bt.bt_min_temp == 25.0  # max of mins
    assert bt.bt_max_temp == 22.0  # min of maxes
    assert bt.bt_min_temp > bt.bt_max_temp


@pytest.mark.parametrize("stored", [None, "", TARGET_TEMP_BOUND_AUTO, -1.0])
def test_an_unconfigured_bound_reads_as_no_bound(stored):
    """Nothing configured and the auto value both leave the bound to the devices."""
    assert (
        _configured_temperature_bound(stored, "Test BT", CONF_TARGET_TEMP_MIN) is None
    )


@pytest.mark.parametrize("stored", ["16.0", 16.0, 16])
def test_a_configured_bound_reads_as_degrees_celsius(stored):
    """The bound is stored as a string but reaches the entity as a number."""
    assert (
        _configured_temperature_bound(stored, "Test BT", CONF_TARGET_TEMP_MIN) == 16.0
    )


@pytest.mark.parametrize("stored", ["warm", float("nan"), float("inf")])
def test_an_unreadable_bound_falls_back_to_the_devices(stored, caplog):
    """A value no temperature can be read from must not take the entity down.

    The bound is read while the climate entity is being constructed, so raising
    here would leave the user with an entry that no longer loads at all rather
    than one whose range is merely wrong.
    """
    with caplog.at_level(logging.WARNING):
        bound = _configured_temperature_bound(stored, "Test BT", CONF_TARGET_TEMP_MIN)

    assert bound is None
