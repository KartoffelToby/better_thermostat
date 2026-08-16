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
import logging
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfTemperature
import pytest

from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.const import (
    CalibrationMode,
    CalibrationType,
)
from custom_components.better_thermostat.utils.controlling import (
    OFFSET_MATCH_TOLERANCE_K,
    RECONCILE_TOLERANCE_K,
    WRITE_CONFIRM_TIMEOUT_S,
    _calibration_match_tolerance,
    _get_valve_control,
    _reconcile_tolerance,
    check_calibration,
    check_system_mode,
    check_target_temperature,
)
from tests.factories import make_snapshot

_CTRL = "custom_components.better_thermostat.utils.controlling"


def _boost_snapshot():
    """Create a snapshot for an active boost scenario.

    Returns
    -------
    WorldSnapshot
        Snapshot with boost preset enabled and room temperature below target.
    """
    return make_snapshot(preset_mode="boost", room_temp=19.0, target_temp=22.0)


# ---------------------------------------------------------------------------
# check_system_mode
# ---------------------------------------------------------------------------


class TestCheckSystemMode:
    """Test check_system_mode function."""

    def _mock_self(self, live_state, last_hvac_mode, cached_hvac_mode=None):
        """Build a mock BetterThermostat with a live TRV state and one Trv."""
        mock_state = Mock()
        mock_state.state = live_state

        mock_hass = Mock()
        mock_hass.states.get.return_value = (
            mock_state if live_state is not None else None
        )

        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.hass = mock_hass
        mock_self.real_trvs = {
            "climate.trv1": Trv.from_legacy_dict(
                "climate.trv1",
                {
                    "hvac_mode": cached_hvac_mode,
                    "last_hvac_mode": last_hvac_mode,
                    "system_mode_received": False,
                },
            )
        }
        return mock_self, mock_state

    @pytest.mark.asyncio
    async def test_mode_matches_immediately(self):
        """Test when the live state matches immediately."""
        mock_self, _ = self._mock_self(
            live_state=HVACMode.HEAT,
            last_hvac_mode=HVACMode.HEAT,
            cached_hvac_mode=HVACMode.HEAT,
        )

        result = await check_system_mode(mock_self, "climate.trv1")

        assert result is True
        assert mock_self.real_trvs["climate.trv1"].system_mode_received is True

    @pytest.mark.asyncio
    async def test_confirms_via_live_state_with_stale_cache(self):
        """The live state confirms the write even when the internal cache is stale.

        With child lock configured or state events suppressed, the internal
        hvac_mode cache is never refreshed; confirmation must not depend on it.
        """
        mock_self, _ = self._mock_self(
            live_state=HVACMode.HEAT,
            last_hvac_mode=HVACMode.HEAT,
            cached_hvac_mode=HVACMode.OFF,
        )

        result = await check_system_mode(mock_self, "climate.trv1")

        assert result is True
        assert mock_self.real_trvs["climate.trv1"].system_mode_received is True
        # The stale cache stays untouched; only the live state was consulted.
        assert mock_self.real_trvs["climate.trv1"].hvac_mode == HVACMode.OFF

    @pytest.mark.asyncio
    async def test_mode_matches_after_delay(self):
        """Test when the live state matches after a short delay."""
        mock_self, mock_state = self._mock_self(
            live_state=HVACMode.OFF, last_hvac_mode=HVACMode.HEAT
        )

        # Simulate the device reporting the new mode after a short delay
        async def update_mode():
            await asyncio.sleep(0.1)
            mock_state.state = HVACMode.HEAT

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
        mock_self, _ = self._mock_self(
            live_state=HVACMode.OFF, last_hvac_mode=HVACMode.HEAT
        )

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
        finally:
            controlling_module.asyncio.sleep = original_sleep_func

    @pytest.mark.asyncio
    async def test_unavailable_state_treated_as_done(self):
        """An unavailable TRV ends the wait and still sets the flag."""
        mock_self, _ = self._mock_self(
            live_state="unavailable", last_hvac_mode=HVACMode.HEAT
        )

        result = await check_system_mode(mock_self, "climate.trv1")

        assert result is True
        assert mock_self.real_trvs["climate.trv1"].system_mode_received is True

    @pytest.mark.asyncio
    async def test_missing_state_treated_as_done(self):
        """A missing TRV state ends the wait and still sets the flag."""
        mock_self, _ = self._mock_self(live_state=None, last_hvac_mode=HVACMode.HEAT)

        result = await check_system_mode(mock_self, "climate.trv1")

        assert result is True
        assert mock_self.real_trvs["climate.trv1"].system_mode_received is True

    @pytest.mark.asyncio
    async def test_system_mode_received_flag_set(self):
        """Test that system_mode_received flag is always set to True."""
        mock_self, _ = self._mock_self(
            live_state=HVACMode.HEAT, last_hvac_mode=HVACMode.HEAT
        )

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
    async def test_step_grid_written_value_confirms_against_read_grid(self):
        """A step-grid written setpoint confirms against the 0.01 read grid.

        The write side stores last_temperature rounded on the device step
        grid (round_by_step(20.7, 0.1) == 20.700000000000003), while the
        read-back passes through convert_to_float's 0.01 grid (20.7). The
        tolerance-based comparison must confirm immediately instead of
        polling until the 360s timeout.
        """
        from custom_components.better_thermostat.utils.helpers import round_by_step

        written = round_by_step(20.7, 0.1)
        assert written != 20.7  # the grids genuinely diverge

        mock_state = Mock()
        mock_state.attributes = {"temperature": 20.7}

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.hass = mock_hass
        mock_self.real_trvs = {
            "climate.trv1": Trv.from_legacy_dict(
                "climate.trv1",
                {"last_temperature": written, "target_temp_received": False},
            )
        }

        result = await asyncio.wait_for(
            check_target_temperature(mock_self, "climate.trv1"), timeout=10
        )

        assert result is True
        assert mock_self.real_trvs["climate.trv1"].target_temp_received is True

    @pytest.mark.asyncio
    async def test_range_mode_confirms_via_target_temp_low(self):
        """A range-capable TRV confirms the write through target_temp_low."""
        mock_state = Mock()
        mock_state.attributes = {
            "temperature": 17.0,
            "target_temp_low": 21.0,
            "supported_features": int(ClimateEntityFeature.TARGET_TEMPERATURE_RANGE),
        }

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


