"""Tests for helper functions in utils/controlling.py.

Tests for:
- check_system_mode()
- check_target_temperature()
- _get_valve_control()

The window suppression that used to live in handle_window_open() is now
decided by the core kernel (see tests/unit/test_core_decide.py) and
applied in control_trv (see test_control_trv.py).
"""

import asyncio
from unittest.mock import MagicMock, Mock

from homeassistant.components.climate.const import HVACMode
import pytest

from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.const import (
    CalibrationMode,
    CalibrationType,
)
from custom_components.better_thermostat.utils.controlling import (
    _get_valve_control,
    check_system_mode,
    check_target_temperature,
)
from tests.factories import make_snapshot


def _boost_snapshot():
    """Snapshot of an active boost: preset set, room below target."""
    return make_snapshot(preset_mode="boost", room_temp=19.0, target_temp=22.0)


class TestCheckSystemMode:
    """Test check_system_mode function."""

    @pytest.mark.asyncio
    async def test_mode_matches_immediately(self):
        """Test when mode matches immediately."""
        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.real_trvs = {
            "climate.trv1": Trv.from_legacy_dict(
                "climate.trv1",
                {
                    "hvac_mode": HVACMode.HEAT,
                    "last_hvac_mode": HVACMode.HEAT,
                    "system_mode_received": False,
                },
            )
        }

        result = await check_system_mode(mock_self, "climate.trv1")

        assert result is True
        assert mock_self.real_trvs["climate.trv1"].system_mode_received is True

    @pytest.mark.asyncio
    async def test_mode_matches_after_delay(self):
        """Test when mode matches after a short delay."""
        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.real_trvs = {
            "climate.trv1": Trv.from_legacy_dict(
                "climate.trv1",
                {
                    "hvac_mode": HVACMode.OFF,
                    "last_hvac_mode": HVACMode.HEAT,
                    "system_mode_received": False,
                },
            )
        }

        # Simulate mode change after 0.5 seconds
        async def update_mode():
            await asyncio.sleep(0.1)
            mock_self.real_trvs["climate.trv1"].hvac_mode = HVACMode.HEAT

        update_task = asyncio.create_task(update_mode())

        result = await check_system_mode(mock_self, "climate.trv1")

        await update_task
        assert result is True
        assert mock_self.real_trvs["climate.trv1"].system_mode_received is True

    @pytest.mark.asyncio
    async def test_timeout_after_360_seconds(self):
        """Test timeout after 360 seconds.

        Note: We use a shorter timeout for testing by mocking sleep.
        """
        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.real_trvs = {
            "climate.trv1": Trv.from_legacy_dict(
                "climate.trv1",
                {
                    "hvac_mode": HVACMode.OFF,
                    "last_hvac_mode": HVACMode.HEAT,
                    "system_mode_received": False,
                },
            )
        }

        # Track sleep calls
        sleep_count = 0
        original_sleep = asyncio.sleep

        async def mock_sleep(duration):
            nonlocal sleep_count
            if duration == 1:
                sleep_count += 1
                # Simulate 361 sleep calls quickly
                if sleep_count > 360:
                    return
            await original_sleep(0.001)  # Actually sleep very briefly

        # Patch asyncio.sleep
        import custom_components.better_thermostat.utils.controlling as controlling_module

        original_sleep_func = controlling_module.asyncio.sleep
        controlling_module.asyncio.sleep = mock_sleep

        try:
            result = await check_system_mode(mock_self, "climate.trv1")

            assert result is True
            # Flag should still be set to True after timeout
            assert mock_self.real_trvs["climate.trv1"].system_mode_received is True
            # Mode should not have changed
            assert mock_self.real_trvs["climate.trv1"].hvac_mode == HVACMode.OFF
        finally:
            controlling_module.asyncio.sleep = original_sleep_func

    @pytest.mark.asyncio
    async def test_system_mode_received_flag_set(self):
        """Test that system_mode_received flag is always set to True."""
        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.real_trvs = {
            "climate.trv1": Trv.from_legacy_dict(
                "climate.trv1",
                {
                    "hvac_mode": HVACMode.HEAT,
                    "last_hvac_mode": HVACMode.HEAT,
                    "system_mode_received": False,
                },
            )
        }

        await check_system_mode(mock_self, "climate.trv1")

        assert mock_self.real_trvs["climate.trv1"].system_mode_received is True


# ---------------------------------------------------------------------------
# check_target_temperature
# ---------------------------------------------------------------------------


