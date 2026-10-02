"""Branch coverage for BetterThermostat._resolve_temperature_range.

Derives the working min/max/step from the child TRV states: the most
restrictive bounds across TRVs, Fahrenheit conversion (with step treated as a
delta), the non-overlapping-range warning, and the step-already-set guard.
"""

import logging

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
from custom_components.better_thermostat.utils.helpers import (
    bound_to_celsius,
    convert_to_float_celsius,
)
from tests.factories import ThermostatStandIn, make_trv

HELPERS_LOGGER = "custom_components.better_thermostat.utils.helpers"


@pytest.fixture
def bt():
    """Minimal BetterThermostat mock for range resolution."""
    mock = ThermostatStandIn()
    mock.device_name = "Test BT"
    mock.bt_min_temp = None
    mock.bt_max_temp = None
    mock.bt_target_temp_min = None
    mock.bt_target_temp_max = None
    mock.bt_target_temp_step = None
    mock.cool_min_temperature = None
    mock.cool_max_temperature = None
    mock.cooler_entity_id = None
    mock.real_trvs = {}
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
    """The range a user configured is the answer, with no device to ask.

    The bounds are resolved while the entity is being built, so a room whose
    heads have not reported yet still has to come up with the range its
    owner set rather than with the defaults.
    """
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


def _celsius(fahrenheit: float) -> float:
    return (fahrenheit - 32.0) * 5.0 / 9.0


# A bound published in whole degrees Fahrenheit may be Home Assistant's
# rounding of the device's bound, so it is read half a published degree
# inward: 41.5 °F and 85.5 °F.
_WHOLE_FAHRENHEIT_MIN_41 = _celsius(41.5)
_WHOLE_FAHRENHEIT_MAX_86 = _celsius(85.5)


def test_fahrenheit_bounds_and_step_converted(bt):
    """Fahrenheit bounds convert to Celsius; the step converts as a delta.

    Whole-degree bounds are read half a degree inside the published range,
    past the half degree a device bound Home Assistant rounded may lie
    outside it.
    """
    states = [_trv(min_t=41.0, max_t=86.0, step=1.0, unit=UnitOfTemperature.FAHRENHEIT)]
    BetterThermostat._resolve_temperature_range(bt, states)
    assert bt.bt_min_temp == pytest.approx(_WHOLE_FAHRENHEIT_MIN_41)
    assert bt.bt_max_temp == pytest.approx(_WHOLE_FAHRENHEIT_MAX_86)
    # 1 °F delta -> 1 * 5/9 °C
    assert bt.bt_target_temp_step == pytest.approx(round(1.0 * 5.0 / 9.0, 4))


def test_fahrenheit_bounds_without_unit_attr_use_system_unit(bt):
    """A climate child reports no unit attribute; the system unit decides.

    HA climate entities never expose ``temperature_unit`` /
    ``unit_of_measurement`` in their state attributes and always report in the
    configured system unit. With a Fahrenheit system the raw 41/86 bounds must
    therefore be read as °F and converted to Celsius, half a degree inside
    the whole degrees published — otherwise BT would treat 41 °F as 41 °C and
    clamp every setpoint far too high.
    """
    bt.hass.config.units.temperature_unit = UnitOfTemperature.FAHRENHEIT
    states = [_trv(min_t=41.0, max_t=86.0, step=1.0)]
    BetterThermostat._resolve_temperature_range(bt, states)
    assert bt.bt_min_temp == pytest.approx(_WHOLE_FAHRENHEIT_MIN_41)
    assert bt.bt_max_temp == pytest.approx(_WHOLE_FAHRENHEIT_MAX_86)
    assert bt.bt_target_temp_step == pytest.approx(round(1.0 * 5.0 / 9.0, 4))


@pytest.mark.parametrize(
    ("published_min", "published_max", "read_min", "read_max"),
    [
        # Tenths: 0.05 °F inward, 39.15 and 86.85, then inward onto the
        # tenths the thermostat publishes and writes in.
        pytest.param(39.1, 86.9, 39.2, 86.8, id="tenths"),
        # Halves: a Fritz!DECT maximum of 28 °C is 82.4 °F, published 82.5;
        # 0.25 °F inward is 82.25, and inward onto the tenths 82.2.
        pytest.param(39.5, 82.5, 39.8, 82.2, id="halves"),
    ],
)
def test_fahrenheit_bounds_off_the_whole_degree_stay_inside(
    bt, published_min, published_max, read_min, read_max
):
    """A bound published in tenths or halves is read inside the device's bound.

    Home Assistant rounds a bound to the precision the integration states,
    so the device's own bound lies up to half of that step either side of
    the published value, and it checks a setpoint against the device's
    bound. The bound is read past that half step, onto the tenth of a degree
    the thermostat publishes its own range and writes setpoints in.
    """
    bt.hass.config.units.temperature_unit = UnitOfTemperature.FAHRENHEIT
    states = [_trv(min_t=published_min, max_t=published_max, step=1.0)]
    BetterThermostat._resolve_temperature_range(bt, states)
    assert bt.bt_min_temp == pytest.approx(_celsius(read_min), abs=1e-9)
    assert bt.bt_max_temp == pytest.approx(_celsius(read_max), abs=1e-9)


