"""Tests for the child-lock switch state handling."""

from unittest.mock import MagicMock

import pytest

from custom_components.better_thermostat.switch import BetterThermostatChildLockSwitch
from custom_components.better_thermostat.trv import Trv

TRV_ID = "climate.trv_kitchen"


def _make_switch(trv: Trv) -> BetterThermostatChildLockSwitch:
    bt_climate = MagicMock()
    bt_climate.unique_id = "bt_1"
    bt_climate.real_trvs = {TRV_ID: trv}
    switch = BetterThermostatChildLockSwitch(bt_climate, TRV_ID, show_trv_name=False)
    switch.async_write_ha_state = MagicMock()
    return switch


def test_update_state_writes_child_lock_flag():
    """Toggling stores the flag in the Trv's advanced mapping."""
    trv = Trv(entity_id=TRV_ID)
    switch = _make_switch(trv)

    switch._update_state(True)

    assert trv.advanced["child_lock"] is True
    switch.async_write_ha_state.assert_called_once()


def test_update_state_handles_missing_advanced_dict():
    """An unset advanced mapping is created instead of crashing."""
    trv = Trv(entity_id=TRV_ID)
    trv.advanced = None
    switch = _make_switch(trv)

    switch._update_state(False)

    assert trv.advanced == {"child_lock": False}


@pytest.mark.parametrize("state", [True, False])
def test_is_on_reflects_advanced_flag(state):
    """The switch state mirrors the stored child_lock flag."""
    trv = Trv(entity_id=TRV_ID, advanced={"child_lock": state})
    switch = _make_switch(trv)

    assert switch.is_on is state
