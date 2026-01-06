"""Tests for grouped TRV calibration synchronization.

Issue #1410: When controlling multiple TRVs as a group with offset calibration,
not all TRVs receive updated calibration simultaneously. One or more TRVs
may have their calibration updates delayed by 30+ minutes.

Root cause: When calibration is sent to a TRV during a control cycle, the
calibration_received flag is set to False. The flag should be set back to True
when the TRV acknowledges the calibration via a state change event. However,
during control cycles, ignore_states=True, so the state change event is ignored.
This leaves calibration_received stuck at False, blocking future calibration
updates for that TRV.

The fix: Before attempting to send calibration, check if the TRV's current
calibration already matches the target value. If so, reset calibration_received
to True. This unblocks the TRV for future calibration updates.
"""

import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

import pytest


@pytest.fixture
def anyio_backend():
    """Configure anyio to use asyncio backend for async tests."""
    return "asyncio"


@pytest.fixture
def mock_hass():
    """Create a mock Home Assistant instance."""
    hass = MagicMock()
    hass.states = MagicMock()
    return hass


@pytest.fixture
def mock_bt_instance(mock_hass):
    """Create a mock BetterThermostat instance for grouped TRV testing."""
    bt = MagicMock()
    bt.hass = mock_hass
    bt.device_name = "Test Thermostat"
    bt.bt_hvac_mode = "heat"
    bt.bt_target_temp = 21.0
    bt.cur_temp = 20.0
    bt.window_open = False
    bt.call_for_heat = True
    bt.tolerance = 0.5
    bt._temp_lock = asyncio.Lock()
    bt.calculate_heating_power = AsyncMock()

    # Setup three TRVs in a group
    bt.real_trvs = {
        "climate.trv_1": {
            "calibration_received": True,
            "last_calibration": 2.0,
            "current_temperature": 20.0,
            "hvac_modes": ["heat", "off"],
            "min_temp": 5.0,
            "max_temp": 30.0,
            "ignore_trv_states": False,
            "advanced": {
                "calibration": 0,  # LOCAL_BASED
                "calibration_mode": 0,  # DEFAULT
            },
        },
        "climate.trv_2": {
            "calibration_received": True,
            "last_calibration": 2.0,
            "current_temperature": 20.0,
            "hvac_modes": ["heat", "off"],
            "min_temp": 5.0,
            "max_temp": 30.0,
            "ignore_trv_states": False,
            "advanced": {"calibration": 0, "calibration_mode": 0},
        },
        "climate.trv_3": {
            "calibration_received": False,  # Stuck at False!
            "last_calibration": 2.0,
            "current_temperature": 20.0,
            "hvac_modes": ["heat", "off"],
            "min_temp": 5.0,
            "max_temp": 30.0,
            "ignore_trv_states": False,
            "advanced": {"calibration": 0, "calibration_mode": 0},
        },
    }
    return bt