class TestCheckTargetTemperature:
    """Test check_target_temperature function."""

    @pytest.mark.asyncio
    async def test_temperature_matches_immediately(self):
        """Test when temperature matches immediately."""
        mock_state = Mock()
        mock_state.attributes = {"temperature": 21.0}

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.hass = mock_hass
        mock_self.real_trvs = {
            "climate.trv1": Trv.from_legacy_dict(
                "climate.trv1",
                {"last_temperature": 21.0, "target_temp_received": False},
            )
        }

        result = await check_target_temperature(mock_self, "climate.trv1")

        assert result is True
        assert mock_self.real_trvs["climate.trv1"].target_temp_received is True

    @pytest.mark.asyncio
    async def test_temperature_is_none(self):
        """Test when current temperature is None."""
        mock_state = Mock()
        mock_state.attributes = {"temperature": None}

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.hass = mock_hass
        mock_self.real_trvs = {
            "climate.trv1": Trv.from_legacy_dict(
                "climate.trv1",
                {"last_temperature": 21.0, "target_temp_received": False},
            )
        }

        result = await check_target_temperature(mock_self, "climate.trv1")

        assert result is True
        assert mock_self.real_trvs["climate.trv1"].target_temp_received is True

    @pytest.mark.asyncio
    async def test_temperature_matches_after_delay(self):
        """Test when temperature matches after a delay."""
        mock_state = Mock()
        mock_state.attributes = {"temperature": 20.0}

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.hass = mock_hass
        mock_self.real_trvs = {
            "climate.trv1": Trv.from_legacy_dict(
                "climate.trv1",
                {"last_temperature": 21.0, "target_temp_received": False},
            )
        }

        # Simulate temperature change after 0.1 seconds
        async def update_temp():
            await asyncio.sleep(0.1)
            mock_state.attributes["temperature"] = 21.0

        update_task = asyncio.create_task(update_temp())

        result = await check_target_temperature(mock_self, "climate.trv1")

        await update_task
        assert result is True
        assert mock_self.real_trvs["climate.trv1"].target_temp_received is True

    @pytest.mark.asyncio
    async def test_timeout_after_360_seconds(self):
        """Test timeout after 360 seconds."""
        mock_state = Mock()
        mock_state.attributes = {"temperature": 20.0}

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.hass = mock_hass
        mock_self.real_trvs = {
            "climate.trv1": Trv.from_legacy_dict(
                "climate.trv1",
                {"last_temperature": 21.0, "target_temp_received": False},
            )
        }

        # Track sleep calls
        sleep_count = 0
        original_sleep = asyncio.sleep

        async def mock_sleep(duration):
            nonlocal sleep_count
            if duration == 1:
                sleep_count += 1
                if sleep_count > 360:
                    return
            await original_sleep(0.001)

        import custom_components.better_thermostat.utils.controlling as controlling_module

        original_sleep_func = controlling_module.asyncio.sleep
        controlling_module.asyncio.sleep = mock_sleep

        try:
            result = await check_target_temperature(mock_self, "climate.trv1")

            assert result is True
            assert mock_self.real_trvs["climate.trv1"].target_temp_received is True
        finally:
            controlling_module.asyncio.sleep = original_sleep_func

    @pytest.mark.asyncio
    async def test_convert_to_float_called(self):
        """Test that convert_to_float is used for temperature conversion."""
        mock_state = Mock()
        mock_state.attributes = {"temperature": "21.0"}  # String value

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.hass = mock_hass
        mock_self.real_trvs = {
            "climate.trv1": Trv.from_legacy_dict(
                "climate.trv1",
                {"last_temperature": 21.0, "target_temp_received": False},
            )
        }

        result = await check_target_temperature(mock_self, "climate.trv1")

        assert result is True
        # convert_to_float should handle string "21.0" and match float 21.0


# ---------------------------------------------------------------------------
# _get_valve_control — boost mode is gated by calibration_type
# ---------------------------------------------------------------------------


