"""Tests for helpers.group_all_members_off – the group-wide "all off" check.

Gates group-wide OFF adoptions: a multi-TRV instance is only considered off
when every available member is off (or, for ``no_off_system_mode`` devices, at
its minimum temperature). Single-TRV instances always agree.
"""

from collections.abc import Mapping
from dataclasses import replace
from unittest.mock import MagicMock

from homeassistant.const import UnitOfTemperature
from homeassistant.core import State
import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.core.containers import BtConfig
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.helpers import (
    bound_to_celsius,
    group_all_members_off,
    setpoint_at_minimum,
)


def _member(
    no_off: bool = False,
    min_temp: float | None = 5.0,
    target_temp_step: float | None = None,
) -> Trv:
    """A Trv carrying what the helper reads; ``_fake_self`` sets its entity id."""
    return Trv(
        entity_id="climate.member",
        advanced={"no_off_system_mode": no_off},
        min_temp=min_temp,
        target_temp_step=target_temp_step,
    )


def _state(entity_id, state_str, temperature=19.0):
    return State(entity_id, state_str, attributes={"temperature": temperature})


def _fake_self(
    members: Mapping[str, Trv],
    states: Mapping[str, State],
    system_unit: UnitOfTemperature = UnitOfTemperature.CELSIUS,
) -> BetterThermostat:
    self_ = object.__new__(BetterThermostat)
    self_.config = BtConfig(device_name="Test")
    self_.real_trvs = {
        entity_id: replace(member, entity_id=entity_id)
        for entity_id, member in members.items()
    }
    hass = MagicMock()
    hass.states.get.side_effect = states.get
    hass.config.units.temperature_unit = system_unit
    self_.hass = hass
    return self_


@pytest.mark.parametrize("mode", ["heat", "off", "auto", "unavailable"])
def test_single_member_always_true(mode):
    """Single-TRV instances always "agree", regardless of that valve's mode."""
    self_ = _fake_self(
        {"climate.a": _member()}, {"climate.a": _state("climate.a", mode)}
    )
    assert group_all_members_off(self_) is True


def test_all_off_true():
    """Every member reporting off -> the group counts as off."""
    members = {f"climate.{n}": _member() for n in ("a", "b", "c")}
    states = {f"climate.{n}": _state(f"climate.{n}", "off") for n in ("a", "b", "c")}
    assert group_all_members_off(_fake_self(members, states)) is True


def test_mixed_false():
    """A single member still heating blocks the group-off verdict."""
    members = {"climate.a": _member(), "climate.b": _member()}
    states = {
        "climate.a": _state("climate.a", "off"),
        "climate.b": _state("climate.b", "heat"),
    }
    assert group_all_members_off(_fake_self(members, states)) is False


def test_no_off_all_at_min_true():
    """no_off members all at min_temp count as off."""
    members = {"climate.a": _member(no_off=True), "climate.b": _member(no_off=True)}
    states = {
        "climate.a": _state("climate.a", "heat", temperature=5.0),
        "climate.b": _state("climate.b", "heat", temperature=5.0),
    }
    assert group_all_members_off(_fake_self(members, states)) is True


def test_no_off_one_above_min_false():
    """One no_off member above min_temp blocks the group-off verdict."""
    members = {"climate.a": _member(no_off=True), "climate.b": _member(no_off=True)}
    states = {
        "climate.a": _state("climate.a", "heat", temperature=5.0),
        "climate.b": _state("climate.b", "heat", temperature=20.0),
    }
    assert group_all_members_off(_fake_self(members, states)) is False


def test_no_off_fahrenheit_at_min_true():
    """no_off members reporting 41 degF (5 degC) at min_temp 5 degC count as off."""
    members = {"climate.a": _member(no_off=True), "climate.b": _member(no_off=True)}
    states = {
        "climate.a": _state("climate.a", "heat", temperature=41.0),
        "climate.b": _state("climate.b", "heat", temperature=41.0),
    }
    self_ = _fake_self(members, states, system_unit=UnitOfTemperature.FAHRENHEIT)
    assert group_all_members_off(self_) is True


def test_no_off_fahrenheit_above_min_false():
    """A no_off member at 50 degF (10 degC) is above min_temp and blocks group-off."""
    members = {"climate.a": _member(no_off=True), "climate.b": _member(no_off=True)}
    states = {
        "climate.a": _state("climate.a", "heat", temperature=41.0),
        "climate.b": _state("climate.b", "heat", temperature=50.0),
    }
    self_ = _fake_self(members, states, system_unit=UnitOfTemperature.FAHRENHEIT)
    assert group_all_members_off(self_) is False


def test_no_off_target_temp_low_at_min_true():
    """A member exposing only target_temp_low at min_temp counts as off."""
    members = {"climate.a": _member(no_off=True), "climate.b": _member(no_off=True)}
    states = {
        "climate.a": _state("climate.a", "heat", temperature=5.0),
        "climate.b": State("climate.b", "heat", attributes={"target_temp_low": 5.0}),
    }
    assert group_all_members_off(_fake_self(members, states)) is True


def test_no_off_null_temperature_falls_back_to_target_temp_low():
    """A device on a target range publishes ``temperature`` as None.

    Home Assistant emits both attributes for a device that supports a single
    target and a range, and leaves ``temperature`` empty while the range is in
    use. Reading only that key makes a head sitting at its minimum look like it
    is heating, and the room comes back from a restart in HEAT.
    """
    members = {"climate.a": _member(no_off=True), "climate.b": _member(no_off=True)}
    states = {
        "climate.a": _state("climate.a", "heat", temperature=5.0),
        "climate.b": State(
            "climate.b",
            "heat",
            attributes={"temperature": None, "target_temp_low": 5.0, "min_temp": 5.0},
        ),
    }
    assert group_all_members_off(_fake_self(members, states)) is True


