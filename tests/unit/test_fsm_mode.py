"""Pure tests for the mode FSM (validated hvac mode x preset)."""

from custom_components.better_thermostat.core.fsm.mode import (
    ModeState,
    set_hvac_mode,
    set_preset,
)
from custom_components.better_thermostat.core.snapshot import HvacMode


def test_initial_state():
    """A fresh region is OFF without preset."""
    state = ModeState()
    assert state.hvac_mode == HvacMode.OFF
    assert state.preset is None


def test_set_known_modes():
    """Known mode strings (and HA's string enums) are accepted."""
    state = set_hvac_mode(ModeState(), "heat")
    assert state.hvac_mode == HvacMode.HEAT
    state = set_hvac_mode(state, "heat_cool")
    assert state.hvac_mode == HvacMode.HEAT_COOL
    state = set_hvac_mode(state, "off")
    assert state.hvac_mode == HvacMode.OFF


def test_invalid_mode_is_ignored():
    """Unknown or missing modes leave the region unchanged."""
    state = set_hvac_mode(ModeState(), "heat")
    assert set_hvac_mode(state, "bogus") == state
    assert set_hvac_mode(state, None) == state


def test_preset_axis_is_orthogonal():
    """Setting a preset keeps the mode; setting the mode keeps the preset."""
    state = set_hvac_mode(ModeState(), "heat")
    state = set_preset(state, "eco")
    assert state.hvac_mode == HvacMode.HEAT
    assert state.preset == "eco"
    state = set_hvac_mode(state, "off")
    assert state.preset == "eco"


def test_preset_none_clears():
    """PRESET_NONE and empty values clear the preset."""
    state = set_preset(ModeState(), "boost")
    assert set_preset(state, "none").preset is None
    assert set_preset(state, "").preset is None
    assert set_preset(state, None).preset is None