class TestGetValveControlBoostCalibrationType:
    """Boost mode controls the valve only on TRVs with direct valve control."""

    def _mock_in_boost(self):
        mock_self = MagicMock()
        mock_self.preset_mode = "boost"
        mock_self.cur_temp = 19.0
        mock_self.bt_target_temp = 22.0
        mock_self.real_trvs = {"climate.trv1": Trv.from_legacy_dict("climate.trv1", {})}
        return mock_self

    def test_boost_direct_valve_returns_valve_settings(self):
        """DIRECT_VALVE_BASED + boost → valve_percent=100, source='boost_mode'."""
        mock_self = self._mock_in_boost()
        bal, source = _get_valve_control(
            mock_self,
            _boost_snapshot(),
            "climate.trv1",
            CalibrationMode.MPC_CALIBRATION,
            CalibrationType.DIRECT_VALVE_BASED,
        )
        assert source == "boost_mode"
        assert bal == {"valve_percent": 100, "apply_valve": True}

    def test_boost_local_based_returns_none(self):
        """LOCAL_BASED (offset) + boost → no valve override (None, None)."""
        mock_self = self._mock_in_boost()
        bal, source = _get_valve_control(
            mock_self,
            _boost_snapshot(),
            "climate.trv1",
            CalibrationMode.MPC_CALIBRATION,
            CalibrationType.LOCAL_BASED,
        )
        assert bal is None
        assert source is None

    def test_boost_target_temp_based_returns_none(self):
        """TARGET_TEMP_BASED + boost → no valve override (None, None)."""
        mock_self = self._mock_in_boost()
        bal, source = _get_valve_control(
            mock_self,
            _boost_snapshot(),
            "climate.trv1",
            CalibrationMode.MPC_CALIBRATION,
            CalibrationType.TARGET_TEMP_BASED,
        )
        assert bal is None
        assert source is None


# ---------------------------------------------------------------------------
# _get_valve_control — boost mode honors valve_max_opening
# ---------------------------------------------------------------------------


class TestGetValveControlBoostMaxOpening:
    """Boost mode should clamp valve_percent to the user's valve_max_opening."""

    def _mock_in_boost(self, max_opening):
        mock_self = MagicMock()
        mock_self.preset_mode = "boost"
        mock_self.cur_temp = 19.0
        mock_self.bt_target_temp = 22.0
        mock_self.real_trvs = {
            "climate.trv1": Trv.from_legacy_dict(
                "climate.trv1", {"valve_max_opening": max_opening}
            )
        }
        return mock_self

    def test_no_setting_defaults_to_100(self):
        """Without a configured limit, boost still applies 100%."""
        mock_self = self._mock_in_boost(max_opening=None)
        mock_self.real_trvs["climate.trv1"] = Trv(entity_id="climate.trv1")
        bal, source = _get_valve_control(
            mock_self,
            _boost_snapshot(),
            "climate.trv1",
            CalibrationMode.MPC_CALIBRATION,
            CalibrationType.DIRECT_VALVE_BASED,
        )
        assert source == "boost_mode"
        assert bal == {"valve_percent": 100, "apply_valve": True}

    def test_setting_100_returns_100(self):
        """An explicit 100% setting yields 100%."""
        mock_self = self._mock_in_boost(max_opening=100)
        bal, _ = _get_valve_control(
            mock_self,
            _boost_snapshot(),
            "climate.trv1",
            CalibrationMode.MPC_CALIBRATION,
            CalibrationType.DIRECT_VALVE_BASED,
        )
        assert bal == {"valve_percent": 100, "apply_valve": True}

    def test_setting_60_clamps_to_60(self):
        """Boost respects a configured 60% maximum."""
        mock_self = self._mock_in_boost(max_opening=60)
        bal, source = _get_valve_control(
            mock_self,
            _boost_snapshot(),
            "climate.trv1",
            CalibrationMode.MPC_CALIBRATION,
            CalibrationType.DIRECT_VALVE_BASED,
        )
        assert source == "boost_mode"
        assert bal == {"valve_percent": 60, "apply_valve": True}

    def test_float_setting_rounded(self):
        """Non-integer settings round to nearest int and clamp to [0, 100]."""
        mock_self = self._mock_in_boost(max_opening=72.6)
        bal, _ = _get_valve_control(
            mock_self,
            _boost_snapshot(),
            "climate.trv1",
            CalibrationMode.MPC_CALIBRATION,
            CalibrationType.DIRECT_VALVE_BASED,
        )
        assert bal == {"valve_percent": 73, "apply_valve": True}

    def test_out_of_range_setting_clamped_to_100(self):
        """A nonsensical >100 value is clamped to 100."""
        mock_self = self._mock_in_boost(max_opening=150)
        bal, _ = _get_valve_control(
            mock_self,
            _boost_snapshot(),
            "climate.trv1",
            CalibrationMode.MPC_CALIBRATION,
            CalibrationType.DIRECT_VALVE_BASED,
        )
        assert bal == {"valve_percent": 100, "apply_valve": True}

    def test_non_numeric_setting_defaults_to_100(self):
        """A garbage non-numeric setting is treated as 'no limit'."""
        mock_self = self._mock_in_boost(max_opening="not a number")
        bal, _ = _get_valve_control(
            mock_self,
            _boost_snapshot(),
            "climate.trv1",
            CalibrationMode.MPC_CALIBRATION,
            CalibrationType.DIRECT_VALVE_BASED,
        )
        assert bal == {"valve_percent": 100, "apply_valve": True}
