"""Test race conditions in parallel TRV control.

Verifies that parallel execution of control_trv() for grouped TRVs
doesn't cause race conditions due to incomplete lock coverage.

The _temp_lock must protect all critical operations including
set_valve(), set_hvac_mode(), set_offset(), and set_temperature()
to prevent shared state corruption when multiple TRVs are controlled
concurrently via asyncio.gather().
"""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.climate.const import HVACMode
import pytest

from custom_components.better_thermostat.utils.const import (
    CalibrationMode,
    CalibrationType,
)
from custom_components.better_thermostat.utils.controlling import control_trv


def _close_coro(coro):
    """Close coroutine to avoid RuntimeWarning."""
    coro.close()
    return Mock()


@pytest.mark.asyncio
async def test_parallel_trv_control_no_race_condition():
    """Test that parallel control_trv() calls don't cause race conditions.

    Scenario: 2 grouped TRVs controlled simultaneously.
    Expected: Both TRVs complete successfully without state corruption.
    """
    # Mock TRV states - force mismatch so set_hvac_mode and set_temperature are called
    mock_state_trv1 = Mock()
    mock_state_trv1.state = HVACMode.OFF  # Mismatch: BT wants HEAT
    mock_state_trv1.attributes = {
        "temperature": 18.0,  # Mismatch: BT wants 22.0
        "current_temperature": 20.0,
        "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
    }

    mock_state_trv2 = Mock()
    mock_state_trv2.state = HVACMode.OFF  # Mismatch: BT wants HEAT
    mock_state_trv2.attributes = {
        "temperature": 18.0,  # Mismatch: BT wants 22.0
        "current_temperature": 20.0,
        "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
    }

    mock_hass = Mock()
    mock_hass.states.get.side_effect = lambda entity_id: (
        mock_state_trv1 if entity_id == "climate.trv1" else mock_state_trv2
    )

    # Shared BetterThermostat instance (like in grouped TRVs scenario)
    mock_self = Mock()
    mock_self.hass = mock_hass
    mock_self.device_name = "test_grouped_thermostat"
    mock_self._temp_lock = asyncio.Lock()
    mock_self.calculate_heating_power = AsyncMock()
    mock_self.task_manager = Mock(create_task=Mock(side_effect=_close_coro))
    mock_self.cur_temp = 20.0
    mock_self.bt_target_temp = 22.0
    mock_self.bt_hvac_mode = HVACMode.HEAT
    mock_self.window_open = False
    mock_self.call_for_heat = True

    # Two TRVs sharing the same Better Thermostat instance
    # Configure to force set_hvac_mode and set_temperature calls
    mock_self.real_trvs = {
        "climate.trv1": {
            "ignore_trv_states": False,
            "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
            "min_temp": 5.0,
            "max_temp": 30.0,
            "temperature": 18.0,  # Different from target to trigger set_temperature
            "last_temperature": 18.0,
            "last_hvac_mode": HVACMode.OFF,  # Different from target to trigger set_hvac_mode
            "system_mode_received": True,
            "target_temp_received": True,
            "calibration_received": False,
            "model_quirks": Mock(override_set_hvac_mode=AsyncMock(return_value=False)),
            "advanced": {
                "calibration_mode": CalibrationMode.MPC_CALIBRATION,
                "calibration": CalibrationType.TARGET_TEMP_BASED,
            },
        },
        "climate.trv2": {
            "ignore_trv_states": False,
            "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
            "min_temp": 5.0,
            "max_temp": 30.0,
            "temperature": 18.0,  # Different from target to trigger set_temperature
            "last_temperature": 18.0,
            "last_hvac_mode": HVACMode.OFF,  # Different from target to trigger set_hvac_mode
            "system_mode_received": True,
            "target_temp_received": True,
            "calibration_received": False,
            "model_quirks": Mock(override_set_hvac_mode=AsyncMock(return_value=False)),
            "advanced": {
                "calibration_mode": CalibrationMode.MPC_CALIBRATION,
                "calibration": CalibrationType.TARGET_TEMP_BASED,
            },
        },
    }

    # Track concurrent execution
    execution_log = []
    lock_acquired_count = 0

    original_lock_acquire = mock_self._temp_lock.acquire

    async def tracked_acquire(*args, **kwargs):
        """Track lock acquisition events for race condition detection."""
        nonlocal lock_acquired_count
        lock_acquired_count += 1
        execution_log.append(f"lock_acquire_{lock_acquired_count}")
        result = await original_lock_acquire(*args, **kwargs)
        execution_log.append(f"lock_acquired_{lock_acquired_count}")
        return result

    mock_self._temp_lock.acquire = tracked_acquire

    with (
        patch(
            "custom_components.better_thermostat.utils.controlling.convert_outbound_states"
        ) as mock_convert,
        patch(
            "custom_components.better_thermostat.utils.controlling.set_valve"
        ) as mock_set_valve,
        patch(
            "custom_components.better_thermostat.utils.controlling.set_hvac_mode"
        ) as mock_set_hvac_mode,
        patch(
            "custom_components.better_thermostat.utils.controlling.set_offset"
        ) as mock_set_offset,
        patch(
            "custom_components.better_thermostat.utils.controlling.set_temperature"
        ) as mock_set_temp,
        patch(
            "custom_components.better_thermostat.utils.controlling.override_set_hvac_mode",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "custom_components.better_thermostat.utils.controlling.get_current_offset",
            new=AsyncMock(return_value=0.0),
        ),
    ):
        mock_convert.return_value = {
            "temperature": 22.0,
            "local_temperature_calibration": 0.0,
            "system_mode": HVACMode.HEAT,
        }

        # Simulate network delay in critical operations
        async def delayed_set_valve(*args, **kwargs):
            """Mock set_valve with delay to detect interleaving."""
            execution_log.append(f"set_valve_start_{args[1]}")
            await asyncio.sleep(0.01)  # Simulate network delay
            execution_log.append(f"set_valve_end_{args[1]}")

        async def delayed_set_hvac_mode(*args, **kwargs):
            """Mock set_hvac_mode with delay to detect interleaving."""
            execution_log.append(f"set_hvac_mode_start_{args[1]}")
            await asyncio.sleep(0.01)
            execution_log.append(f"set_hvac_mode_end_{args[1]}")

        async def delayed_set_offset(*args, **kwargs):
            """Mock set_offset with delay to detect interleaving."""
            execution_log.append(f"set_offset_start_{args[1]}")
            await asyncio.sleep(0.01)
            execution_log.append(f"set_offset_end_{args[1]}")

        async def delayed_set_temp(*args, **kwargs):
            """Mock set_temperature with delay to detect interleaving."""
            execution_log.append(f"set_temp_start_{args[1]}")
            await asyncio.sleep(0.01)
            execution_log.append(f"set_temp_end_{args[1]}")

        mock_set_valve.side_effect = delayed_set_valve
        mock_set_hvac_mode.side_effect = delayed_set_hvac_mode
        mock_set_offset.side_effect = delayed_set_offset
        mock_set_temp.side_effect = delayed_set_temp

        # Run both TRVs in parallel (like control_queue does)
        results = await asyncio.gather(
            control_trv(mock_self, "climate.trv1"),
            control_trv(mock_self, "climate.trv2"),
            return_exceptions=True,
        )

        # Both should succeed
        assert results[0] is True
        assert results[1] is True

        # At least one operation should have been called
        total_calls = (
            mock_set_temp.call_count
            + mock_set_hvac_mode.call_count
            + mock_set_offset.call_count
            + mock_set_valve.call_count
        )
        assert total_calls >= 2, (
            f"Expected at least 2 operation calls, got {total_calls}"
        )

        # CRITICAL TEST: Operations should NOT interleave
        # If lock coverage is incomplete, we'll see interleaving like:
        #   set_temp_start_trv1
        #   set_temp_start_trv2  <- BAD! Started before trv1 finished
        #   set_temp_end_trv1
        #   set_temp_end_trv2
        #
        # With proper lock coverage, we should see:
        #   set_temp_start_trv1
        #   set_temp_end_trv1
        #   set_temp_start_trv2  <- GOOD! Started after trv1 finished
        #   set_temp_end_trv2

        print("\nExecution log:")
        for event in execution_log:
            print(f"  {event}")

        # Check for interleaving of ANY operation (not just set_temp)
        # Look for set_hvac_mode since that's what actually gets called
        operation_events = [
            e for e in execution_log if "set_hvac_mode" in e or "set_temp" in e
        ]

        if len(operation_events) >= 4:
            # Find all starts and ends
            starts = [e for e in operation_events if "start" in e]
            ends = [e for e in operation_events if "end" in e]

            if len(starts) >= 2 and len(ends) >= 2:
                # Check for interleaving: any end before all starts
                # This would indicate operations running concurrently
                for i, event in enumerate(operation_events):
                    if "end" in event:
                        # Find how many starts came before this end
                        starts_before = sum(
                            1 for e in operation_events[:i] if "start" in e
                        )
                        ends_before = sum(1 for e in operation_events[:i] if "end" in e)

                        # If multiple operations started before this one ended, we have interleaving
                        if starts_before > ends_before + 1:
                            raise AssertionError(
                                f"Race condition detected: {starts_before} operations started "
                                f"before this one ended.\n"
                                f"  Event: {event}\n"
                                f"  Events before: {operation_events[:i]}"
                            )


