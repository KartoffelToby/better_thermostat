"""Tests for the door sensor event handler.

Door sensors share the window sensor vocabulary and debounce behavior
but are bound to their own entity, open flag, delays and queue. These
tests exercise the door binding through the public door helpers.
"""

import asyncio
from unittest.mock import MagicMock, Mock, patch

import pytest

from custom_components.better_thermostat.events.door import (
    door_queue,
    trigger_door_change,
)

_CONTACT = "custom_components.better_thermostat.events.contact"


def _make_bt(*, sensor_state="off", door_open=False):
    bt = MagicMock()
    bt.device_name = "Test BT"
    bt.door_id = "binary_sensor.door"
    bt.door_open = door_open
    bt.async_write_ha_state = Mock()
    bt.door_queue_task = asyncio.Queue()

    state = Mock()
    state.state = sensor_state
    bt.hass.states.get.return_value = state
    return bt


def _event(state_value):
    new_state = Mock()
    new_state.state = state_value
    event = Mock()
    event.data = {"new_state": new_state}
    return event


@pytest.mark.asyncio
@pytest.mark.parametrize("reading", ["on", "true", "open", "unknown", "unavailable"])
async def test_open_readings_queue_an_open_event(reading):
    """Every documented open synonym is accepted as open."""
    bt = _make_bt(sensor_state=reading)
    await trigger_door_change(bt, _event(reading))
    assert bt.door_queue_task.get_nowait() is True


@pytest.mark.asyncio
@pytest.mark.parametrize("reading", ["off", "false", "closed"])
async def test_closed_readings_queue_a_close_event(reading):
    """Every documented closed synonym is accepted as closed."""
    bt = _make_bt(sensor_state=reading, door_open=True)
    await trigger_door_change(bt, _event(reading))
    assert bt.door_queue_task.get_nowait() is False


@pytest.mark.asyncio
async def test_unrecognized_state_raises_a_door_issue():
    """Anything outside the documented vocabulary raises a door repair issue."""
    bt = _make_bt(sensor_state="banana")
    with patch(f"{_CONTACT}.ir.async_create_issue") as issue:
        await trigger_door_change(bt, _event("banana"))
    issue.assert_called_once()
    assert issue.call_args.kwargs["translation_key"] == "invalid_door_state"
    assert bt.door_queue_task.empty()


@pytest.mark.asyncio
async def test_unchanged_state_is_skipped():
    """Events that do not change the saved door state are dropped."""
    bt = _make_bt(sensor_state="off", door_open=False)
    await trigger_door_change(bt, _event("off"))
    assert bt.door_queue_task.empty()


@pytest.mark.asyncio
async def test_queue_confirms_open_and_triggers_control():
    """A confirmed door-open event sets door_open and queues a control run."""
    bt = _make_bt(sensor_state="on")
    bt.door_delay = 0
    bt.door_delay_after = 0
    bt.in_maintenance = False
    bt.control_queue_task = asyncio.Queue()

    task = asyncio.create_task(door_queue(bt))
    await bt.door_queue_task.put(True)
    await asyncio.wait_for(bt.door_queue_task.join(), timeout=1)

    assert bt.door_open is True
    bt.async_write_ha_state.assert_called_once()
    assert bt.control_queue_task.qsize() == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_queue_ignores_event_when_sensor_flipped_back():
    """A door that closed again during the debounce delay keeps its state."""
    bt = _make_bt(sensor_state="off")
    bt.door_delay = 0
    bt.door_delay_after = 0
    bt.in_maintenance = False
    bt.control_queue_task = asyncio.Queue()

    task = asyncio.create_task(door_queue(bt))
    await bt.door_queue_task.put(True)
    await asyncio.wait_for(bt.door_queue_task.join(), timeout=1)

    assert bt.door_open is False
    bt.async_write_ha_state.assert_not_called()
    assert bt.control_queue_task.qsize() == 0

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_queue_survives_a_sensor_that_vanished_during_debounce():
    """A sensor removed during the debounce delay drops the event, not the task."""
    bt = _make_bt(sensor_state="on")
    bt.door_delay = 0
    bt.door_delay_after = 0
    bt.hass.states.get.return_value = None

    task = asyncio.create_task(door_queue(bt))
    await bt.door_queue_task.put(True)
    await asyncio.wait_for(bt.door_queue_task.join(), timeout=1)

    assert not task.done()
    bt.async_write_ha_state.assert_not_called()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_queue_skips_event_on_unrecognized_state_during_debounce():
    """A state outside the vocabulary during debounce confirms nothing."""
    bt = _make_bt(sensor_state="banana")
    bt.door_delay = 0
    bt.door_delay_after = 0
    bt.in_maintenance = False
    bt.control_queue_task = asyncio.Queue()

    task = asyncio.create_task(door_queue(bt))
    await bt.door_queue_task.put(True)
    await asyncio.wait_for(bt.door_queue_task.join(), timeout=1)

    assert bt.door_open is False
    bt.async_write_ha_state.assert_not_called()
    assert bt.control_queue_task.qsize() == 0

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_valid_reading_clears_stale_invalid_state_issue():
    """A recognized reading deletes any previously raised invalid-state issue."""
    bt = _make_bt(sensor_state="on")
    with patch(f"{_CONTACT}.ir.async_delete_issue") as delete:
        await trigger_door_change(bt, _event("on"))
    delete.assert_called_once()
    assert delete.call_args.args[2] == "invalid_door_state_Test BT"


@pytest.mark.asyncio
async def test_unrecognized_state_does_not_clear_the_issue():
    """An invalid reading raises the issue and must not delete it."""
    bt = _make_bt(sensor_state="banana")
    with (
        patch(f"{_CONTACT}.ir.async_create_issue"),
        patch(f"{_CONTACT}.ir.async_delete_issue") as delete,
    ):
        await trigger_door_change(bt, _event("banana"))
    delete.assert_not_called()