class TestReconcileTolerance:
    """Tests for _reconcile_tolerance()."""

    @staticmethod
    def _mock_self(system_unit=UnitOfTemperature.CELSIUS):
        mock_self = MagicMock()
        mock_self.device_name = "Test"
        mock_self.hass.config.units.temperature_unit = system_unit
        return mock_self

    @staticmethod
    def _state(attributes):
        state = Mock()
        state.attributes = attributes
        return state

    def test_no_reported_step_falls_back_to_the_base_tolerance(self):
        """Without a usable step there is no grid to derive a tolerance from."""
        tolerance = _reconcile_tolerance(self._mock_self(), self._state({}))
        assert tolerance == RECONCILE_TOLERANCE_K

    def test_celsius_step_yields_half_a_step(self):
        """A snapped value sits at most half a step from the commanded one."""
        tolerance = _reconcile_tolerance(
            self._mock_self(), self._state({"target_temp_step": 0.5})
        )
        assert tolerance == pytest.approx(0.25, abs=1e-5)

    def test_fahrenheit_step_scales_as_an_interval(self):
        """A 1 °F step is 0.5556 K, so the tolerance is half of that."""
        tolerance = _reconcile_tolerance(
            self._mock_self(UnitOfTemperature.FAHRENHEIT),
            self._state({"target_temp_step": 1.0}),
        )
        assert tolerance == pytest.approx(1.0 * 5.0 / 9.0 / 2.0, abs=1e-5)

    def test_kelvin_step_is_not_scaled(self):
        """A Kelvin interval equals a Celsius one."""
        tolerance = _reconcile_tolerance(
            self._mock_self(UnitOfTemperature.KELVIN),
            self._state({"target_temp_step": 0.5}),
        )
        assert tolerance == pytest.approx(0.25, abs=1e-5)


# ---------------------------------------------------------------------------
# check_calibration
# ---------------------------------------------------------------------------


def _sleep_recorder():
    """Patch asyncio.sleep to a no-op that records requested durations."""
    durations = []

    async def _sleep(duration):
        durations.append(duration)

    return durations, patch("asyncio.sleep", new=AsyncMock(side_effect=_sleep))


