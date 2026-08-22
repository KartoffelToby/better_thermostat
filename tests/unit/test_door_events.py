"""Tests for the door event handlers around the door FSM.

The FSM transitions themselves are pinned in test_fsm_window.py (the door
reuses the window region); these tests cover the shell glue: sensor events
starting pending transitions, the queue handler committing or cancelling
them, and the control kicks.
"""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.decide import running_kernel_state
from custom_components.better_thermostat.core.fsm.window import WindowPhase, WindowState
from custom_components.better_thermostat.events.door import (
    door_queue,
    trigger_door_change,
)

_DOOR = "custom_components.better_thermostat.events.door"
_LOGBOOK = f"{_DOOR}.async_fire_logbook_entry"


def _make_bt(*, sensor_state="off", door_open=False, open_delay=0, close_delay=0):
    bt = MagicMock()
    bt.device_name = "Test BT"
    bt.door_id = "binary_sensor.door"
    bt.door_open = door_open
    bt.door_delay = open_delay
    bt.door_delay_after = close_delay
    bt.clock = FakeClock()
    bt.kernel_state = running_kernel_state()
    bt.kernel_state = replace(
        bt.kernel_state,
        door=WindowState(phase=WindowPhase.OPEN if door_open else WindowPhase.CLOSED),
    )
    bt.in_maintenance = False
    bt._heating_tracker = Mock()
    bt.async_write_ha_state = Mock()
    bt.door_queue_task = asyncio.Queue()
    bt.control_queue_task = asyncio.Queue()

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


