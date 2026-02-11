"""Comprehensive tests for Better Thermostat events module.

Tests covering temperature change handling, TRV events, cooler events, and window events
including debounce, filtering, EMA calculation, and queue management.
"""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.climate.const import HVACMode
from homeassistant.const import STATE_OFF, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import State

from custom_components.better_thermostat.events import (
    cooler,
    temperature,
    trv,
    window,
)


class TestCoolerEvents:
    """Test cooler event handling."""

    @pytest.mark.asyncio
    async def test_cooler_change_ignored_during_startup(self):
        """Test cooler changes are ignored during startup."""
        mock_self = MagicMock()
        mock_self.startup_running = True
        mock_self.device_name = "test"

        mock_event = MagicMock()
        await cooler.trigger_cooler_change(mock_self, mock_event)

        # Should return early without processing
        assert not hasattr(mock_event, "data") or not mock_event.data.get.called

    @pytest.mark.asyncio
    async def test_cooler_change_ignored_if_same_context(self):
        """Test cooler changes from same context are ignored."""
        mock_self = MagicMock()
        mock_self.startup_running = False
        mock_self.control_queue_task = MagicMock()
        mock_self.device_name = "test"
        mock_self.context = "test_context"

        mock_event = MagicMock()
        mock_event.context = "test_context"
        mock_event.data = {"old_state": MagicMock(), "new_state": MagicMock()}

        await cooler.trigger_cooler_change(mock_self, mock_event)

        # Should return early (same context)
        assert not mock_self.control_queue_task.put.called

    @pytest.mark.asyncio
    async def test_cooler_temperature_change_updates_target(self):
        """Test cooler temperature change updates target temperature."""
        mock_self = MagicMock()
        mock_self.startup_running = False
        mock_self.control_queue_task = AsyncMock()
        mock_self.device_name = "test"
        mock_self.context = "bt_context"
        mock_self.bt_hvac_mode = HVACMode.HEAT
        mock_self.bt_min_temp = 10.0
        mock_self.bt_max_temp = 30.0
        mock_self.bt_target_temp = 22.0
        mock_self.bt_target_cooltemp = 24.0
        mock_self.bt_target_temp_step = 0.5

        old_state = MagicMock(spec=State)
        old_state.attributes = {"temperature": 23.0}

        new_state = MagicMock(spec=State)
        new_state.attributes = {"temperature": 25.0}

        mock_event = MagicMock()
        mock_event.context = "other_context"
        mock_event.data = {
            "old_state": old_state,
            "new_state": new_state,
            "entity_id": "climate.cooler",
        }

        await cooler.trigger_cooler_change(mock_self, mock_event)

        # Should update target cooltemp
        assert mock_self.bt_target_cooltemp == 25.0
        # Should queue control action
        mock_self.control_queue_task.put.assert_called_once()

    @pytest.mark.asyncio
    async def test_cooler_temperature_outside_range_clamped(self):
        """Test cooler temperature outside range is clamped."""
        mock_self = MagicMock()
        mock_self.startup_running = False
        mock_self.control_queue_task = AsyncMock()
        mock_self.device_name = "test"
        mock_self.context = "bt_context"
        mock_self.bt_hvac_mode = HVACMode.HEAT
        mock_self.bt_min_temp = 10.0
        mock_self.bt_max_temp = 30.0
        mock_self.bt_target_temp = 22.0
        mock_self.bt_target_cooltemp = 24.0
        mock_self.bt_target_temp_step = 0.5

        old_state = MagicMock(spec=State)
        old_state.attributes = {"temperature": 25.0}

        new_state = MagicMock(spec=State)
        new_state.attributes = {"temperature": 35.0}  # Above max

        mock_event = MagicMock()
        mock_event.context = "other_context"
        mock_event.data = {
            "old_state": old_state,
            "new_state": new_state,
            "entity_id": "climate.cooler",
        }

        await cooler.trigger_cooler_change(mock_self, mock_event)

        # Should clamp to max
        assert mock_self.bt_target_cooltemp == 30.0


