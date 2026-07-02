"""Tests for helpers.group_all_members_off – the group-wide "all off" check.

Gates group-wide OFF adoptions: a multi-TRV instance is only considered off
when every available member is off (or, for ``no_off_system_mode`` devices, at
its minimum temperature). Single-TRV instances always agree.
"""

import types
from unittest.mock import MagicMock

from homeassistant.const import UnitOfTemperature
from homeassistant.core import State

from custom_components.better_thermostat.utils.helpers import group_all_members_off


def _member(no_off=False, min_temp=5.0):
    """A minimal stand-in for a Trv exposing only what the helper reads."""
    return types.SimpleNamespace(
        advanced={"no_off_system_mode": no_off}, min_temp=min_temp
    )


def _state(entity_id, state_str, temperature=19.0):
    return State(entity_id, state_str, attributes={"temperature": temperature})


def _fake_self(members, states, system_unit=UnitOfTemperature.CELSIUS):
    self_ = types.SimpleNamespace()
    self_.device_name = "Test"
    self_.real_trvs = members
    hass = MagicMock()
    hass.states.get.side_effect = states.get
    hass.config.units.temperature_unit = system_unit
    self_.hass = hass
    return self_


def test_single_member_always_true():
    """Single-TRV instances always "agree", regardless of that valve's mode."""
    self_ = _fake_self(
        {"climate.a": _member()}, {"climate.a": _state("climate.a", "heat")}
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