class TestCheckCalibration:
    """The calibration watchdog always releases the offset write gate.

    Without it, an offset the device never acknowledges leaves
    calibration_received False for the lifetime of the entity and no
    further offset is ever written.
    """

    @staticmethod
    def _mock_self(live_state=HVACMode.HEAT, **trv_overrides):
        """Build a mock BetterThermostat around one offset-writing TRV."""
        mock_state = Mock()
        mock_state.state = live_state

        mock_hass = Mock()
        mock_hass.states.get.return_value = (
            mock_state if live_state is not None else None
        )

        cfg = {
            "last_calibration": -2.0,
            "local_calibration_step": 0.5,
            "calibration_received": False,
        }
        cfg.update(trv_overrides)

        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.hass = mock_hass
        mock_self.real_trvs = {
            "climate.trv1": Trv.from_legacy_dict("climate.trv1", cfg)
        }
        return mock_self

    @pytest.mark.asyncio
    async def test_matching_offset_confirms_immediately(self, caplog):
        """A device already holding the written offset confirms at once."""
        mock_self = self._mock_self()
        durations, sleep_patch = _sleep_recorder()

        with (
            caplog.at_level(logging.WARNING, logger=_CTRL),
            patch(f"{_CTRL}.get_current_offset", new=AsyncMock(return_value=-2.0)),
            sleep_patch,
        ):
            result = await check_calibration(mock_self, "climate.trv1")

        assert result is True
        assert mock_self.real_trvs["climate.trv1"].calibration_received is True
        assert 1 not in durations
        assert caplog.records == []

    @pytest.mark.asyncio
    async def test_half_a_step_away_confirms(self):
        """The device's own grid snap is a confirmation, not a miss."""
        mock_self = self._mock_self(local_calibration_step=1.0)
        durations, sleep_patch = _sleep_recorder()

        with (
            patch(f"{_CTRL}.get_current_offset", new=AsyncMock(return_value=-2.5)),
            sleep_patch,
        ):
            await check_calibration(mock_self, "climate.trv1")

        assert mock_self.real_trvs["climate.trv1"].calibration_received is True
        assert 1 not in durations

    @pytest.mark.asyncio
    async def test_offset_confirmed_after_a_delay_logs_no_warning(self, caplog):
        """A late confirmation ends the wait without an annunciation."""
        mock_self = self._mock_self()
        reports = [-1.0, -1.0, -1.0, -2.0]
        durations, sleep_patch = _sleep_recorder()

        with (
            caplog.at_level(logging.WARNING, logger=_CTRL),
            patch(
                f"{_CTRL}.get_current_offset",
                new=AsyncMock(side_effect=lambda *_: reports.pop(0)),
            ),
            sleep_patch,
        ):
            await check_calibration(mock_self, "climate.trv1")

        assert mock_self.real_trvs["climate.trv1"].calibration_received is True
        assert durations.count(1) == 3
        assert caplog.records == []

    @pytest.mark.asyncio
    async def test_unconfirmed_offset_times_out_and_still_releases(self, caplog):
        """A device that never takes the offset releases the gate anyway.

        The release is what paces the re-assert: the write gate only
        re-sends once the flag is back, so an unacknowledged offset is
        retried once per confirmation window, not once per cycle.
        """
        mock_self = self._mock_self()
        durations, sleep_patch = _sleep_recorder()

        with (
            caplog.at_level(logging.WARNING, logger=_CTRL),
            patch(f"{_CTRL}.get_current_offset", new=AsyncMock(return_value=-1.0)),
            sleep_patch,
        ):
            result = await check_calibration(mock_self, "climate.trv1")

        assert result is True
        assert mock_self.real_trvs["climate.trv1"].calibration_received is True
        assert durations.count(1) == WRITE_CONFIRM_TIMEOUT_S + 1
        assert "did not confirm the calibration offset" in caplog.text

    @pytest.mark.asyncio
    async def test_unparseable_report_is_not_a_confirmation(self, caplog):
        """A garbage reading confirms nothing and runs into the timeout."""
        mock_self = self._mock_self()
        durations, sleep_patch = _sleep_recorder()

        with (
            caplog.at_level(logging.WARNING, logger=_CTRL),
            patch(
                f"{_CTRL}.get_current_offset",
                new=AsyncMock(return_value="not-a-number"),
            ),
            sleep_patch,
        ):
            await check_calibration(mock_self, "climate.trv1")

        assert mock_self.real_trvs["climate.trv1"].calibration_received is True
        assert durations.count(1) == WRITE_CONFIRM_TIMEOUT_S + 1
        assert "did not confirm the calibration offset" in caplog.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("live_state", [None, STATE_UNAVAILABLE, STATE_UNKNOWN])
    async def test_unreachable_trv_ends_the_wait(self, live_state):
        """A TRV that dropped out cannot confirm; the gate opens regardless."""
        mock_self = self._mock_self(live_state=live_state)
        durations, sleep_patch = _sleep_recorder()

        with (
            patch(f"{_CTRL}.get_current_offset", new=AsyncMock(return_value=-1.0)),
            sleep_patch,
        ):
            result = await check_calibration(mock_self, "climate.trv1")

        assert result is True
        assert mock_self.real_trvs["climate.trv1"].calibration_received is True
        assert 1 not in durations

    @pytest.mark.asyncio
    async def test_without_a_written_offset_the_wait_ends_at_once(self):
        """Nothing was commanded, so there is nothing to confirm."""
        mock_self = self._mock_self(last_calibration=None)
        durations, sleep_patch = _sleep_recorder()

        with (
            patch(f"{_CTRL}.get_current_offset", new=AsyncMock(return_value=-1.0)),
            sleep_patch,
        ):
            await check_calibration(mock_self, "climate.trv1")

        assert mock_self.real_trvs["climate.trv1"].calibration_received is True
        assert 1 not in durations

    @pytest.mark.asyncio
    async def test_adapter_error_still_releases_the_write_gate(self):
        """An adapter raising mid-poll does not wedge the offset channel.

        The offset write only goes out while calibration_received is
        True, so a watchdog that ended without releasing it would
        silence the channel for the lifetime of the entity.
        """
        mock_self = self._mock_self()

        async def _raise(_self, _entity_id):
            raise RuntimeError("adapter unavailable")

        with patch(f"{_CTRL}.get_current_offset", new=_raise):
            with pytest.raises(RuntimeError):
                await check_calibration(mock_self, "climate.trv1")

        assert mock_self.real_trvs["climate.trv1"].calibration_received is True

    @pytest.mark.asyncio
    async def test_cancellation_still_releases_the_write_gate(self):
        """A cancelled watchdog leaves the offset channel writable."""
        mock_self = self._mock_self()

        with patch(f"{_CTRL}.get_current_offset", new=AsyncMock(return_value=0.0)):
            task = asyncio.create_task(check_calibration(mock_self, "climate.trv1"))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert mock_self.real_trvs["climate.trv1"].calibration_received is True