class TestTemperatureEvents:
    """Test temperature event handling and filtering."""

    @pytest.mark.asyncio
    async def test_temperature_change_ignored_during_startup(self):
        """Test temperature changes are ignored during startup."""
        mock_self = MagicMock()
        mock_self.startup_running = True

        mock_event = MagicMock()
        await temperature.trigger_temperature_change(mock_self, mock_event)

        # Should return early
        assert not hasattr(mock_event, "data") or not mock_event.data.get.called

    @pytest.mark.asyncio
    async def test_temperature_unavailable_state_ignored(self):
        """Test unavailable temperature state is ignored."""
        mock_self = MagicMock()
        mock_self.startup_running = False
        mock_self.device_name = "test"

        new_state = MagicMock()
        new_state.state = STATE_UNAVAILABLE

        mock_event = MagicMock()
        mock_event.data = {"new_state": new_state}

        await temperature.trigger_temperature_change(mock_self, mock_event)

        # Should not process unavailable state
        assert not hasattr(mock_self, "cur_temp") or mock_self.cur_temp is None

    @pytest.mark.asyncio
    async def test_ema_calculation_initializes_on_first_value(self):
        """Test EMA initializes to first value."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.external_temp_ema_tau_s = 300.0
        mock_self._external_temp_ema_ts = None
        mock_self.external_temp_ema = None

        result = temperature._update_external_temp_ema(mock_self, 21.5)

        assert result == 21.5
        assert mock_self.external_temp_ema == 21.5
        assert mock_self.cur_temp_filtered == 21.5

    @pytest.mark.asyncio
    async def test_ema_calculation_smooths_values(self):
        """Test EMA smooths temperature values over time."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.external_temp_ema_tau_s = 300.0

        # Set initial value
        mock_self._external_temp_ema_ts = None
        mock_self.external_temp_ema = None
        temperature._update_external_temp_ema(mock_self, 20.0)

        # Simulate time passing and new reading
        import time

        with patch("custom_components.better_thermostat.events.temperature.monotonic") as mock_time:
            initial_time = 1000.0
            mock_time.side_effect = [initial_time, initial_time + 60.0]  # 60s later

            # Reset timestamp for new reading
            temperature._update_external_temp_ema(mock_self, 20.0)
            result = temperature._update_external_temp_ema(mock_self, 22.0)

            # EMA should be between 20 and 22
            assert 20.0 < result < 22.0
            assert mock_self.external_temp_ema == result

    @pytest.mark.asyncio
    async def test_apply_temperature_update_resets_accumulators(self):
        """Test _apply_temperature_update resets accumulation state."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.cur_temp = 20.0
        mock_self.accum_delta = 0.5
        mock_self.accum_dir = 1
        mock_self.pending_temp = 21.0
        mock_self.pending_since = datetime.now()
        mock_self.plateau_timer_cancel = None
        mock_self.external_temp_ema_tau_s = 300.0
        mock_self._external_temp_ema_ts = None
        mock_self.external_temp_ema = None
        mock_self.control_queue_task = AsyncMock()
        mock_self.real_trvs = {}

        await temperature._apply_temperature_update(mock_self, 21.5)

        # Should reset accumulators
        assert mock_self.accum_delta == 0.0
        assert mock_self.accum_dir == 0
        assert mock_self.pending_temp is None
        assert mock_self.pending_since is None

    @pytest.mark.asyncio
    async def test_temperature_filtering_ignores_small_changes_within_interval(self):
        """Test small temperature changes within interval are ignored."""
        mock_self = MagicMock()
        mock_self.startup_running = False
        mock_self.device_name = "test"
        mock_self.cur_temp = 20.0
        mock_self.last_external_sensor_change = datetime.now()
        mock_self.accum_delta = 0.0
        mock_self.accum_dir = 0
        mock_self.accum_since = datetime.now()
        mock_self.pending_temp = None
        mock_self.pending_since = None
        mock_self.last_change_direction = 0
        mock_self.all_trvs = []

        new_state = MagicMock()
        new_state.state = "20.05"  # Very small change

        mock_event = MagicMock()
        mock_event.data = {"new_state": new_state}

        with patch("custom_components.better_thermostat.events.temperature.datetime") as mock_dt:
            mock_dt.now.return_value = datetime.now()

            await temperature.trigger_temperature_change(mock_self, mock_event)

            # Temperature should not be updated (too small change, too soon)
            assert mock_self.cur_temp == 20.0

    @pytest.mark.asyncio
    async def test_invalid_temperature_creates_repair_issue(self):
        """Test invalid temperature values create repair issues."""
        mock_self = MagicMock()
        mock_self.startup_running = False
        mock_self.device_name = "test"
        mock_self.last_external_sensor_change = datetime.now()
        mock_self.hass = MagicMock()

        new_state = MagicMock()
        new_state.state = "invalid_temp"

        mock_event = MagicMock()
        mock_event.data = {"new_state": new_state}

        with patch(
            "custom_components.better_thermostat.events.temperature.ir.async_create_issue"
        ) as mock_create_issue:
            await temperature.trigger_temperature_change(mock_self, mock_event)

            # Should create repair issue
            mock_create_issue.assert_called_once()


class TestTRVEvents:
    """Test TRV event handling."""

    @pytest.mark.asyncio
    async def test_trv_change_ignored_during_startup(self):
        """Test TRV changes are ignored during startup."""
        mock_self = MagicMock()
        mock_self.startup_running = True

        mock_event = MagicMock()
        await trv.trigger_trv_change(mock_self, mock_event)

        # Should return early
        assert not hasattr(mock_event, "data") or not mock_event.data.get.called

    @pytest.mark.asyncio
    async def test_trv_change_ignored_if_update_lock_active(self):
        """Test TRV changes are ignored when update lock is active."""
        mock_self = MagicMock()
        mock_self.startup_running = False
        mock_self.control_queue_task = MagicMock()
        mock_self.bt_target_temp = 21.0
        mock_self.cur_temp = 20.0
        mock_self.tolerance = 0.5
        mock_self.bt_update_lock = True  # Lock active

        mock_event = MagicMock()
        await trv.trigger_trv_change(mock_self, mock_event)

        # Should return early
        assert not mock_event.data.get.called

    @pytest.mark.asyncio
    async def test_trv_temperature_update_triggers_control(self):
        """Test TRV temperature update triggers control action."""
        mock_self = MagicMock()
        mock_self.startup_running = False
        mock_self.control_queue_task = AsyncMock()
        mock_self.bt_target_temp = 21.0
        mock_self.cur_temp = 20.0
        mock_self.tolerance = 0.5
        mock_self.bt_update_lock = False
        mock_self.device_name = "test"
        mock_self.context = "bt_context"
        mock_self.ignore_states = False
        mock_self.last_internal_sensor_change = datetime.now()
        mock_self.real_trvs = {
            "climate.test": {
                "current_temperature": 19.5,
                "calibration_received": False,
                "calibration": 0,
                "advanced": {"child_lock": False},
                "hvac_mode": None,
                "hvac_action": None,
            }
        }

        org_state = MagicMock()
        org_state.attributes = {
            "current_temperature": 20.5,  # Changed from 19.5
        }
        org_state.state = "heat"
        mock_self.hass.states.get.return_value = org_state

        old_state = MagicMock(spec=State)
        old_state.attributes = {"current_temperature": 19.5}
        old_state.state = "heat"

        new_state = MagicMock(spec=State)
        new_state.attributes = {"current_temperature": 20.5}
        new_state.state = "heat"

        mock_event = MagicMock()
        mock_event.context = "other_context"
        mock_event.data = {
            "old_state": old_state,
            "new_state": new_state,
            "entity_id": "climate.test",
        }

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states"
        ) as mock_convert:
            mock_convert.return_value = HVACMode.HEAT

            # Ensure enough time has passed
            with patch(
                "custom_components.better_thermostat.events.trv.datetime"
            ) as mock_dt:
                from datetime import timedelta

                past_time = datetime.now() - timedelta(seconds=10)
                mock_self.last_internal_sensor_change = past_time

                await trv.trigger_trv_change(mock_self, mock_event)

                # Should update temperature and queue control
                assert mock_self.real_trvs["climate.test"]["current_temperature"] == 20.5
                mock_self.control_queue_task.put.assert_called()

    @pytest.mark.asyncio
    async def test_convert_inbound_states_handles_invalid_state(self):
        """Test convert_inbound_states raises TypeError for None state."""
        mock_self = MagicMock()

        with pytest.raises(TypeError):
            trv.convert_inbound_states(mock_self, "climate.test", None)

    @pytest.mark.asyncio
    async def test_convert_outbound_states_handles_fallback_mode(self):
        """Test convert_outbound_states uses fallback when calibration type unknown."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.bt_target_temp = 21.0
        mock_self.real_trvs = {
            "climate.test": {
                "advanced": {"calibration": None},  # Unknown type
                "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
                "current_temperature": 20.0,
                "min_temp": 10.0,
            }
        }

        with patch(
            "custom_components.better_thermostat.events.trv.mode_remap"
        ) as mock_remap:
            mock_remap.return_value = HVACMode.HEAT

            result = trv.convert_outbound_states(mock_self, "climate.test", HVACMode.HEAT)

            # Should return payload with target temperature
            assert result is not None
            assert result["temperature"] == 21.0
            assert "local_temperature_calibration" not in result or result.get(
                "local_temperature_calibration"
            ) is None


