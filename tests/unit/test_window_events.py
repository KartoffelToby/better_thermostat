"""Tests for the window event handlers around the window FSM.

The FSM transitions themselves are pinned in test_fsm_window.py; these
tests cover the shell glue: sensor events starting pending transitions,
the queue handler committing or cancelling them, and the control kicks.
"""

import asyncio
from dataclasses import replace
from unittest.mock import MagicMock, Mock, patch

import pytest

from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.decide import running_kernel_state
from custom_components.better_thermostat.core.fsm.window import WindowPhase, WindowState
from custom_components.better_thermostat.events.window import (
    trigger_window_change,
    window_queue,
)

_WINDOW = "custom_components.better_thermostat.events.window"


def _make_bt(*, sensor_state="off", window_open=False, open_delay=0, close_delay=0):
    bt = MagicMock()
    bt.device_name = "Test BT"
    bt.window_id = "binary_sensor.window"
    bt.window_open = window_open
    bt.window_delay = open_delay
    bt.window_delay_after = close_delay
    bt.clock = FakeClock()
    bt.kernel_state = running_kernel_state()
    bt.kernel_state = replace(
        bt.kernel_state,
        window=WindowState(
            phase=WindowPhase.OPEN if window_open else WindowPhase.CLOSED
        ),
    )
    bt.in_maintenance = False
    bt._heating_tracker = Mock()
    bt.async_write_ha_state = Mock()
    bt.window_queue_task = asyncio.Queue()
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


class TestTriggerWindowChange:
    """Sensor events start pending transitions and enqueue the re-check."""

    @pytest.mark.asyncio
    async def test_open_event_starts_pending_transition(self):
        """An 'on' reading enters OPENING and queues the delayed re-check."""
        bt = _make_bt(sensor_state="on", open_delay=10)
        await trigger_window_change(bt, _event("on"))
        assert bt.kernel_state.window.phase == WindowPhase.OPENING
        assert bt.window_queue_task.get_nowait() is False
        # Heating power learning is disabled for the open period.
        assert bt._heating_tracker.start_temp is None

    @pytest.mark.asyncio
    async def test_close_event_starts_pending_transition(self):
        """An 'off' reading on an open window enters CLOSING."""
        bt = _make_bt(sensor_state="off", window_open=True, close_delay=10)
        await trigger_window_change(bt, _event("off"))
        assert bt.kernel_state.window.phase == WindowPhase.CLOSING
        assert bt.window_queue_task.get_nowait() is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reading", ["true", "open"])
    async def test_open_synonyms_are_accepted(self, reading):
        """'true' and 'open' count as open, as the repair issue promises."""
        bt = _make_bt(sensor_state=reading, open_delay=10)
        await trigger_window_change(bt, _event(reading))
        assert bt.kernel_state.window.phase == WindowPhase.OPENING

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reading", ["false", "closed"])
    async def test_closed_synonyms_are_accepted(self, reading):
        """'false' and 'closed' count as closed."""
        bt = _make_bt(sensor_state=reading, window_open=True, close_delay=10)
        await trigger_window_change(bt, _event(reading))
        assert bt.kernel_state.window.phase == WindowPhase.CLOSING

    @pytest.mark.asyncio
    async def test_unknown_sensor_state_is_treated_as_closed(self):
        """'unknown' counts as closed so heating continues; no open transition."""
        bt = _make_bt(sensor_state="unknown", open_delay=10)
        await trigger_window_change(bt, _event("unknown"))
        # Window was closed and stays closed: nothing to queue.
        assert bt.kernel_state.window.phase == WindowPhase.CLOSED
        assert bt.window_queue_task.empty()

    @pytest.mark.asyncio
    async def test_unavailable_sensor_closes_an_open_window(self):
        """A lost sensor on an open window closes it so heating resumes."""
        bt = _make_bt(sensor_state="unavailable", window_open=True, close_delay=10)
        await trigger_window_change(bt, _event("unavailable"))
        assert bt.kernel_state.window.phase == WindowPhase.CLOSING
        assert bt.window_queue_task.get_nowait() is True

    @pytest.mark.asyncio
    async def test_unchanged_state_is_skipped(self):
        """An event repeating the committed state queues nothing."""
        bt = _make_bt(sensor_state="on", window_open=True)
        await trigger_window_change(bt, _event("on"))
        assert bt.window_queue_task.empty()
        assert bt.kernel_state.window.phase == WindowPhase.OPEN

    @pytest.mark.asyncio
    async def test_unrecognized_state_raises_an_issue(self):
        """Garbage sensor values raise a repair issue and queue nothing."""
        bt = _make_bt(sensor_state="banana")
        with patch(f"{_WINDOW}.ir.async_create_issue") as issue:
            await trigger_window_change(bt, _event("banana"))
        issue.assert_called_once()
        assert bt.window_queue_task.empty()

    @pytest.mark.asyncio
    async def test_missing_sensor_state_returns_early(self):
        """Without a sensor state in hass, the event is ignored."""
        bt = _make_bt()
        bt.hass.states.get.return_value = None
        await trigger_window_change(bt, _event("on"))
        assert bt.window_queue_task.empty()


