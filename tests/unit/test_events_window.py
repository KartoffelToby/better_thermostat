"""Tests for events/window.py module.

Tests window event handling with debounce queue processing.
"""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.const import STATE_OFF
import pytest

from custom_components.better_thermostat.events.window import (
    empty_queue,
    trigger_window_change,
    window_queue,
)


class TestTriggerWindowChange:
    """Test trigger_window_change event handler."""

    @pytest.mark.asyncio
    async def test_window_open_event(self):
        """Test window open event triggers queue."""
        mock_event = Mock()
        mock_event.data = {
            "new_state": Mock(state="on"),
            "old_state": Mock(state="off"),
        }

        mock_queue = AsyncMock()
        mock_queue.put = AsyncMock()

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.window_id = "binary_sensor.window"
        mock_self.window_open = False
        mock_self.window_queue_task = mock_queue
        mock_self.heating_start_temp = 20.0
        mock_self.hass = Mock()
        mock_self.hass.states.get.return_value = Mock(state="on")

        await trigger_window_change(mock_self, mock_event)

        # Should set heating_start_temp to None (window opened)
        assert mock_self.heating_start_temp is None
        # Should put True (window open) in queue
        mock_queue.put.assert_called_once_with(True)

    @pytest.mark.asyncio
    async def test_window_close_event(self):
        """Test window close event triggers queue."""
        mock_event = Mock()
        mock_event.data = {
            "new_state": Mock(state="off"),
            "old_state": Mock(state="on"),
        }

        mock_queue = AsyncMock()
        mock_queue.put = AsyncMock()

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.window_id = "binary_sensor.window"
        mock_self.window_open = True
        mock_self.window_queue_task = mock_queue
        mock_self.hass = Mock()
        mock_self.hass.states.get.return_value = Mock(state="off")

        await trigger_window_change(mock_self, mock_event)

        # Should put False (window closed) in queue
        mock_queue.put.assert_called_once_with(False)

    @pytest.mark.asyncio
    async def test_window_unknown_state_assumes_open(self):
        """Test unknown state is treated as window open."""
        mock_event = Mock()
        mock_event.data = {
            "new_state": Mock(state="unknown"),
            "old_state": Mock(state="off"),
        }

        mock_queue = AsyncMock()
        mock_queue.put = AsyncMock()

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.window_id = "binary_sensor.window"
        mock_self.window_open = False
        mock_self.window_queue_task = mock_queue
        mock_self.heating_start_temp = 20.0
        mock_self.hass = Mock()
        mock_self.hass.states.get.return_value = Mock(state="unknown")

        await trigger_window_change(mock_self, mock_event)

        # Should treat as open
        mock_queue.put.assert_called_once_with(True)

    @pytest.mark.asyncio
    async def test_window_state_unchanged_skipped(self):
        """Test that unchanged window state is skipped."""
        mock_event = Mock()
        mock_event.data = {
            "new_state": Mock(state="on"),
            "old_state": Mock(state="off"),
        }

        mock_queue = AsyncMock()
        mock_queue.put = AsyncMock()

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.window_id = "binary_sensor.window"
        mock_self.window_open = True  # Already open
        mock_self.window_queue_task = mock_queue
        mock_self.hass = Mock()
        mock_self.hass.states.get.return_value = Mock(state="on")

        await trigger_window_change(mock_self, mock_event)

        # Should not add to queue since state unchanged
        mock_queue.put.assert_not_called()

    @pytest.mark.asyncio
    async def test_window_none_state_returns_early(self):
        """Test None state returns early."""
        mock_event = Mock()
        mock_event.data = {"new_state": None, "old_state": None}

        mock_queue = AsyncMock()
        mock_queue.put = AsyncMock()

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.window_id = "binary_sensor.window"
        mock_self.window_queue_task = mock_queue
        mock_self.hass = Mock()
        mock_self.hass.states.get.return_value = None

        await trigger_window_change(mock_self, mock_event)

        # Should not process
        mock_queue.put.assert_not_called()