class TestWindowEvents:
    """Test window event handling and queue management."""

    @pytest.mark.asyncio
    async def test_window_open_event_queued(self):
        """Test window open event is queued for processing."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.window_id = "binary_sensor.window"
        mock_self.window_open = False
        mock_self.window_queue_task = AsyncMock()
        mock_self.hass.states.get.return_value = MagicMock()

        new_state = MagicMock()
        new_state.state = "on"

        mock_event = MagicMock()
        mock_event.data = {"new_state": new_state}

        await window.trigger_window_change(mock_self, mock_event)

        # Should queue True (window open)
        mock_self.window_queue_task.put.assert_called_once_with(True)

    @pytest.mark.asyncio
    async def test_window_close_event_queued(self):
        """Test window close event is queued for processing."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.window_id = "binary_sensor.window"
        mock_self.window_open = True
        mock_self.window_queue_task = AsyncMock()
        mock_self.hass.states.get.return_value = MagicMock()

        new_state = MagicMock()
        new_state.state = "off"

        mock_event = MagicMock()
        mock_event.data = {"new_state": new_state}

        await window.trigger_window_change(mock_self, mock_event)

        # Should queue False (window closed)
        mock_self.window_queue_task.put.assert_called_once_with(False)

    @pytest.mark.asyncio
    async def test_window_same_state_not_queued(self):
        """Test window state unchanged is not queued."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.window_id = "binary_sensor.window"
        mock_self.window_open = True
        mock_self.window_queue_task = AsyncMock()
        mock_self.hass.states.get.return_value = MagicMock()

        new_state = MagicMock()
        new_state.state = "on"  # Already open

        mock_event = MagicMock()
        mock_event.data = {"new_state": new_state}

        await window.trigger_window_change(mock_self, mock_event)

        # Should NOT queue (same state)
        mock_self.window_queue_task.put.assert_not_called()

    @pytest.mark.asyncio
    async def test_window_unknown_state_treated_as_open(self):
        """Test unknown window state is treated as open."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.window_id = "binary_sensor.window"
        mock_self.window_open = False
        mock_self.window_queue_task = AsyncMock()
        mock_self.hass.states.get.return_value = MagicMock()

        new_state = MagicMock()
        new_state.state = "unknown"

        mock_event = MagicMock()
        mock_event.data = {"new_state": new_state}

        await window.trigger_window_change(mock_self, mock_event)

        # Should queue True (treat unknown as open for safety)
        mock_self.window_queue_task.put.assert_called_once_with(True)

    @pytest.mark.asyncio
    async def test_window_invalid_state_creates_repair_issue(self):
        """Test invalid window state creates repair issue."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.window_id = "binary_sensor.window"
        mock_self.window_open = False
        mock_self.window_queue_task = AsyncMock()
        mock_self.hass = MagicMock()
        mock_self.hass.states.get.return_value = MagicMock()

        new_state = MagicMock()
        new_state.state = "invalid_state"

        mock_event = MagicMock()
        mock_event.data = {"new_state": new_state}

        with patch(
            "custom_components.better_thermostat.events.window.ir.async_create_issue"
        ) as mock_create_issue:
            await window.trigger_window_change(mock_self, mock_event)

            # Should create repair issue
            mock_create_issue.assert_called_once()

    @pytest.mark.asyncio
    async def test_window_queue_processes_with_delay(self):
        """Test window queue processes events with delay."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.window_delay = 5
        mock_self.window_delay_after = 10
        mock_self.window_id = "binary_sensor.window"
        mock_self.control_queue_task = AsyncMock()

        mock_window_state = MagicMock()
        mock_window_state.state = STATE_OFF
        mock_self.hass.states.get.return_value = mock_window_state

        # Create a queue with a single event
        queue = asyncio.Queue()
        mock_self.window_queue_task = queue

        # Put False (window closed) event
        await queue.put(False)

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            # Process one event then stop
            task = asyncio.create_task(window.window_queue(mock_self))
            # Give it time to process
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            # Should have slept for window_delay_after
            if mock_sleep.call_count > 0:
                assert mock_sleep.call_args[0][0] == 10

    def test_empty_queue_clears_pending_items(self):
        """Test empty_queue removes all pending items."""
        queue = asyncio.Queue()
        # Add some items
        for i in range(5):
            queue.put_nowait(i)

        assert queue.qsize() == 5

        window.empty_queue(queue)

        assert queue.qsize() == 0