async def _run_queue_once(bt):
    """Drive window_queue through exactly one queued event."""
    task = asyncio.create_task(window_queue(bt))
    await asyncio.wait_for(bt.window_queue_task.join(), timeout=1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


class TestWindowQueue:
    """The queue handler commits, cancels, and kicks control."""

    @pytest.mark.asyncio
    async def test_commit_open_kicks_the_control_queue(self):
        """A confirmed open commits the region and queues a control cycle."""
        bt = _make_bt(sensor_state="on")
        await trigger_window_change(bt, _event("on"))
        await _run_queue_once(bt)
        assert bt.kernel_state.window.phase == WindowPhase.OPEN
        assert bt.kernel_state.window.effective_open is True
        assert bt.control_queue_task.qsize() == 1

    @pytest.mark.asyncio
    async def test_commit_close_kicks_the_control_queue(self):
        """A confirmed close commits the region and queues a control cycle."""
        bt = _make_bt(sensor_state="off", window_open=True)
        await trigger_window_change(bt, _event("off"))
        await _run_queue_once(bt)
        assert bt.kernel_state.window.phase == WindowPhase.CLOSED
        assert bt.kernel_state.window.effective_open is False
        assert bt.control_queue_task.qsize() == 1

    @pytest.mark.asyncio
    async def test_false_positive_does_not_commit(self):
        """A sensor that reverted within the debounce window changes nothing.

        With a zero delay there is no debounce window — the transition
        commits at the event itself — so the false positive only exists
        for a configured delay.
        """
        bt = _make_bt(sensor_state="on", open_delay=5)
        await trigger_window_change(bt, _event("on"))
        # The sensor reads 'off' again by the time the wait elapses.
        bt.hass.states.get.return_value.state = "off"

        async def fake_sleep(seconds):
            bt.clock.advance(seconds)

        with patch(f"{_WINDOW}.asyncio.sleep", side_effect=fake_sleep):
            await _run_queue_once(bt)

        assert bt.kernel_state.window.phase == WindowPhase.CLOSED
        assert bt.kernel_state.window.effective_open is False
        assert bt.control_queue_task.empty()

    @pytest.mark.asyncio
    async def test_maintenance_defers_the_control_kick(self):
        """During maintenance the state updates but control is deferred."""
        bt = _make_bt(sensor_state="on")
        bt.in_maintenance = True
        await trigger_window_change(bt, _event("on"))
        await _run_queue_once(bt)
        assert bt.kernel_state.window.effective_open is True
        assert bt.control_queue_task.empty()
        assert bt._control_needed_after_maintenance is True

    @pytest.mark.asyncio
    async def test_delay_raised_mid_flight_still_commits(self):
        """A delay raised mid-debounce extends the wait.

        It must not strand the region in OPENING.
        """
        bt = _make_bt(sensor_state="on", open_delay=5)
        await trigger_window_change(bt, _event("on"))

        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)
            bt.clock.advance(seconds)
            # The user raises the delay while the first wait runs.
            bt.window_delay = 30

        with patch(f"{_WINDOW}.asyncio.sleep", side_effect=fake_sleep):
            await _run_queue_once(bt)

        assert slept == [5, 25]
        assert bt.kernel_state.window.phase == WindowPhase.OPEN
        assert bt.control_queue_task.qsize() == 1

    @pytest.mark.asyncio
    async def test_reopen_during_close_debounce_cancels_without_kick(self):
        """A window reopened during the close debounce stays open.

        The cancelled transition must not kick the control queue.
        """
        bt = _make_bt(sensor_state="off", window_open=True, close_delay=5)
        await trigger_window_change(bt, _event("off"))
        # The sensor reads 'on' again by the time the wait elapses.
        bt.hass.states.get.return_value.state = "on"

        async def fake_sleep(seconds):
            bt.clock.advance(seconds)

        with patch(f"{_WINDOW}.asyncio.sleep", side_effect=fake_sleep):
            await _run_queue_once(bt)

        assert bt.kernel_state.window.phase == WindowPhase.OPEN
        assert bt.control_queue_task.empty()

    @pytest.mark.asyncio
    async def test_pending_control_items_are_replaced(self):
        """A stale control item is dropped in favor of the fresh kick."""
        bt = _make_bt(sensor_state="on")
        await bt.control_queue_task.put("stale")
        await trigger_window_change(bt, _event("on"))
        await _run_queue_once(bt)
        assert bt.control_queue_task.qsize() == 1
        assert bt.control_queue_task.get_nowait() is bt


@pytest.mark.asyncio
async def test_cancellation_during_processing_propagates():
    """A cancel arriving mid-debounce is logged and re-raised cleanly."""
    bt = _make_bt(sensor_state="on", open_delay=30)
    await trigger_window_change(bt, _event("on"))
    entered_sleep = asyncio.Event()

    async def fake_sleep(_seconds):
        entered_sleep.set()
        await asyncio.Future()  # cancelled by task cancellation

    with patch(f"{_WINDOW}.asyncio.sleep", side_effect=fake_sleep):
        task = asyncio.create_task(window_queue(bt))
        await entered_sleep.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