@pytest.mark.asyncio
async def test_shared_state_corruption_in_parallel_execution():
    """Test that shared state doesn't get corrupted during parallel execution."""
    mock_state = Mock()
    mock_state.state = HVACMode.HEAT
    mock_state.attributes = {
        "temperature": 22.0,
        "current_temperature": 20.0,
        "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
    }

    mock_hass = Mock()
    mock_hass.states.get.return_value = mock_state

    mock_self = Mock()
    mock_self.hass = mock_hass
    mock_self.device_name = "test_thermostat"
    mock_self._temp_lock = asyncio.Lock()
    mock_self.calculate_heating_power = AsyncMock()
    mock_self.task_manager = Mock(create_task=Mock(side_effect=_close_coro))
    mock_self.cur_temp = 20.0
    mock_self.bt_target_temp = 22.0
    mock_self.bt_hvac_mode = HVACMode.HEAT
    mock_self.window_open = False
    mock_self.call_for_heat = True

    mock_self.real_trvs = {
        "climate.trv1": {
            "ignore_trv_states": False,
            "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
            "min_temp": 5.0,
            "max_temp": 30.0,
            "temperature": 22.0,
            "last_hvac_mode": HVACMode.HEAT,
            "system_mode_received": False,
            "target_temp_received": False,
            "calibration_received": False,
            "model_quirks": Mock(override_set_hvac_mode=AsyncMock(return_value=False)),
            "advanced": {
                "calibration_mode": CalibrationMode.MPC_CALIBRATION,
                "calibration": CalibrationType.TARGET_TEMP_BASED,
            },
        },
        "climate.trv2": {
            "ignore_trv_states": False,
            "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
            "min_temp": 5.0,
            "max_temp": 30.0,
            "temperature": 22.0,
            "last_hvac_mode": HVACMode.HEAT,
            "system_mode_received": False,
            "target_temp_received": False,
            "calibration_received": False,
            "model_quirks": Mock(override_set_hvac_mode=AsyncMock(return_value=False)),
            "advanced": {
                "calibration_mode": CalibrationMode.MPC_CALIBRATION,
                "calibration": CalibrationType.TARGET_TEMP_BASED,
            },
        },
    }

    with (
        patch(
            "custom_components.better_thermostat.utils.controlling.convert_outbound_states"
        ) as mock_convert,
        patch("custom_components.better_thermostat.utils.controlling.set_temperature"),
        patch(
            "custom_components.better_thermostat.utils.controlling.override_set_hvac_mode",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "custom_components.better_thermostat.utils.controlling.get_current_offset",
            new=AsyncMock(return_value=0.0),
        ),
    ):
        mock_convert.return_value = {
            "temperature": 22.0,
            "local_temperature_calibration": 0.0,
            "system_mode": HVACMode.HEAT,
        }

        # Run both TRVs in parallel
        results = await asyncio.gather(
            control_trv(mock_self, "climate.trv1"),
            control_trv(mock_self, "climate.trv2"),
            return_exceptions=True,
        )

        # Both should complete
        assert results[0] is True
        assert results[1] is True

        # Both ignore_trv_states should be reset to False
        assert mock_self.real_trvs["climate.trv1"]["ignore_trv_states"] is False
        assert mock_self.real_trvs["climate.trv2"]["ignore_trv_states"] is False

        # With proper lock coverage, state should not be corrupted
        # Verifies state consistency after parallel execution