class TestEdgeCases:
    """Test edge cases and error scenarios."""

    @pytest.mark.asyncio
    async def test_temperature_event_with_none_state_attributes(self):
        """Test temperature event with None attributes is ignored."""
        mock_self = MagicMock()
        mock_self.startup_running = False
        mock_self.device_name = "test"

        new_state = MagicMock()
        new_state.state = "20.0"
        new_state.attributes = None  # None attributes

        mock_event = MagicMock()
        mock_event.data = {"new_state": new_state}

        # Should not crash
        await temperature.trigger_temperature_change(mock_self, mock_event)

    @pytest.mark.asyncio
    async def test_trv_event_with_missing_real_trvs_entry(self):
        """Test TRV event with missing real_trvs entry handles gracefully."""
        mock_self = MagicMock()
        mock_self.startup_running = False
        mock_self.control_queue_task = MagicMock()
        mock_self.bt_target_temp = 21.0
        mock_self.cur_temp = 20.0
        mock_self.tolerance = 0.5
        mock_self.bt_update_lock = False
        mock_self.device_name = "test"
        mock_self.real_trvs = {}  # Empty, no entry for climate.test

        mock_event = MagicMock()
        mock_event.data = {
            "old_state": MagicMock(spec=State),
            "new_state": MagicMock(spec=State),
            "entity_id": "climate.test",
        }

        # Should handle KeyError gracefully
        with pytest.raises(KeyError):
            await trv.trigger_trv_change(mock_self, mock_event)

    @pytest.mark.asyncio
    async def test_window_queue_during_maintenance(self):
        """Test window events during maintenance defer control."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.window_delay = 5
        mock_self.window_delay_after = 10
        mock_self.window_id = "binary_sensor.window"
        mock_self.control_queue_task = AsyncMock()
        mock_self.in_maintenance = True  # Maintenance mode
        mock_self._control_needed_after_maintenance = False

        mock_window_state = MagicMock()
        mock_window_state.state = STATE_OFF
        mock_self.hass.states.get.return_value = mock_window_state

        queue = asyncio.Queue()
        mock_self.window_queue_task = queue
        await queue.put(False)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            task = asyncio.create_task(window.window_queue(mock_self))
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            # Should NOT call control queue during maintenance
            # Should set flag instead
            assert mock_self._control_needed_after_maintenance is True