class TestTriggerDoorChange:
    """Sensor events start pending transitions and enqueue the re-check."""

    @pytest.mark.asyncio
    async def test_open_event_starts_pending_transition(self):
        """An 'on' reading enters OPENING and queues the delayed re-check."""
        bt = _make_bt(sensor_state="on", open_delay=10)
        await trigger_door_change(bt, _event("on"))
        assert bt.kernel_state.door.phase == WindowPhase.OPENING
        assert bt.door_queue_task.get_nowait() is False
        # Heating power learning is disabled for the open period.
        assert bt._heating_tracker.start_temp is None

    @pytest.mark.asyncio
    async def test_close_event_starts_pending_transition(self):
        """An 'off' reading on an open door enters CLOSING."""
        bt = _make_bt(sensor_state="off", door_open=True, close_delay=10)
        await trigger_door_change(bt, _event("off"))
        assert bt.kernel_state.door.phase == WindowPhase.CLOSING
        assert bt.door_queue_task.get_nowait() is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reading", ["true", "open"])
    async def test_open_synonyms_are_accepted(self, reading):
        """'true' and 'open' count as open, as the repair issue promises."""
        bt = _make_bt(sensor_state=reading, open_delay=10)
        await trigger_door_change(bt, _event(reading))
        assert bt.kernel_state.door.phase == WindowPhase.OPENING

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reading", ["false", "closed"])
    async def test_closed_synonyms_are_accepted(self, reading):
        """'false' and 'closed' count as closed."""
        bt = _make_bt(sensor_state=reading, door_open=True, close_delay=10)
        await trigger_door_change(bt, _event(reading))
        assert bt.kernel_state.door.phase == WindowPhase.CLOSING

    @pytest.mark.asyncio
    async def test_unknown_sensor_state_is_treated_as_closed(self):
        """'unknown' counts as closed so heating continues; no open transition."""
        bt = _make_bt(sensor_state="unknown", open_delay=10)
        await trigger_door_change(bt, _event("unknown"))
        # Door was closed and stays closed: nothing to queue.
        assert bt.kernel_state.door.phase == WindowPhase.CLOSED
        assert bt.door_queue_task.empty()

    @pytest.mark.asyncio
    async def test_unavailable_sensor_closes_an_open_door(self):
        """A lost sensor on an open door closes it so heating resumes."""
        bt = _make_bt(sensor_state="unavailable", door_open=True, close_delay=10)
        await trigger_door_change(bt, _event("unavailable"))
        assert bt.kernel_state.door.phase == WindowPhase.CLOSING
        assert bt.door_queue_task.get_nowait() is True

    @pytest.mark.asyncio
    async def test_unchanged_state_is_skipped(self):
        """An event repeating the committed state queues nothing."""
        bt = _make_bt(sensor_state="on", door_open=True)
        await trigger_door_change(bt, _event("on"))
        assert bt.door_queue_task.empty()
        assert bt.kernel_state.door.phase == WindowPhase.OPEN

    @pytest.mark.asyncio
    async def test_flip_during_open_debounce_cancels_the_region(self):
        """A close reading during a pending OPENING reaches the region.

        The region cancels the false positive instead of the event being
        swallowed by the committed-state dedup.
        """
        bt = _make_bt(sensor_state="on", open_delay=10)
        await trigger_door_change(bt, _event("on"))
        assert bt.kernel_state.door.phase == WindowPhase.OPENING

        bt.hass.states.get.return_value.state = "off"
        await trigger_door_change(bt, _event("off"))
        assert bt.kernel_state.door.phase == WindowPhase.CLOSED
        assert bt.door_queue_task.qsize() == 2

    @pytest.mark.asyncio
    async def test_reopen_after_flip_restarts_the_debounce(self):
        """A re-open after a mid-debounce flip starts a fresh pending window."""
        bt = _make_bt(sensor_state="on", open_delay=10)
        await trigger_door_change(bt, _event("on"))
        bt.hass.states.get.return_value.state = "off"
        await trigger_door_change(bt, _event("off"))

        bt.clock.advance(3)
        bt.hass.states.get.return_value.state = "on"
        await trigger_door_change(bt, _event("on"))
        assert bt.kernel_state.door.phase == WindowPhase.OPENING
        assert bt.kernel_state.door.pending_since == bt.clock.monotonic()

    @pytest.mark.asyncio
    async def test_rapid_flips_on_bounded_queue_do_not_block(self):
        """Mid-debounce flips on the production maxsize-1 queue coalesce.

        Extra flips drop their queue item instead of blocking a putter;
        the settle worker still commits the region and announces once.
        """
        bt = _make_bt(sensor_state="on", open_delay=5)
        bt.door_queue_task = asyncio.Queue(maxsize=1)
        tasks_before = asyncio.all_tasks()
        for reading in ("on", "off", "on", "off", "on"):
            bt.hass.states.get.return_value.state = reading
            await asyncio.wait_for(trigger_door_change(bt, _event(reading)), timeout=1)
        assert bt.door_queue_task.qsize() == 1
        assert asyncio.all_tasks() == tasks_before
        assert bt.kernel_state.door.phase == WindowPhase.OPENING

        async def fake_sleep(seconds):
            bt.clock.advance(seconds)

        with (
            patch(f"{_DOOR}.asyncio.sleep", side_effect=fake_sleep),
            patch(_LOGBOOK, new_callable=AsyncMock) as logbook,
            patch(f"{_DOOR}.request_control_cycle") as kick,
        ):
            await _run_queue_once(bt)

        assert bt.kernel_state.door.phase == WindowPhase.OPEN
        assert logbook.call_count == 1
        assert logbook.call_args.args[1] == "door_open"
        assert kick.call_count == 1

    @pytest.mark.asyncio
    async def test_unrecognized_state_raises_an_issue(self):
        """Garbage sensor values raise a repair issue and queue nothing."""
        bt = _make_bt(sensor_state="banana")
        with patch(f"{_DOOR}.ir.async_create_issue") as issue:
            await trigger_door_change(bt, _event("banana"))
        issue.assert_called_once()
        assert bt.door_queue_task.empty()

    @pytest.mark.asyncio
    async def test_missing_sensor_state_returns_early(self):
        """Without a sensor state in hass, the event is ignored."""
        bt = _make_bt()
        bt.hass.states.get.return_value = None
        await trigger_door_change(bt, _event("on"))
        assert bt.door_queue_task.empty()