class TestCalibrationMatchTolerance:
    """Tests for _calibration_match_tolerance()."""

    @staticmethod
    def _mock_self(step):
        mock_self = MagicMock()
        mock_self.device_name = "Test"
        mock_self.real_trvs = {
            "climate.trv1": Trv.from_legacy_dict(
                "climate.trv1", {"local_calibration_step": step}
            )
        }
        return mock_self

    def test_step_yields_half_a_step(self):
        """A snapped offset sits at most half a step from the command."""
        tolerance = _calibration_match_tolerance(self._mock_self(1.0), "climate.trv1")
        assert tolerance == pytest.approx(0.5, abs=1e-5)

    def test_unusable_step_falls_back_to_the_floor(self):
        """Without a usable step there is no grid to derive a tolerance from."""
        tolerance = _calibration_match_tolerance(self._mock_self(0), "climate.trv1")
        assert tolerance == OFFSET_MATCH_TOLERANCE_K

    def test_step_below_the_floor_does_not_narrow_it(self):
        """A declared step finer than the reported grid does not narrow it."""
        tolerance = _calibration_match_tolerance(self._mock_self(0.01), "climate.trv1")
        assert tolerance == OFFSET_MATCH_TOLERANCE_K

    def test_the_floor_is_the_offset_channel_s_own(self):
        """The offset floor is pinned; the setpoint channel keeps its own."""
        from custom_components.better_thermostat.utils.helpers import (
            SETPOINT_MATCH_TOLERANCE,
        )

        assert OFFSET_MATCH_TOLERANCE_K == 0.05
        assert SETPOINT_MATCH_TOLERANCE == 0.01
