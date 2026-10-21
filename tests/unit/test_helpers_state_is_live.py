"""Tests for helpers.state_is_live and its negation state_says_nothing.

A device state counts as live when it is present and neither ``unavailable``
nor ``unknown``. Every caller that reads a value from a device state relies on
this split, so both predicates have to agree on each kind of state.
"""

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import State
import pytest

from custom_components.better_thermostat.utils.helpers import (
    state_is_live,
    state_says_nothing,
)


def _state(value: str) -> State:
    """A climate state carrying *value* and a retained setpoint attribute."""
    return State("climate.trv", value, {"temperature": 21.0})


def test_missing_state_is_not_live() -> None:
    """A state the state machine does not hold says nothing about its device."""
    assert state_is_live(None) is False
    assert state_says_nothing(None) is True


@pytest.mark.parametrize("value", [STATE_UNAVAILABLE, STATE_UNKNOWN])
def test_sentinel_state_is_not_live(value: str) -> None:
    """An unavailable or unknown state is not live, its attributes notwithstanding."""
    state = _state(value)
    assert state_is_live(state) is False
    assert state_says_nothing(state) is True


@pytest.mark.parametrize("value", ["heat", "off", "21.5", ""])
def test_reported_state_is_live(value: str) -> None:
    """Any other state string reports the device as it is now."""
    state = _state(value)
    assert state_is_live(state) is True
    assert state_says_nothing(state) is False