@pytest.mark.parametrize("lower", [True, False])
def test_celsius_bounds_are_read_as_published(lower):
    """On a Celsius system a bound is read exactly as a temperature is.

    Only a Fahrenheit publication is rounded away from the device's bound,
    so every Celsius bound, and every value that is no temperature at all,
    reads the same as any other published temperature.
    """
    values = [i / 100 for i in range(-2000, 10000)] + [
        None,
        "None",
        "unknown",
        "",
        "abc",
        "5",
        "30.0",
    ]
    for value in values:
        for unit in (UnitOfTemperature.CELSIUS, None):
            assert bound_to_celsius(
                str(value), unit, lower=lower, instance_name="test"
            ) == convert_to_float_celsius(
                str(value), "test", "", unit_of_measurement=unit
            ), (value, unit)


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


def test_non_overlapping_ranges_still_assigned(bt, caplog):
    """Non-overlapping head ranges (min > max) are assigned and warned about."""
    states = [
        _trv(min_t=25.0, max_t=30.0, eid="climate.a"),
        _trv(min_t=16.0, max_t=22.0, eid="climate.b"),
    ]
    with caplog.at_level(logging.WARNING):
        BetterThermostat._resolve_temperature_range(bt, states)
    assert bt.bt_min_temp == 25.0  # max of mins
    assert bt.bt_max_temp == 22.0  # min of maxes
    assert bt.bt_min_temp > bt.bt_max_temp
    assert "heating min temp" in caplog.text


def _with_cooler(bt, cooler_id="climate.cooler"):
    """Configure ``bt`` with a cooler and one head, ``climate.trv``."""
    bt.cooler_entity_id = cooler_id
    bt.real_trvs = {"climate.trv": make_trv("climate.trv")}


def test_a_cooler_bounds_the_cooling_channel_alone(bt):
    """The heater bounds the heating channel, the cooler the cooling channel.

    Intersecting the two would cap the cooling target at the heater's maximum
    and lift the heating target onto the cooler's minimum.
    """
    _with_cooler(bt)
    states = [
        _trv(min_t=5.0, max_t=30.0, eid="climate.trv"),
        _trv(min_t=16.0, max_t=35.0, eid="climate.cooler"),
    ]

    BetterThermostat._resolve_temperature_range(bt, states)

    assert (bt.bt_min_temp, bt.bt_max_temp) == (5.0, 30.0)
    assert (bt.cool_min_temperature, bt.cool_max_temperature) == (16.0, 35.0)


def test_without_a_cooler_the_cooling_channel_stays_unresolved(bt):
    """A room without a cooler resolves the heads' range and nothing else."""
    states = [
        _trv(min_t=5.0, max_t=28.0, eid="climate.a"),
        _trv(min_t=7.0, max_t=30.0, eid="climate.b"),
    ]

    BetterThermostat._resolve_temperature_range(bt, states)

    assert (bt.bt_min_temp, bt.bt_max_temp) == (7.0, 28.0)
    assert (bt.cool_min_temperature, bt.cool_max_temperature) == (None, None)


def test_an_unavailable_cooler_leaves_the_cooling_channel_unresolved(bt):
    """A cooler that reported no state contributes no bounds."""
    _with_cooler(bt)

    BetterThermostat._resolve_temperature_range(
        bt, [_trv(min_t=5.0, max_t=30.0, eid="climate.trv")]
    )

    assert (bt.bt_min_temp, bt.bt_max_temp) == (5.0, 30.0)
    assert (bt.cool_min_temperature, bt.cool_max_temperature) == (None, None)


def test_configured_bounds_replace_both_channels(bt):
    """A configured bound holds on the heating and the cooling channel alike."""
    _with_cooler(bt)
    bt.bt_target_temp_min = 10.0
    bt.bt_target_temp_max = 32.0
    states = [
        _trv(min_t=5.0, max_t=30.0, eid="climate.trv"),
        _trv(min_t=16.0, max_t=35.0, eid="climate.cooler"),
    ]

    BetterThermostat._resolve_temperature_range(bt, states)

    assert (bt.bt_min_temp, bt.bt_max_temp) == (10.0, 32.0)
    assert (bt.cool_min_temperature, bt.cool_max_temperature) == (10.0, 32.0)


