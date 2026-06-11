"""Branch coverage for BetterThermostat._resolve_temperature_range.

Derives the working min/max/step from the child TRV states: the most
restrictive bounds across TRVs, Fahrenheit conversion (with step treated as a
delta), the non-overlapping-range warning, and the step-already-set guard.
"""

from unittest.mock import MagicMock

from homeassistant.components.climate.const import (
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_TARGET_TEMP_STEP,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import State
import pytest

from custom_components.better_thermostat.climate import BetterThermostat


@pytest.fixture
def bt():
    """Minimal BetterThermostat mock for range resolution."""
    mock = MagicMock()
    mock.device_name = "Test BT"
    mock.bt_min_temp = None
    mock.bt_max_temp = None
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
