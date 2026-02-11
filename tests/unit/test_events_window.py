"""Tests for events/window.py module."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.better_thermostat.events.window import (
    empty_queue,
    trigger_window_change,
    window_queue,
)


class TestTriggerWindowChange:
    """Test trigger_window_change function."""

    @pytest.fixture
    def mock_self(self):
        """Create a mock BetterThermostat instance."""
        mock = MagicMock()
        mock.device_name = "Test Thermostat"
        mock.hass = MagicMock()
        mock.window_id = "binary_sensor.window"
        mock.window_open = False
        mock.window_queue_task = AsyncMock()
        mock.window_queue_task.put = AsyncMock()
        mock.async_write_ha_state = MagicMock()
        return mock

    @pytest.mark.anyio
    async def test_puts_true_in_queue_when_window_opens(self, mock_self):
        """Test that True is put in queue when window opens."""
        mock_state = MagicMock()
        mock_state.state = "on"
        mock_self.hass.states.get.return_value = mock_state

        event = MagicMock()
        event.data = {"new_state": mock_state}

        await trigger_window_change(mock_self, event)

        mock_self.window_queue_task.put.assert_called_once_with(True)

    @pytest.mark.anyio
    async def test_puts_false_in_queue_when_window_closes(self, mock_self):
        """Test that False is put in queue when window closes."""
        mock_self.window_open = True
        mock_state = MagicMock()
        mock_state.state = "off"
        mock_self.hass.states.get.return_value = mock_state

        event = MagicMock()
        event.data = {"new_state": mock_state}

        await trigger_window_change(mock_self, event)

        mock_self.window_queue_task.put.assert_called_once_with(False)

    @pytest.mark.anyio
    async def test_skips_when_new_state_is_none(self, mock_self):
        """Test that function returns early when new_state is None."""
        event = MagicMock()
        event.data = {"new_state": None}

        await trigger_window_change(mock_self, event)

        mock_self.window_queue_task.put.assert_not_called()

    @pytest.mark.anyio
    async def test_skips_when_window_state_unchanged(self, mock_self):
        """Test that function skips when window state hasn't changed."""
        mock_self.window_open = False
        mock_state = MagicMock()
        mock_state.state = "off"
        mock_self.hass.states.get.return_value = mock_state

        event = MagicMock()
        event.data = {"new_state": mock_state}

        await trigger_window_change(mock_self, event)

        # Should not put anything in queue when state hasn't changed
        mock_self.window_queue_task.put.assert_not_called()

    @pytest.mark.anyio
    async def test_treats_unknown_as_open(self, mock_self):
        """Test that 'unknown' state is treated as window open."""
        mock_state = MagicMock()
        mock_state.state = "unknown"
        mock_self.hass.states.get.return_value = mock_state

        event = MagicMock()
        event.data = {"new_state": mock_state}

        await trigger_window_change(mock_self, event)

        mock_self.window_queue_task.put.assert_called_once_with(True)

    @pytest.mark.anyio
    async def test_treats_unavailable_as_open(self, mock_self):
        """Test that 'unavailable' state is treated as window open."""
        mock_state = MagicMock()
        mock_state.state = "unavailable"
        mock_self.hass.states.get.return_value = mock_state

        event = MagicMock()
        event.data = {"new_state": mock_state}

        await trigger_window_change(mock_self, event)

        mock_self.window_queue_task.put.assert_called_once_with(True)

    @pytest.mark.anyio
    async def test_resets_heating_start_temp_when_window_opens(self, mock_self):
        """Test that heating_start_temp is reset when window opens."""
        mock_self.heating_start_temp = 20.0
        mock_state = MagicMock()
        mock_state.state = "on"
        mock_self.hass.states.get.return_value = mock_state

        event = MagicMock()
        event.data = {"new_state": mock_state}

        await trigger_window_change(mock_self, event)

        assert mock_self.heating_start_temp is None

    @pytest.mark.anyio
    async def test_creates_issue_for_invalid_state(self, mock_self):
        """Test that issue is created for invalid window state."""
        mock_state = MagicMock()
        mock_state.state = "invalid"
        mock_self.hass.states.get.return_value = mock_state

        event = MagicMock()
        event.data = {"new_state": mock_state}

        with patch(
            "custom_components.better_thermostat.events.window.ir.async_create_issue"
        ) as mock_create_issue:
            await trigger_window_change(mock_self, event)

        mock_create_issue.assert_called_once()
        # Should not put anything in queue for invalid state
        mock_self.window_queue_task.put.assert_not_called()