class TestCalibrationReceivedReset:
    """Tests for calibration_received flag reset logic."""

    @pytest.mark.anyio
    async def test_calibration_received_reset_when_current_matches_target(
        self, mock_bt_instance
    ):
        """Test that calibration_received resets when TRV calibration matches target.

        Scenario:
        - TRV has calibration_received = False (stuck from previous cycle)
        - TRV's current calibration (2.0) matches target calibration (2.0)
        - The flag should be reset to True
        """
        from custom_components.better_thermostat.utils.controlling import control_trv

        entity_id = "climate.trv_3"

        # Setup mock TRV state
        mock_trv_state = MagicMock()
        mock_trv_state.state = "heat"
        mock_trv_state.attributes = {"temperature": 21.0}
        mock_bt_instance.hass.states.get.return_value = mock_trv_state

        # Mock the adapter functions
        with (
            patch(
                "custom_components.better_thermostat.utils.controlling.get_current_offset",
                new_callable=AsyncMock,
            ) as mock_get_offset,
            patch(
                "custom_components.better_thermostat.utils.controlling.convert_outbound_states"
            ) as mock_convert,
            patch(
                "custom_components.better_thermostat.utils.controlling.set_offset",
                new_callable=AsyncMock,
            ) as mock_set_offset,
            patch(
                "custom_components.better_thermostat.utils.controlling.set_temperature",
                new_callable=AsyncMock,
            ),
            patch(
                "custom_components.better_thermostat.utils.controlling.set_hvac_mode",
                new_callable=AsyncMock,
            ),
            patch(
                "custom_components.better_thermostat.utils.controlling.set_valve",
                new_callable=AsyncMock,
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            # Current calibration matches what we want to send
            mock_get_offset.return_value = 2.0
            mock_convert.return_value = {
                "temperature": 21.0,
                "local_temperature_calibration": 2.0,  # Same as current!
                "local_temperature": 20.0,
                "system_mode": "heat",
            }

            # Before: calibration_received is False
            assert (
                mock_bt_instance.real_trvs[entity_id]["calibration_received"] is False
            )

            await control_trv(mock_bt_instance, entity_id)

            # After: calibration_received should be True
            assert mock_bt_instance.real_trvs[entity_id]["calibration_received"] is True

            # set_offset should NOT be called (calibration already correct)
            mock_set_offset.assert_not_called()

    @pytest.mark.anyio
    async def test_calibration_received_stays_false_when_mismatch(
        self, mock_bt_instance
    ):
        """Test that calibration_received stays False when calibration differs.

        Scenario:
        - TRV has calibration_received = False
        - TRV's current calibration (2.0) differs from target (3.0)
        - The flag should stay False (TRV hasn't acknowledged yet)
        - No new calibration should be sent (blocked by False flag)
        """
        from custom_components.better_thermostat.utils.controlling import control_trv

        entity_id = "climate.trv_3"

        mock_trv_state = MagicMock()
        mock_trv_state.state = "heat"
        mock_trv_state.attributes = {"temperature": 21.0}
        mock_bt_instance.hass.states.get.return_value = mock_trv_state

        with (
            patch(
                "custom_components.better_thermostat.utils.controlling.get_current_offset",
                new_callable=AsyncMock,
            ) as mock_get_offset,
            patch(
                "custom_components.better_thermostat.utils.controlling.convert_outbound_states"
            ) as mock_convert,
            patch(
                "custom_components.better_thermostat.utils.controlling.set_offset",
                new_callable=AsyncMock,
            ) as mock_set_offset,
            patch(
                "custom_components.better_thermostat.utils.controlling.set_temperature",
                new_callable=AsyncMock,
            ),
            patch(
                "custom_components.better_thermostat.utils.controlling.set_hvac_mode",
                new_callable=AsyncMock,
            ),
            patch(
                "custom_components.better_thermostat.utils.controlling.set_valve",
                new_callable=AsyncMock,
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            # Current calibration differs from target
            mock_get_offset.return_value = 2.0
            mock_convert.return_value = {
                "temperature": 21.0,
                "local_temperature_calibration": 3.0,  # Different from current!
                "local_temperature": 20.0,
                "system_mode": "heat",
            }

            assert (
                mock_bt_instance.real_trvs[entity_id]["calibration_received"] is False
            )

            await control_trv(mock_bt_instance, entity_id)

            # Still False - we haven't received acknowledgment
            assert (
                mock_bt_instance.real_trvs[entity_id]["calibration_received"] is False
            )

            # set_offset should NOT be called (blocked by False flag)
            mock_set_offset.assert_not_called()

    @pytest.mark.anyio
    async def test_calibration_sent_when_received_true_and_differs(
        self, mock_bt_instance
    ):
        """Test that calibration is sent when flag is True and values differ.

        Scenario:
        - TRV has calibration_received = True
        - TRV's current calibration (2.0) differs from target (3.0)
        - New calibration should be sent
        - Flag should become False after sending
        """
        from custom_components.better_thermostat.utils.controlling import control_trv

        entity_id = "climate.trv_1"  # This one has calibration_received = True

        mock_trv_state = MagicMock()
        mock_trv_state.state = "heat"
        mock_trv_state.attributes = {"temperature": 21.0}
        mock_bt_instance.hass.states.get.return_value = mock_trv_state

        with (
            patch(
                "custom_components.better_thermostat.utils.controlling.get_current_offset",
                new_callable=AsyncMock,
            ) as mock_get_offset,
            patch(
                "custom_components.better_thermostat.utils.controlling.convert_outbound_states"
            ) as mock_convert,
            patch(
                "custom_components.better_thermostat.utils.controlling.set_offset",
                new_callable=AsyncMock,
            ) as mock_set_offset,
            patch(
                "custom_components.better_thermostat.utils.controlling.set_temperature",
                new_callable=AsyncMock,
            ),
            patch(
                "custom_components.better_thermostat.utils.controlling.set_hvac_mode",
                new_callable=AsyncMock,
            ),
            patch(
                "custom_components.better_thermostat.utils.controlling.set_valve",
                new_callable=AsyncMock,
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_get_offset.return_value = 2.0
            mock_convert.return_value = {
                "temperature": 21.0,
                "local_temperature_calibration": 3.0,  # Different!
                "local_temperature": 20.0,
                "system_mode": "heat",
            }

            assert mock_bt_instance.real_trvs[entity_id]["calibration_received"] is True

            await control_trv(mock_bt_instance, entity_id)

            # set_offset should be called with new calibration
            mock_set_offset.assert_called_once_with(mock_bt_instance, entity_id, 3.0)

            # Flag should now be False (waiting for acknowledgment)
            assert (
                mock_bt_instance.real_trvs[entity_id]["calibration_received"] is False
            )

    @pytest.mark.anyio
    async def test_calibration_tolerance_within_half_degree(self, mock_bt_instance):
        """Test that calibration within 0.5 degree tolerance is considered matching.

        Scenario:
        - TRV has calibration_received = False
        - Current calibration is 2.3, target is 2.0
        - Difference (0.3) is within tolerance (0.5)
        - Flag should be reset to True
        """
        from custom_components.better_thermostat.utils.controlling import control_trv

        entity_id = "climate.trv_3"

        mock_trv_state = MagicMock()
        mock_trv_state.state = "heat"
        mock_trv_state.attributes = {"temperature": 21.0}
        mock_bt_instance.hass.states.get.return_value = mock_trv_state

        with (
            patch(
                "custom_components.better_thermostat.utils.controlling.get_current_offset",
                new_callable=AsyncMock,
            ) as mock_get_offset,
            patch(
                "custom_components.better_thermostat.utils.controlling.convert_outbound_states"
            ) as mock_convert,
            patch(
                "custom_components.better_thermostat.utils.controlling.set_offset",
                new_callable=AsyncMock,
            ),
            patch(
                "custom_components.better_thermostat.utils.controlling.set_temperature",
                new_callable=AsyncMock,
            ),
            patch(
                "custom_components.better_thermostat.utils.controlling.set_hvac_mode",
                new_callable=AsyncMock,
            ),
            patch(
                "custom_components.better_thermostat.utils.controlling.set_valve",
                new_callable=AsyncMock,
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            # Current is 2.3, target is 2.0 - within 0.5 tolerance
            mock_get_offset.return_value = 2.3
            mock_convert.return_value = {
                "temperature": 21.0,
                "local_temperature_calibration": 2.0,
                "local_temperature": 20.0,
                "system_mode": "heat",
            }

            assert (
                mock_bt_instance.real_trvs[entity_id]["calibration_received"] is False
            )

            await control_trv(mock_bt_instance, entity_id)

            # Should be reset to True (within tolerance)
            assert mock_bt_instance.real_trvs[entity_id]["calibration_received"] is True

    @pytest.mark.anyio
    async def test_calibration_tolerance_outside_half_degree(self, mock_bt_instance):
        """Test that calibration outside 0.5 degree tolerance is not matching.

        Scenario:
        - TRV has calibration_received = False
        - Current calibration is 2.6, target is 2.0
        - Difference (0.6) is outside tolerance (0.5)
        - Flag should stay False
        """
        from custom_components.better_thermostat.utils.controlling import control_trv

        entity_id = "climate.trv_3"

        mock_trv_state = MagicMock()
        mock_trv_state.state = "heat"
        mock_trv_state.attributes = {"temperature": 21.0}
        mock_bt_instance.hass.states.get.return_value = mock_trv_state

        with (
            patch(
                "custom_components.better_thermostat.utils.controlling.get_current_offset",
                new_callable=AsyncMock,
            ) as mock_get_offset,
            patch(
                "custom_components.better_thermostat.utils.controlling.convert_outbound_states"
            ) as mock_convert,
            patch(
                "custom_components.better_thermostat.utils.controlling.set_offset",
                new_callable=AsyncMock,
            ),
            patch(
                "custom_components.better_thermostat.utils.controlling.set_temperature",
                new_callable=AsyncMock,
            ),
            patch(
                "custom_components.better_thermostat.utils.controlling.set_hvac_mode",
                new_callable=AsyncMock,
            ),
            patch(
                "custom_components.better_thermostat.utils.controlling.set_valve",
                new_callable=AsyncMock,
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            # Current is 2.6, target is 2.0 - outside 0.5 tolerance
            mock_get_offset.return_value = 2.6
            mock_convert.return_value = {
                "temperature": 21.0,
                "local_temperature_calibration": 2.0,
                "local_temperature": 20.0,
                "system_mode": "heat",
            }

            assert (
                mock_bt_instance.real_trvs[entity_id]["calibration_received"] is False
            )

            await control_trv(mock_bt_instance, entity_id)

            # Should stay False (outside tolerance)
            assert (
                mock_bt_instance.real_trvs[entity_id]["calibration_received"] is False
            )