def test_unavailable_members_skipped():
    """An unavailable member is ignored; the rest still decide the outcome."""
    members = {f"climate.{n}": _member() for n in ("a", "b", "c")}
    states = {
        "climate.a": _state("climate.a", "off"),
        "climate.b": State("climate.b", "unavailable"),
        "climate.c": _state("climate.c", "off"),
    }
    assert group_all_members_off(_fake_self(members, states)) is True


def test_all_unavailable_false():
    """No live member to confirm off -> do not treat the group as off."""
    members = {"climate.a": _member(), "climate.b": _member()}
    states = {
        "climate.a": State("climate.a", "unavailable"),
        "climate.b": State("climate.b", "unknown"),
    }
    assert group_all_members_off(_fake_self(members, states)) is False


# The lowest setpoint the thermostat writes to a head whose minimum is
# published as 41 °F: half a published degree inside it, 41.5 °F.
_PARKED_MIN_CELSIUS = bound_to_celsius(
    "41", UnitOfTemperature.FAHRENHEIT, lower=True, instance_name="test"
)


# What a head publishes next to its setpoint: whole degrees when Home
# Assistant rounds it to whole degrees Fahrenheit, tenths when the
# integration states that precision.
_WHOLE_DEGREES = {"min_temp": 41, "max_temp": 86, "current_temperature": 68}
_TENTHS = {"min_temp": 41.0, "max_temp": 86.0, "current_temperature": 67.6}


def _fahrenheit_room(reported, published):
    """Two no-off heads at ``reported`` °F, published like ``published``."""
    members = {
        "climate.a": _member(no_off=True, min_temp=_PARKED_MIN_CELSIUS),
        "climate.b": _member(no_off=True, min_temp=_PARKED_MIN_CELSIUS),
    }
    states = {
        entity_id: State(
            entity_id, "heat", attributes={"temperature": reported, **published}
        )
        for entity_id in members
    }
    return _fake_self(members, states, system_unit=UnitOfTemperature.FAHRENHEIT)


@pytest.mark.parametrize(
    ("reported", "published"),
    [
        pytest.param(41.5, _TENTHS, id="parked_published_in_tenths"),
        pytest.param(42.0, _WHOLE_DEGREES, id="parked_published_in_whole_degrees"),
        pytest.param(41.0, _TENTHS, id="end_stop_published_in_tenths"),
        pytest.param(41.0, _WHOLE_DEGREES, id="end_stop_published_in_whole_degrees"),
    ],
)
def test_no_off_at_the_minimum_on_fahrenheit_counts_as_off(reported, published):
    """A Fahrenheit head at its minimum counts as off, however it got there.

    The thermostat parks the head at 41.5 °F. The head reports that back on
    the 0.01 grid of a reading, or, when Home Assistant publishes it in whole
    degrees, as 42 °F; and a user may turn it further, to the device's own
    41 °F, below it. All of these are the head at its minimum.
    """
    assert group_all_members_off(_fahrenheit_room(reported, published)) is True


@pytest.mark.parametrize(
    ("reported", "published"),
    [
        pytest.param(42.0, _TENTHS, id="42_published_in_tenths"),
        pytest.param(42.1, _WHOLE_DEGREES, id="42_1_next_to_whole_degrees"),
        pytest.param(42.5, _WHOLE_DEGREES, id="42_5_next_to_whole_degrees"),
        pytest.param(43.0, _WHOLE_DEGREES, id="43_published_in_whole_degrees"),
    ],
)
def test_no_off_above_the_parked_minimum_on_fahrenheit_heats(reported, published):
    """A setpoint above what a parked head reports is one the user chose.

    A head published in tenths reports its parked 41.5 °F as it is, so 42 °F
    is a setpoint of its own. A head published in whole degrees reports it
    as 42 °F, and anything past that half degree is above the minimum.
    """
    assert group_all_members_off(_fahrenheit_room(reported, published)) is False


def test_no_off_parked_on_a_coarser_device_grid_counts_as_off():
    """A head whose grid has no point at the minimum is parked on the next one.

    A 1 °F grid holds 41.5 °F as 42 °F, and reports it as that even when
    Home Assistant publishes it in tenths.
    """
    one_fahrenheit_degree = 5.0 / 9.0
    members = {
        "climate.a": _member(
            no_off=True,
            min_temp=_PARKED_MIN_CELSIUS,
            target_temp_step=one_fahrenheit_degree,
        )
    }
    members["climate.b"] = members["climate.a"]
    states = {
        entity_id: State(entity_id, "heat", attributes={"temperature": 42.0, **_TENTHS})
        for entity_id in members
    }
    self_ = _fake_self(members, states, system_unit=UnitOfTemperature.FAHRENHEIT)
    assert group_all_members_off(self_) is True


@pytest.mark.parametrize(
    ("setpoint", "min_temp"),
    [pytest.param(None, 5.0, id="no_setpoint"), pytest.param(5.0, None, id="no_min")],
)
def test_a_missing_setpoint_or_minimum_is_not_at_the_minimum(setpoint, min_temp):
    """Without both values there is nothing to say the head is at its minimum."""
    assert (
        setpoint_at_minimum(setpoint, min_temp, step=None, whole_degrees=False) is False
    )
