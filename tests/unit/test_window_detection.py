"""Tests for the window detection switch over the window and door regions.

Turned off, open windows and doors stop turning the heating off at once,
a close delay still running included. Turned back on, a window that is
open waits out the configured open delay before it turns the heating
off. The regions keep following their sensors while detection is off, so
an opening window still ends the heating cycle being learned.
"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest

from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.decide import decide, running_kernel_state
from custom_components.better_thermostat.core.desired import Suppression
from custom_components.better_thermostat.core.fsm.window import WindowPhase, WindowState
from custom_components.better_thermostat.core.snapshot import HvacMode
from custom_components.better_thermostat.events.contact import set_window_detection
from custom_components.better_thermostat.events.door import trigger_door_change
from custom_components.better_thermostat.events.window import (
    trigger_window_change,
    window_queue,
)
from custom_components.better_thermostat.utils.thermal_learning import (
    HeatingPowerTracker,
    Reading,
)
from tests.factories import ThermostatStandIn, make_snapshot, make_state

_CONTACT = "custom_components.better_thermostat.events.contact"
_LOGBOOK = f"{_CONTACT}.async_fire_logbook_entry"
WINDOW_ID = "binary_sensor.window"
DOOR_ID = "binary_sensor.door"


def _make_bt(
    *,
    window_reads="off",
    door_reads="off",
    window=WindowState(),
    door=WindowState(),
    detection=True,
    open_delay=0,
    close_delay=0,
    door_sensor=DOOR_ID,
):
    bt = ThermostatStandIn()
    bt.device_name = "Test BT"
    bt.window_sensor_entity_id = WINDOW_ID
    bt.door_sensor_entity_id = door_sensor
    bt.window_open_delay_seconds = open_delay
    bt.window_close_delay_seconds = close_delay
    bt.door_open_delay_seconds = open_delay
    bt.door_close_delay_seconds = close_delay
    bt.clock = FakeClock(monotonic_value=1000.0)
    bt.kernel_state = replace(
        running_kernel_state(), window=window, door=door, window_detection=detection
    )
    bt.in_maintenance = False
    bt._heating_tracker = HeatingPowerTracker(
        start=Reading(20.0, datetime(2026, 1, 1, tzinfo=UTC))
    )
    bt.async_write_ha_state = Mock()
    bt.window_queue_task = asyncio.Queue(maxsize=1)
    bt.door_queue_task = asyncio.Queue(maxsize=1)
    bt.control_queue_task = asyncio.Queue(maxsize=1)

    # Only a configured sensor can be read; any other lookup fails the test.
    readings = {WINDOW_ID: window_reads}
    if door_sensor is not None:
        readings[door_sensor] = door_reads

    def _state(entity_id):
        state = Mock()
        state.state = readings[entity_id]
        return state

    bt.hass.states.get.side_effect = _state
    return bt


def _event(state_value):
    new_state = Mock()
    new_state.state = state_value
    event = Mock()
    event.data = {"new_state": new_state}
    return event


async def _run_window_queue_once(bt):
    """Drive the window queue worker through every queued item."""
    task = asyncio.create_task(window_queue(bt))
    await asyncio.wait_for(bt.window_queue_task.join(), timeout=1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# --- decide(): the mask on the window/door tier ------------------------------


@pytest.mark.parametrize(
    "region",
    [
        WindowState(phase=WindowPhase.OPEN),
        WindowState(phase=WindowPhase.CLOSING, pending_since=900.0),
    ],
    ids=["open", "close_delay_running"],
)
@pytest.mark.parametrize("contact", ["window", "door"])
def test_detection_off_heats_through_an_open_contact(region, contact):
    """Off, an open contact leaves the heating to the heat demand."""
    desired, state = decide(
        make_snapshot(heat_target_temperature=21.0),
        make_state(window_detection=False, **{contact: region}),
    )
    assert desired.call_for_heat is True
    assert {trv.hvac_mode for trv in desired.trvs.values()} == {HvacMode.HEAT}
    assert {trv.suppression for trv in desired.trvs.values()} == {None}
    assert state.window_detection is False


def test_detection_on_turns_the_heating_off_during_the_close_delay():
    """On, a window inside its close delay still holds the heating off."""
    desired, _ = decide(
        make_snapshot(heat_target_temperature=21.0),
        make_state(window=WindowState(phase=WindowPhase.CLOSING, pending_since=900.0)),
    )
    assert {trv.suppression for trv in desired.trvs.values()} == {Suppression.WINDOW}


# --- Turning detection off ----------------------------------------------------


class TestTurningDetectionOff:
    """Off, every running delay is dropped and the regions read the sensor."""

    @pytest.mark.asyncio
    async def test_a_running_close_delay_is_dropped(self):
        """A window closed a moment ago stops holding the heating off at once."""
        bt = _make_bt(
            window_reads="off",
            window=WindowState(phase=WindowPhase.CLOSING, pending_since=990.0),
            close_delay=900,
        )
        await set_window_detection(bt, False)
        assert bt.kernel_state.window_detection is False
        assert bt.kernel_state.window == WindowState()
        assert bt.window_queue_task.empty()
        assert bt.control_queue_task.qsize() == 1
        bt.async_write_ha_state.assert_called()

    @pytest.mark.asyncio
    async def test_a_running_open_delay_commits_the_reading(self):
        """An opening still inside its delay is taken as open, without the wait.

        The region keeps following the sensor, so learning sees the window.
        """
        bt = _make_bt(
            door_reads="on",
            door=WindowState(phase=WindowPhase.OPENING, pending_since=990.0),
            open_delay=300,
        )
        await set_window_detection(bt, False)
        assert bt.kernel_state.door == WindowState(phase=WindowPhase.OPEN)
        assert bt.door_queue_task.empty()

    @pytest.mark.asyncio
    async def test_nothing_goes_to_the_logbook(self):
        """Turning the heating back on is the switch's doing, not a window's."""
        bt = _make_bt(window=WindowState(phase=WindowPhase.OPEN), window_reads="on")
        with patch(_LOGBOOK, new_callable=AsyncMock) as logbook:
            await set_window_detection(bt, False)
        logbook.assert_not_called()
        assert bt.kernel_state.window.phase == WindowPhase.OPEN

    @pytest.mark.asyncio
    async def test_maintenance_defers_the_control_kick(self):
        """During maintenance the control cycle waits until it ends."""
        bt = _make_bt()
        bt.in_maintenance = True
        await set_window_detection(bt, False)
        assert bt.control_queue_task.empty()
        assert bt._control_needed_after_maintenance is True

    @pytest.mark.asyncio
    async def test_setting_the_same_value_changes_nothing(self):
        """Repeating the current setting resets no region and kicks no cycle."""
        region = WindowState(phase=WindowPhase.OPENING, pending_since=990.0)
        bt = _make_bt(window_reads="on", window=region, open_delay=300)
        await set_window_detection(bt, True)
        assert bt.kernel_state.window == region
        assert bt.control_queue_task.empty()

    @pytest.mark.asyncio
    async def test_a_contact_kind_without_sensor_is_left_alone(self):
        """Only the configured contact kinds are read."""
        bt = _make_bt(window=WindowState(phase=WindowPhase.OPEN), door_sensor=None)
        await set_window_detection(bt, False)
        assert bt.kernel_state.door == WindowState()
        assert bt.kernel_state.window == WindowState()


class TestEventsWhileDetectionIsOff:
    """Off, sensor changes reach the regions without delay."""

    @pytest.mark.asyncio
    async def test_an_opening_commits_at_once_and_aborts_the_cycle(self):
        """The open delay does not apply, and the heating cycle learns nothing."""
        bt = _make_bt(window_reads="on", detection=False, open_delay=300)
        await trigger_window_change(bt, _event("on"))
        assert bt.kernel_state.window == WindowState(phase=WindowPhase.OPEN)
        assert bt._heating_tracker.start is None

    @pytest.mark.asyncio
    async def test_a_closing_commits_at_once(self):
        bt = _make_bt(
            door_reads="off",
            door=WindowState(phase=WindowPhase.OPEN),
            detection=False,
            close_delay=300,
        )
        await trigger_door_change(bt, _event("off"))
        assert bt.kernel_state.door == WindowState()

    @pytest.mark.asyncio
    async def test_the_worker_publishes_the_change_without_a_logbook_entry(self):
        """The window attribute follows the sensor; the logbook names no window.

        The window did not switch the heating, so a logbook entry saying it
        did would be wrong.
        """
        bt = _make_bt(window_reads="on", detection=False)
        await trigger_window_change(bt, _event("on"))
        with patch(_LOGBOOK, new_callable=AsyncMock) as logbook:
            await _run_window_queue_once(bt)
        logbook.assert_not_called()
        bt.async_write_ha_state.assert_called()
        assert bt.control_queue_task.qsize() == 1


# --- Turning detection back on ------------------------------------------------


class TestTurningDetectionOn:
    """On, the regions start over from the sensor with the configured delays."""

    @pytest.mark.asyncio
    async def test_an_open_window_waits_out_the_open_delay(self):
        """No immediate switch-off: the opening is pending for the open delay."""
        bt = _make_bt(
            window_reads="on",
            window=WindowState(phase=WindowPhase.OPEN),
            detection=False,
            open_delay=300,
        )
        await set_window_detection(bt, True)
        assert bt.kernel_state.window == WindowState(
            phase=WindowPhase.OPENING, pending_since=1000.0
        )
        assert bt.kernel_state.window.effective_open is False
        assert bt.window_queue_task.qsize() == 1
        assert bt.control_queue_task.qsize() == 1

    @pytest.mark.asyncio
    async def test_the_commit_after_the_delay_turns_the_heating_off(self):
        """The worker announces the open window once the delay has run out."""
        bt = _make_bt(
            window_reads="on",
            window=WindowState(phase=WindowPhase.OPEN),
            detection=False,
            open_delay=300,
        )
        # The worker announced the window while detection was off.
        bt.announced_contact_open["window"] = True
        await set_window_detection(bt, True)
        bt.control_queue_task.get_nowait()

        async def fake_sleep(seconds):
            bt.clock.advance(seconds)

        with (
            patch(f"{_CONTACT}.asyncio.sleep", side_effect=fake_sleep),
            patch(_LOGBOOK, new_callable=AsyncMock) as logbook,
        ):
            await _run_window_queue_once(bt)

        assert bt.kernel_state.window == WindowState(phase=WindowPhase.OPEN)
        assert logbook.call_args.args[1] == "window_open"
        assert bt.control_queue_task.qsize() == 1

    @pytest.mark.asyncio
    async def test_without_an_open_delay_the_heating_turns_off_at_once(self):
        """A delay of zero commits the open window with the switch."""
        bt = _make_bt(door_reads="on", door=WindowState(phase=WindowPhase.OPEN))
        bt.kernel_state = replace(bt.kernel_state, window_detection=False)
        with patch(_LOGBOOK, new_callable=AsyncMock) as logbook:
            await set_window_detection(bt, True)
        assert bt.kernel_state.door == WindowState(phase=WindowPhase.OPEN)
        assert bt.door_queue_task.empty()
        assert bt.announced_contact_open["door"] is True
        logbook.assert_called_once()
        assert logbook.call_args.args[1] == "door_open"

    @pytest.mark.asyncio
    async def test_a_closed_window_stays_closed(self):
        bt = _make_bt(detection=False, open_delay=300)
        with patch(_LOGBOOK, new_callable=AsyncMock) as logbook:
            await set_window_detection(bt, True)
        assert bt.kernel_state.window == WindowState()
        assert bt.window_queue_task.empty()
        logbook.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_full_settle_queue_is_left_to_the_worker(self):
        """A pending settle run re-reads the region, so nothing is lost."""
        bt = _make_bt(window_reads="on", detection=False, open_delay=300)
        bt.window_queue_task.put_nowait(False)
        await set_window_detection(bt, True)
        assert bt.kernel_state.window.phase == WindowPhase.OPENING
        assert bt.window_queue_task.qsize() == 1