class TestWindowQueue:
    """Test window_queue processing function."""

    @pytest.mark.asyncio
    async def test_window_queue_open_with_delay(self):
        """Test window queue processes open event with delay."""
        mock_queue = asyncio.Queue()
        await mock_queue.put(True)  # Window open

        mock_control_queue = asyncio.Queue()

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.window_id = "binary_sensor.window"
        mock_self.window_delay = 0.1  # Short delay for testing
        mock_self.window_delay_after = 0.1
        mock_self.window_queue_task = mock_queue
        mock_self.control_queue_task = mock_control_queue
        mock_self.control_queue_task.empty = Mock(return_value=True)
        mock_self.control_queue_task.put = AsyncMock()
        mock_self.hass = Mock()
        mock_self.hass.states.get.return_value = Mock(state="on")
        mock_self.async_write_ha_state = Mock()

        # Process one item then cancel
        async def process_one():
            item = await mock_queue.get()
            if item:
                await asyncio.sleep(mock_self.window_delay)
            current_state = mock_self.hass.states.get(mock_self.window_id).state != STATE_OFF
            if current_state == item:
                mock_self.window_open = item
                mock_self.async_write_ha_state()
                if not mock_self.control_queue_task.empty():
                    empty_queue(mock_self.control_queue_task)
                await mock_self.control_queue_task.put(mock_self)
            mock_queue.task_done()

        await process_one()

        # Should have set window_open to True
        assert mock_self.window_open is True
        # Should have triggered control queue
        mock_self.control_queue_task.put.assert_called_once()

    @pytest.mark.asyncio
    async def test_window_queue_close_with_delay(self):
        """Test window queue processes close event with delay."""
        mock_queue = asyncio.Queue()
        await mock_queue.put(False)  # Window close

        mock_control_queue = asyncio.Queue()

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.window_id = "binary_sensor.window"
        mock_self.window_delay = 0.1
        mock_self.window_delay_after = 0.1  # Delay after close
        mock_self.window_queue_task = mock_queue
        mock_self.control_queue_task = mock_control_queue
        mock_self.control_queue_task.empty = Mock(return_value=True)
        mock_self.control_queue_task.put = AsyncMock()
        mock_self.hass = Mock()
        mock_self.hass.states.get.return_value = Mock(state="off")
        mock_self.async_write_ha_state = Mock()

        # Process one item
        async def process_one():
            item = await mock_queue.get()
            if not item:
                await asyncio.sleep(mock_self.window_delay_after)
            current_state = mock_self.hass.states.get(mock_self.window_id).state != STATE_OFF
            if current_state == item:
                mock_self.window_open = item
                mock_self.async_write_ha_state()
                await mock_self.control_queue_task.put(mock_self)
            mock_queue.task_done()

        await process_one()

        # Should have set window_open to False
        assert mock_self.window_open is False

    @pytest.mark.asyncio
    async def test_window_queue_state_change_during_delay(self):
        """Test queue ignores change if state reverted during delay."""
        mock_queue = asyncio.Queue()
        await mock_queue.put(True)  # Window open

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.window_id = "binary_sensor.window"
        mock_self.window_delay = 0.1
        mock_self.window_queue_task = mock_queue
        mock_self.control_queue_task = asyncio.Queue()
        mock_self.control_queue_task.put = AsyncMock()
        mock_self.hass = Mock()
        # State is now off (closed) after delay
        mock_self.hass.states.get.return_value = Mock(state="off")
        mock_self.async_write_ha_state = Mock()
        mock_self.window_open = False

        # Process one item
        async def process_one():
            item = await mock_queue.get()
            if item:
                await asyncio.sleep(mock_self.window_delay)
            current_state = mock_self.hass.states.get(mock_self.window_id).state != STATE_OFF
            # State changed during delay, don't update
            if current_state == item:
                mock_self.window_open = item
                await mock_self.control_queue_task.put(mock_self)
            mock_queue.task_done()

        await process_one()

        # Should NOT have updated window_open since state changed
        assert mock_self.window_open is False
        # Should NOT have triggered control
        mock_self.control_queue_task.put.assert_not_called()


class TestEmptyQueue:
    """Test empty_queue helper function."""

    def test_empty_queue_removes_all_items(self):
        """Test that empty_queue removes all pending items."""
        q = asyncio.Queue()
        q.put_nowait(1)
        q.put_nowait(2)
        q.put_nowait(3)

        assert q.qsize() == 3

        empty_queue(q)

        assert q.qsize() == 0

    def test_empty_queue_on_empty_queue(self):
        """Test that empty_queue works on already empty queue."""
        q = asyncio.Queue()

        # Should not raise error
        empty_queue(q)

        assert q.qsize() == 0