def test_a_room_with_only_a_cooler_bounds_heating_by_it(bt):
    """With no head to ask, the cooler's range bounds the heating channel too."""
    bt.cooler_entity_id = "climate.cooler"

    BetterThermostat._resolve_temperature_range(
        bt, [_trv(min_t=16.0, max_t=35.0, eid="climate.cooler")]
    )

    assert (bt.bt_min_temp, bt.bt_max_temp) == (16.0, 35.0)
    assert (bt.cool_min_temperature, bt.cool_max_temperature) == (16.0, 35.0)


def test_a_dual_role_device_bounds_both_channels(bt):
    """A device wired as head and as cooler bounds each channel with its range."""
    _with_cooler(bt, cooler_id="climate.trv")
    state = _trv(min_t=16.0, max_t=31.0, eid="climate.trv")

    BetterThermostat._resolve_temperature_range(bt, [state, state])

    assert (bt.bt_min_temp, bt.bt_max_temp) == (16.0, 31.0)
    assert (bt.cool_min_temperature, bt.cool_max_temperature) == (16.0, 31.0)


def test_a_fahrenheit_cooler_bound_is_read_inward_in_celsius(bt):
    """The cooler's °F bound is converted like a head's, inward of its value."""
    _with_cooler(bt)
    bt.hass.config.units.temperature_unit = UnitOfTemperature.FAHRENHEIT
    states = [
        _trv(min_t=41.0, max_t=86.0, eid="climate.trv"),
        _trv(min_t=61.0, max_t=95.0, eid="climate.cooler"),
    ]

    BetterThermostat._resolve_temperature_range(bt, states)

    assert bt.cool_max_temperature == bound_to_celsius(
        95.0, UnitOfTemperature.FAHRENHEIT, lower=False, instance_name="Test BT"
    )
    assert bt.cool_max_temperature > bt.bt_max_temp


def test_heater_and_cooler_ranges_that_do_not_overlap_are_no_conflict(bt, caplog):
    """Separate channels need no overlap, so there is nothing to warn about."""
    _with_cooler(bt)
    states = [
        _trv(min_t=4.0, max_t=15.0, eid="climate.trv"),
        _trv(min_t=18.0, max_t=30.0, eid="climate.cooler"),
    ]

    with caplog.at_level(logging.WARNING):
        BetterThermostat._resolve_temperature_range(bt, states)

    assert (bt.bt_min_temp, bt.bt_max_temp) == (4.0, 15.0)
    assert (bt.cool_min_temperature, bt.cool_max_temperature) == (18.0, 30.0)
    assert "min temp" not in caplog.text


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


@pytest.mark.parametrize(
    ("system_unit", "published"),
    [
        pytest.param(UnitOfTemperature.CELSIUS, 0.5, id="celsius"),
        pytest.param(UnitOfTemperature.FAHRENHEIT, 0.9, id="fahrenheit"),
    ],
)
def test_the_published_step_is_in_the_system_unit(bt, system_unit, published):
    """The step the entity publishes is a difference in the system unit.

    Home Assistant publishes the step unconverted next to targets it has
    converted into the system unit, so a 0.5 °C step is published as 0.9 on
    a Fahrenheit system and as 0.5 on a Celsius one.
    """
    bt.bt_target_temp_step = 0.5
    bt._unit = system_unit
    step = BetterThermostat.target_temperature_step.fget(bt)
    assert step == pytest.approx(published)


@pytest.mark.parametrize(
    ("lower", "read"),
    [pytest.param(True, 39.3, id="min"), pytest.param(False, 39.2, id="max")],
)
def test_a_fahrenheit_bound_off_every_published_grid_is_read_onto_a_tenth(lower, read):
    """A bound finer than tenths was not rounded by Home Assistant.

    It is the device's own bound, so it needs no half step of room, only the
    tenth of a degree the thermostat publishes and writes in, inward of it.
    """
    bound = bound_to_celsius(
        "39.25", UnitOfTemperature.FAHRENHEIT, lower=lower, instance_name="test"
    )
    assert bound == pytest.approx(_celsius(read))


@pytest.mark.parametrize(
    "system_unit", [UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT]
)
def test_the_thermostat_publishes_in_tenths(bt, system_unit):
    """The entity publishes its temperatures in tenths on every system.

    On a Fahrenheit system Home Assistant would round them to whole degrees,
    which rounds the range outward past the bounds the thermostat holds and
    moves a target between two degrees onto one of them.
    """
    bt._unit = system_unit
    assert BetterThermostat.precision.fget(bt) == 0.1


@pytest.mark.parametrize(
    ("system_unit", "published"),
    [
        pytest.param(UnitOfTemperature.CELSIUS, 0.1, id="celsius"),
        pytest.param(UnitOfTemperature.FAHRENHEIT, 1.0, id="fahrenheit"),
    ],
)
def test_without_a_step_the_default_of_the_system_unit_is_published(
    bt, system_unit, published
):
    """Without a step the entity publishes Home Assistant's default for the unit."""
    bt.bt_target_temp_step = None
    bt._unit = system_unit
    assert BetterThermostat.target_temperature_step.fget(bt) == published