class TestWindowQueue:
    """Test window_queue function."""

    @pytest.fixture
    def mock_self(self):
        """Create a mock BetterThermostat instance."""
        mock = MagicMock()
        mock.device_name = "Test Thermostat"
        mock.hass = MagicMock()
        mock.window_id = "binary_sensor.window"
        mock.window_delay = 5
        mock.window_delay_after = 10
        mock.window_open = False
        mock.control_queue_task = AsyncMock()
        mock.control_queue_task.put = AsyncMock()
        mock.control_queue_task.empty = MagicMock(return_value=True)
        mock.async_write_ha_state = MagicMock()
        return mock

    @pytest.mark.anyio
    async def test_waits_before_opening_window(self, mock_self):
        """Test that function waits configured delay before opening window."""
        queue = asyncio.Queue()
        mock_self.window_queue_task = queue

        # Put window open event in queue
        await queue.put(True)

        mock_window_state = MagicMock()
        mock_window_state.state = "on"
        mock_self.hass.states.get.return_value = mock_window_state

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            # Start the queue processor in background
            task = asyncio.create_task(window_queue(mock_self))

            # Give it time to process
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Should wait window_delay (5 seconds)
        assert any(call[0][0] == 5 for call in mock_sleep.call_args_list)

    @pytest.mark.anyio
    async def test_waits_before_closing_window(self, mock_self):
        """Test that function waits configured delay after closing window."""
        queue = asyncio.Queue()
        mock_self.window_queue_task = queue

        # Put window close event in queue
        await queue.put(False)

        mock_window_state = MagicMock()
        mock_window_state.state = "off"
        mock_self.hass.states.get.return_value = mock_window_state

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            task = asyncio.create_task(window_queue(mock_self))

            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Should wait window_delay_after (10 seconds)
        assert any(call[0][0] == 10 for call in mock_sleep.call_args_list)

    @pytest.mark.anyio
    async def test_updates_window_open_state_after_delay(self, mock_self):
        """Test that window_open is updated after delay."""
        queue = asyncio.Queue()
        mock_self.window_queue_task = queue

        await queue.put(True)

        mock_window_state = MagicMock()
        mock_window_state.state = "on"
        mock_self.hass.states.get.return_value = mock_window_state

        with patch("asyncio.sleep", new_callable=AsyncMock):
            task = asyncio.create_task(window_queue(mock_self))

            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert mock_self.window_open is True

    @pytest.mark.anyio
    async def test_defers_control_during_maintenance(self, mock_self):
        """Test that control is deferred when in maintenance mode."""
        queue = asyncio.Queue()
        mock_self.window_queue_task = queue
        mock_self.in_maintenance = True
        mock_self._control_needed_after_maintenance = False

        await queue.put(True)

        mock_window_state = MagicMock()
        mock_window_state.state = "on"
        mock_self.hass.states.get.return_value = mock_window_state

        with patch("asyncio.sleep", new_callable=AsyncMock):
            task = asyncio.create_task(window_queue(mock_self))

            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Should set flag instead of calling control queue
        assert mock_self._control_needed_after_maintenance is True


class TestEmptyQueue:
    """Test empty_queue function."""

    def test_empties_queue_completely(self):
        """Test that function empties all items from queue."""
        queue = asyncio.Queue()
        queue.put_nowait("item1")
        queue.put_nowait("item2")
        queue.put_nowait("item3")

        assert queue.qsize() == 3

        empty_queue(queue)

        assert queue.qsize() == 0

    def test_handles_empty_queue(self):
        """Test that function handles already empty queue."""
        queue = asyncio.Queue()

        # Should not raise exception
        empty_queue(queue)

        assert queue.qsize() == 0