@pytest.mark.asyncio
async def test_lock_protects_critical_sections():
    """Test that lock actually protects all critical operations."""
    mock_state = Mock()
    mock_state.state = HVACMode.HEAT
    mock_state.attributes = {
        "temperature": 22.0,
        "current_temperature": 20.0,
        "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
    }

    mock_hass = Mock()
    mock_hass.states.get.return_value = mock_state

    mock_self = Mock()
    mock_self.hass = mock_hass
    mock_self.device_name = "test_thermostat"
    mock_self._temp_lock = asyncio.Lock()
    mock_self.calculate_heating_power = AsyncMock()
    mock_self.task_manager = Mock(create_task=Mock(side_effect=_close_coro))
    mock_self.cur_temp = 20.0
    mock_self.bt_target_temp = 22.0
    mock_self.bt_hvac_mode = HVACMode.HEAT
    mock_self.window_open = False
    mock_self.call_for_heat = True
    mock_self.real_trvs = {
        "climate.trv1": {
            "ignore_trv_states": False,
            "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
            "min_temp": 5.0,
            "max_temp": 30.0,
            "temperature": 22.0,
            "last_hvac_mode": HVACMode.HEAT,
            "system_mode_received": False,
            "target_temp_received": False,
            "calibration_received": False,
            "model_quirks": Mock(override_set_hvac_mode=AsyncMock(return_value=False)),
            "advanced": {
                "calibration_mode": CalibrationMode.MPC_CALIBRATION,
                "calibration": CalibrationType.TARGET_TEMP_BASED,
            },
        }
    }

    # Track lock state during critical operations
    lock_state_during_operations = []

    with (
        patch(
            "custom_components.better_thermostat.utils.controlling.convert_outbound_states"
        ) as mock_convert,
        patch(
            "custom_components.better_thermostat.utils.controlling.set_valve"
        ) as mock_set_valve,
        patch(
            "custom_components.better_thermostat.utils.controlling.set_hvac_mode"
        ) as mock_set_hvac_mode,
        patch(
            "custom_components.better_thermostat.utils.controlling.set_offset"
        ) as mock_set_offset,
        patch(
            "custom_components.better_thermostat.utils.controlling.set_temperature"
        ) as mock_set_temp,
        patch(
            "custom_components.better_thermostat.utils.controlling.override_set_hvac_mode",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "custom_components.better_thermostat.utils.controlling.get_current_offset",
            new=AsyncMock(return_value=0.0),
        ),
    ):
        mock_convert.return_value = {
            "temperature": 22.0,
            "local_temperature_calibration": 0.0,
            "system_mode": HVACMode.HEAT,
        }

        async def check_lock_on_set_valve(*args, **kwargs):
            """Record lock state when set_valve is called."""
            lock_state_during_operations.append(
                ("set_valve", mock_self._temp_lock.locked())
            )

        async def check_lock_on_set_hvac_mode(*args, **kwargs):
            """Record lock state when set_hvac_mode is called."""
            lock_state_during_operations.append(
                ("set_hvac_mode", mock_self._temp_lock.locked())
            )

        async def check_lock_on_set_offset(*args, **kwargs):
            """Record lock state when set_offset is called."""
            lock_state_during_operations.append(
                ("set_offset", mock_self._temp_lock.locked())
            )

        async def check_lock_on_set_temp(*args, **kwargs):
            """Record lock state when set_temperature is called."""
            lock_state_during_operations.append(
                ("set_temperature", mock_self._temp_lock.locked())
            )

        mock_set_valve.side_effect = check_lock_on_set_valve
        mock_set_hvac_mode.side_effect = check_lock_on_set_hvac_mode
        mock_set_offset.side_effect = check_lock_on_set_offset
        mock_set_temp.side_effect = check_lock_on_set_temp

        result = await control_trv(mock_self, "climate.trv1")

        assert result is True

        # All operations must run while lock is held
        print("\nLock state during critical operations:")
        for operation, locked in lock_state_during_operations:
            print(f"  {operation}: locked={locked}")

        for operation, locked in lock_state_during_operations:
            assert locked is True, (
                f"Operation {operation} ran WITHOUT lock protection! "
                f"This causes race conditions in parallel execution."
            )
