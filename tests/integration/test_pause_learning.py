"""The Pause Learning switch against a running Home Assistant.

An automation turns the switch on and off through the switch services; the
thermostat publishes the pause, its learners hold from that moment on, and
the pause outlives a reload and a restart.
"""

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import mock_restore_cache

from custom_components.better_thermostat.utils.const import (
    ATTR_STATE_DEVICES_RELEASED,
    ATTR_STATE_LEARNING_PAUSED,
    ATTR_STATE_RELEASED_TRVS,
)
from custom_components.better_thermostat.utils.scheduler import request_control_cycle
from custom_components.better_thermostat.utils.thermal_learning import Reading

from .conftest import (
    BT_ENTITY,
    build_devices,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import GENERIC_HEAT_TRV, TRV_ID


def _switch(hass, entry, suffix: str) -> str:
    registry = er.async_get(hass)
    (entity_id,) = [
        reg.entity_id
        for reg in er.async_entries_for_config_entry(registry, entry.entry_id)
        if reg.domain == "switch" and reg.unique_id.endswith(suffix)
    ]
    return entity_id


def _pause_switch(hass, entry) -> str:
    return _switch(hass, entry, "_pause_learning")


def _published_pause(hass) -> object:
    state = hass.states.get(BT_ENTITY)
    assert state is not None
    return state.attributes.get(ATTR_STATE_LEARNING_PAUSED)


async def _started(hass):
    set_room_sensor(hass, 19.0)
    await build_devices(hass, GENERIC_HEAT_TRV)
    entry = make_entry(GENERIC_HEAT_TRV)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    return entry, bt


async def test_the_switch_pauses_and_resumes_learning(hass):
    """Turning the switch on pauses every learner at once and shows it on the thermostat.

    Turning it off shows at once too; the learners resume once the control
    cycle the switch requests has run.
    """
    entry, bt = await _started(hass)
    switch = _pause_switch(hass, entry)
    assert hass.states.get(switch).state == STATE_OFF
    assert _published_pause(hass) is False

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": switch}, blocking=True
    )
    await hass.async_block_till_done()

    assert hass.states.get(switch).state == STATE_ON
    assert _published_pause(hass) is True
    held: bool = bt.learning_held
    assert held is True

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": switch}, blocking=True
    )
    await hass.async_block_till_done()

    assert hass.states.get(switch).state == STATE_OFF
    assert _published_pause(hass) is False
    assert await wait_for(hass, lambda: bt.learning_held is False)


async def test_a_heating_cycle_under_way_is_dropped_when_learning_pauses(hass):
    """The cycle the heating-power tracker is timing teaches nothing once paused.

    The next control cycle runs the tracker with the pause and drops the
    cycle's start, as an opening window does.
    """
    entry, bt = await _started(hass)
    bt._heating_tracker.start = Reading(19.0, bt.clock.utcnow())

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": _pause_switch(hass, entry)}, blocking=True
    )
    request_control_cycle(bt)

    assert await wait_for(hass, lambda: bt._heating_tracker.start is None)


async def test_the_cycle_that_ends_a_pause_still_holds(hass):
    """The control cycle requested on resuming learns nothing either.

    A heating cycle the tracker started during the pause would otherwise be
    timed from paused time; that cycle drops it, and the hold ends after it.
    """
    entry, bt = await _started(hass)
    switch = _pause_switch(hass, entry)
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": switch}, blocking=True
    )
    await hass.async_block_till_done()
    bt._heating_tracker.start = Reading(19.0, bt.clock.utcnow())

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": switch}, blocking=True
    )

    assert await wait_for(hass, lambda: bt.learning_held is False)
    assert bt._heating_tracker.start is None


async def test_an_idle_stretch_under_way_is_dropped_when_learning_pauses(hass):
    """The idle stretch the heat-loss tracker is timing teaches nothing once paused."""
    entry, bt = await _started(hass)
    set_room_sensor(hass, 24.0)
    await hass.async_block_till_done()
    reading = Reading(24.0, bt.clock.utcnow())
    bt._loss_tracker.start = reading
    bt._loss_tracker.end = reading

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": _pause_switch(hass, entry)}, blocking=True
    )
    request_control_cycle(bt)

    assert await wait_for(hass, lambda: bt._loss_tracker.start is None)


async def test_the_pause_outlives_a_reload(hass):
    """A reload of the entry keeps learning paused."""
    entry, _bt = await _started(hass)
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": _pause_switch(hass, entry)}, blocking=True
    )
    await hass.async_block_till_done()

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    reloaded = await wait_for_startup(hass, entry)

    assert reloaded.learning_paused is True
    assert reloaded.learning_held is True
    assert hass.states.get(_pause_switch(hass, entry)).state == STATE_ON


async def test_the_pause_outlives_a_restart(hass):
    """A thermostat saved with learning paused starts with its learners held."""
    mock_restore_cache(
        hass,
        (
            State(
                BT_ENTITY,
                "heat",
                {"temperature": 21.0, ATTR_STATE_LEARNING_PAUSED: True},
            ),
        ),
    )
    entry, bt = await _started(hass)

    assert bt.learning_paused is True
    assert bt.learning_held is True
    assert hass.states.get(_pause_switch(hass, entry)).state == STATE_ON
    assert _published_pause(hass) is True


async def test_a_thermostat_saved_before_the_switch_existed_learns(hass):
    """A saved state without the attribute restores to learning, not to paused."""
    mock_restore_cache(hass, (State(BT_ENTITY, "heat", {"temperature": 21.0}),))
    entry, bt = await _started(hass)

    assert bt.learning_paused is False
    assert bt.learning_held is False
    assert hass.states.get(_pause_switch(hass, entry)).state == STATE_OFF


async def test_a_thermostat_saved_with_its_devices_released_starts_held(hass):
    """Devices released before a restart hold the learners from the start.

    The pause switch stays off: a release is not a pause. Taking the
    devices back ends the hold once the cycle it requests has run.
    """
    mock_restore_cache(
        hass,
        (
            State(
                BT_ENTITY,
                "heat",
                {"temperature": 21.0, ATTR_STATE_DEVICES_RELEASED: True},
            ),
        ),
    )
    entry, bt = await _started(hass)

    assert bt.devices_released is True
    assert (bt.learning_paused, bt.learning_held) == (False, True)
    assert hass.states.get(_pause_switch(hass, entry)).state == STATE_OFF
    assert _published_pause(hass) is False

    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": _switch(hass, entry, "_release_devices")},
        blocking=True,
    )

    assert await wait_for(hass, lambda: bt.learning_held is False)
    released: bool = bt.devices_released
    assert released is False


async def test_a_thermostat_saved_with_a_trv_released_starts_held(hass):
    """A TRV released before a restart holds the learners from the start.

    Taking it back ends the hold once the cycle it requests has run.
    """
    mock_restore_cache(
        hass,
        (
            State(
                BT_ENTITY,
                "heat",
                {"temperature": 21.0, ATTR_STATE_RELEASED_TRVS: [TRV_ID]},
            ),
        ),
    )
    entry, bt = await _started(hass)

    assert bt.released_trvs == {TRV_ID}
    assert (bt.learning_paused, bt.learning_held) == (False, True)
    assert _published_pause(hass) is False

    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": _switch(hass, entry, f"_{TRV_ID}_release")},
        blocking=True,
    )

    assert await wait_for(hass, lambda: bt.learning_held is False)
    assert bt.released_trvs == set()