async def _run_queue_once(bt):
    """Drive door_queue through exactly one queued event."""
    task = asyncio.create_task(door_queue(bt))
    await asyncio.wait_for(bt.door_queue_task.join(), timeout=1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


class TestDoorQueue:
    """The queue handler commits, cancels, and kicks control."""

    @pytest.mark.asyncio
    async def test_commit_open_kicks_the_control_queue(self):
        """A confirmed open commits the region and queues a control cycle."""
        bt = _make_bt(sensor_state="on")
        await trigger_door_change(bt, _event("on"))
        await _run_queue_once(bt)
        assert bt.kernel_state.door.phase == WindowPhase.OPEN
        assert bt.kernel_state.door.effective_open is True
        assert bt.control_queue_task.qsize() == 1

    @pytest.mark.asyncio
    async def test_commit_close_kicks_the_control_queue(self):
        """A confirmed close commits the region and queues a control cycle."""
        bt = _make_bt(sensor_state="off", door_open=True)
        await trigger_door_change(bt, _event("off"))
        await _run_queue_once(bt)
        assert bt.kernel_state.door.phase == WindowPhase.CLOSED
        assert bt.kernel_state.door.effective_open is False
        assert bt.control_queue_task.qsize() == 1

    @pytest.mark.asyncio
    async def test_false_positive_does_not_commit(self):
        """A sensor that reverted within the debounce window changes nothing.

        With a zero delay there is no debounce window — the transition
        commits at the event itself — so the false positive only exists
        for a configured delay.
        """
        bt = _make_bt(sensor_state="on", open_delay=5)
        await trigger_door_change(bt, _event("on"))
        # The sensor reads 'off' again by the time the wait elapses.
        bt.hass.states.get.return_value.state = "off"

        async def fake_sleep(seconds):
            bt.clock.advance(seconds)

        with patch(f"{_DOOR}.asyncio.sleep", side_effect=fake_sleep):
            await _run_queue_once(bt)

        assert bt.kernel_state.door.phase == WindowPhase.CLOSED
        assert bt.kernel_state.door.effective_open is False
        assert bt.control_queue_task.empty()

    @pytest.mark.asyncio
    async def test_maintenance_defers_the_control_kick(self):
        """During maintenance the state updates but control is deferred."""
        bt = _make_bt(sensor_state="on")
        bt.in_maintenance = True
        await trigger_door_change(bt, _event("on"))
        await _run_queue_once(bt)
        assert bt.kernel_state.door.effective_open is True
        assert bt.control_queue_task.empty()
        assert bt._control_needed_after_maintenance is True

    @pytest.mark.asyncio
    async def test_delay_raised_mid_flight_still_commits(self):
        """A delay raised mid-debounce extends the wait.

        It must not strand the region in OPENING.
        """
        bt = _make_bt(sensor_state="on", open_delay=5)
        await trigger_door_change(bt, _event("on"))

        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)
            bt.clock.advance(seconds)
            # The user raises the delay while the first wait runs.
            bt.door_delay = 30

        with patch(f"{_DOOR}.asyncio.sleep", side_effect=fake_sleep):
            await _run_queue_once(bt)

        assert slept == [5, 25]
        assert bt.kernel_state.door.phase == WindowPhase.OPEN
        assert bt.control_queue_task.qsize() == 1

    @pytest.mark.asyncio
    async def test_reopen_during_close_debounce_cancels_without_kick(self):
        """A door reopened during the close debounce stays open.

        The cancelled transition must not kick the control queue.
        """
        bt = _make_bt(sensor_state="off", door_open=True, close_delay=5)
        await trigger_door_change(bt, _event("off"))
        # The sensor reads 'on' again by the time the wait elapses.
        bt.hass.states.get.return_value.state = "on"

        async def fake_sleep(seconds):
            bt.clock.advance(seconds)

        with patch(f"{_DOOR}.asyncio.sleep", side_effect=fake_sleep):
            await _run_queue_once(bt)

        assert bt.kernel_state.door.phase == WindowPhase.OPEN
        assert bt.control_queue_task.empty()

    @pytest.mark.asyncio
    async def test_mid_debounce_flip_restarts_the_debounce(self):
        """Debounce continuity: open, close, reopen with open_delay=30.

        The door was not continuously open for 30s at t0+30, so the
        commit happens 30s after the reopen (t0+50), announced exactly
        once.
        """
        bt = _make_bt(sensor_state="on", open_delay=30)
        await trigger_door_change(bt, _event("on"))

        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 1:
                # The sensor flips closed at t0+10 and reopens at t0+20
                # while the settle worker waits out the first debounce.
                bt.clock.advance(10)
                bt.hass.states.get.return_value.state = "off"
                await trigger_door_change(bt, _event("off"))
                assert bt.kernel_state.door.phase == WindowPhase.CLOSED
                bt.clock.advance(10)
                bt.hass.states.get.return_value.state = "on"
                await trigger_door_change(bt, _event("on"))
                bt.clock.advance(10)
                # At t0+30 the original debounce would have committed.
                assert bt.kernel_state.door.effective_open is False
            else:
                bt.clock.advance(seconds)

        with (
            patch(f"{_DOOR}.asyncio.sleep", side_effect=fake_sleep),
            patch(_LOGBOOK, new_callable=AsyncMock) as logbook,
            patch(f"{_DOOR}.request_control_cycle") as kick,
        ):
            await _run_queue_once(bt)

        assert sleeps == [30, 20]
        assert bt.clock.monotonic() == 50
        assert bt.kernel_state.door.phase == WindowPhase.OPEN
        assert logbook.call_count == 1
        assert logbook.call_args.args[1] == "door_open"
        assert kick.call_count == 1

    @pytest.mark.asyncio
    async def test_rapid_toggle_announces_the_commit_once(self):
        """Several queued items sharing one commit announce it exactly once."""
        bt = _make_bt(sensor_state="on", open_delay=5)
        await trigger_door_change(bt, _event("on"))
        bt.hass.states.get.return_value.state = "off"
        await trigger_door_change(bt, _event("off"))
        bt.hass.states.get.return_value.state = "on"
        await trigger_door_change(bt, _event("on"))
        assert bt.door_queue_task.qsize() == 3

        async def fake_sleep(seconds):
            bt.clock.advance(seconds)

        with (
            patch(f"{_DOOR}.asyncio.sleep", side_effect=fake_sleep),
            patch(_LOGBOOK, new_callable=AsyncMock) as logbook,
            patch(f"{_DOOR}.request_control_cycle") as kick,
        ):
            await _run_queue_once(bt)

        assert bt.kernel_state.door.phase == WindowPhase.OPEN
        assert logbook.call_count == 1
        assert logbook.call_args.args[1] == "door_open"
        assert kick.call_count == 1

    @pytest.mark.asyncio
    async def test_zero_delay_open_close_announces_nothing(self):
        """A zero-delay open immediately followed by a close nets no flip.

        In particular no orphan 'door_close' logbook entry fires.
        """
        bt = _make_bt(sensor_state="on")
        await trigger_door_change(bt, _event("on"))
        bt.hass.states.get.return_value.state = "off"
        await trigger_door_change(bt, _event("off"))
        assert bt.door_queue_task.qsize() == 2

        with (
            patch(_LOGBOOK, new_callable=AsyncMock) as logbook,
            patch(f"{_DOOR}.request_control_cycle") as kick,
        ):
            await _run_queue_once(bt)

        assert bt.kernel_state.door.phase == WindowPhase.CLOSED
        logbook.assert_not_called()
        kick.assert_not_called()

    @pytest.mark.asyncio
    async def test_pending_control_items_are_replaced(self):
        """A stale control item is dropped in favor of the fresh kick."""
        bt = _make_bt(sensor_state="on")
        await bt.control_queue_task.put("stale")
        await trigger_door_change(bt, _event("on"))
        await _run_queue_once(bt)
        assert bt.control_queue_task.qsize() == 1
        assert bt.control_queue_task.get_nowait() is bt


@pytest.mark.asyncio
async def test_cancellation_during_processing_propagates():
    """A cancel arriving mid-debounce is logged and re-raised cleanly."""
    bt = _make_bt(sensor_state="on", open_delay=30)
    await trigger_door_change(bt, _event("on"))
    entered_sleep = asyncio.Event()

    async def fake_sleep(_seconds):
        entered_sleep.set()
        await asyncio.Future()  # cancelled by task cancellation

    with patch(f"{_DOOR}.asyncio.sleep", side_effect=fake_sleep):
        task = asyncio.create_task(door_queue(bt))
        await entered_sleep.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
