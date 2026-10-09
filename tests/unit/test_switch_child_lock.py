"""Tests for the child-lock switch state handling."""

from unittest.mock import AsyncMock, MagicMock

from homeassistant.const import STATE_OFF
from homeassistant.core import State
import pytest

from custom_components.better_thermostat.entity import (
    LAST_AVAILABLE_STATE,
    last_available_state,
)
from custom_components.better_thermostat.switch import (
    BetterThermostatChildLockSwitch,
    _switch_state_wins,
)
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.entry_schema import (
    TrvAdvanced,
    TrvSettings,
    parse_settings,
)
from tests.factories import ThermostatStandIn

TRV_ID = "climate.trv_kitchen"


def _parsed_trv(child_lock: object) -> TrvSettings:
    """Return the thermostat entry the entry parser reads from a stored lock."""
    settings = parse_settings(
        {
            "name": "Kitchen",
            "thermostat": [
                {
                    "trv": TRV_ID,
                    "integration": "generic",
                    "advanced": {"child_lock": child_lock},
                }
            ],
        }
    )
    return settings["thermostat"][0]


def _parsed_advanced(child_lock: object) -> TrvAdvanced:
    """Return the advanced options the entry parser reads from a stored lock."""
    advanced = _parsed_trv(child_lock).get("advanced")
    assert advanced is not None
    return advanced


def _state_writes(switch: BetterThermostatChildLockSwitch) -> MagicMock:
    """Return the recorder standing in for the switch's state write."""
    writes = switch.async_write_ha_state
    assert isinstance(writes, MagicMock)
    return writes


def _make_switch(trv: Trv) -> BetterThermostatChildLockSwitch:
    bt_climate = ThermostatStandIn()
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
    _state_writes(switch).assert_called_once()


@pytest.mark.parametrize("state", [True, False])
def test_is_on_reflects_advanced_flag(state):
    """The switch state mirrors the stored child_lock flag."""
    trv = Trv(entity_id=TRV_ID, advanced={"child_lock": state})
    switch = _make_switch(trv)

    assert switch.is_on is state


@pytest.mark.parametrize(
    ("stored", "expected"),
    [("false", False), ("true", True), ("True", True), (0, False), (None, False)],
)
def test_is_on_reads_a_stored_spelling_as_the_options_flow_saves_it(stored, expected):
    """An older entry's ``"false"`` shows the switch off, ``None`` as off."""
    trv = Trv(entity_id=TRV_ID, advanced={"child_lock": stored})
    switch = _make_switch(trv)

    assert switch.is_on is expected


@pytest.mark.parametrize(("stored", "expected"), [("false", False), ("true", True)])
def test_the_configured_option_reads_a_stored_spelling(stored, expected):
    """The option recorded next to the switch state reads ``"false"`` as off."""
    switch = _make_switch(Trv(entity_id=TRV_ID))
    switch._bt_climate.all_trvs = [_parsed_trv(stored)]

    assert switch._configured_child_lock() is expected


@pytest.mark.asyncio
async def test_a_restored_off_matching_a_stored_false_sends_nothing():
    """A TRV holding ``"false"`` already holds the restored off."""
    trv = Trv(entity_id=TRV_ID, advanced=_parsed_advanced("false"))
    switch = _make_switch(trv)
    switch._bt_climate.all_trvs = []
    switch.async_get_last_available_state = AsyncMock(
        return_value=State("switch.child_lock", STATE_OFF)
    )
    switch.async_get_last_extra_data = AsyncMock(return_value=None)
    switch._set_child_lock = AsyncMock()

    await switch._restore_child_lock()

    assert trv.advanced["child_lock"] is False
    switch._set_child_lock.assert_not_awaited()


def test_a_switch_of_a_head_not_built_shows_off_and_publishes_nothing():
    """A head the thermostat could not build has no lock to show or set.

    The switch stays registered so it comes back once the head is built,
    and toggling it in the meantime publishes nothing.
    """
    switch = _make_switch(Trv(entity_id=TRV_ID, advanced={"child_lock": True}))
    switch._bt_climate.real_trvs = {}

    assert switch.is_on is False
    switch._update_state(True)

    _state_writes(switch).assert_not_called()


_UNAVAILABLE = State("switch.child_lock", "unavailable")


@pytest.mark.parametrize(
    "recorded", [pytest.param(["on"], id="list"), pytest.param("on", id="string")]
)
def test_a_recorded_state_that_is_not_an_object_restores_the_saved_one(recorded):
    """Extra data whose recorded state is no JSON object is ignored."""
    restored = last_available_state(_UNAVAILABLE, {LAST_AVAILABLE_STATE: recorded})

    assert restored is _UNAVAILABLE


def test_a_recorded_state_object_is_restored():
    """Extra data holding the last available state as an object restores it."""
    shown = State("switch.child_lock", "on")

    restored = last_available_state(
        _UNAVAILABLE, {LAST_AVAILABLE_STATE: shown.as_dict()}
    )

    assert restored is not None
    assert restored.state == "on"


@pytest.mark.parametrize(
    ("recorded", "expected"),
    [
        pytest.param([True], None, id="list"),
        pytest.param({"value": True}, None, id="dict"),
        pytest.param(True, True, id="same_option"),
        pytest.param(False, None, id="changed_option"),
        pytest.param(None, True, id="unrecorded"),
    ],
)
def test_the_recorded_option_is_compared_as_it_was_saved(recorded, expected):
    """A recorded option that is not the configured bool lets the option hold."""
    assert _switch_state_wins("on", {"configured": recorded}, True) is expected
