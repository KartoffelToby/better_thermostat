"""Phase 0 baseline tests for climate.py – regression safety net before refactoring.

Tests the 6 most important methods using unbound-method calls with a shared
mock_bt fixture (MagicMock with explicit attributes).
"""

from collections import deque
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.climate.const import (
    ATTR_HVAC_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    HVACAction,
    HVACMode,
    PRESET_AWAY,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_NONE,
)
from homeassistant.const import ATTR_TEMPERATURE

from custom_components.better_thermostat.climate import BetterThermostat


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_bt():
    """Create a mock BetterThermostat with sensible defaults."""
    bt = MagicMock()
    bt.hass = MagicMock()
    bt.device_name = "Test BT"
    # Temperature
    bt.cur_temp = 20.0
    bt.bt_target_temp = 22.0
    bt.bt_target_cooltemp = 26.0
    bt.bt_min_temp = 5.0
    bt.bt_max_temp = 30.0
    bt.bt_target_temp_step = 0.5
    bt.tolerance = 0.5
    # HVAC
    bt.bt_hvac_mode = HVACMode.HEAT
    bt.hvac_mode = HVACMode.HEAT  # property stand-in
    bt.window_open = False
    bt.ignore_states = False
    # Hysteresis
    bt._tolerance_last_action = HVACAction.IDLE
    bt._tolerance_hold_active = False
    # Heating power
    bt.heating_power = 0.05
    bt.heating_start_temp = None
    bt.heating_start_timestamp = None
    bt.heating_end_temp = None
    bt.heating_end_timestamp = None
    bt.old_attr_hvac_action = None
    bt.attr_hvac_action = None
    bt.heating_cycles = deque(maxlen=20)
    bt.last_heating_power_stats = deque(maxlen=10)
    bt.heating_power_normalized = None
    bt.min_target_temp = 18.0
    bt.max_target_temp = 24.0
    bt.outdoor_sensor = None
    # Heat loss
    bt.heat_loss_rate = 0.01
    bt.loss_start_temp = None
    bt.loss_start_timestamp = None
    bt.loss_end_temp = None
    bt.loss_end_timestamp = None
    bt._loss_last_action = HVACAction.IDLE
    bt.loss_cycles = deque(maxlen=20)
    bt.last_heat_loss_stats = deque(maxlen=10)
    # Presets
    bt._preset_mode = PRESET_NONE
    bt._preset_temperature = None
    bt._preset_temperatures = {
        PRESET_NONE: 20.0,
        PRESET_COMFORT: 21.0,
        PRESET_ECO: 19.0,
        PRESET_AWAY: 16.0,
    }
    bt._enabled_presets = [PRESET_COMFORT, PRESET_ECO, PRESET_AWAY]
    bt.bt_update_lock = False
    # TRVs
    bt.real_trvs = {}
    # HA callbacks
    bt.control_queue_task = AsyncMock()
    bt.async_write_ha_state = MagicMock()
    bt.schedule_save_state = MagicMock()
    bt.in_maintenance = False
    bt._control_needed_after_maintenance = False
    # Property stand-ins for min_temp / max_temp
    bt.min_temp = bt.bt_min_temp
    bt.max_temp = bt.bt_max_temp
    # Bind real methods
    bt._should_heat_with_tolerance = lambda prev, tol: (
        BetterThermostat._should_heat_with_tolerance(bt, prev, tol)
    )
    bt._compute_hvac_action = lambda: (
        BetterThermostat._compute_hvac_action(bt)
    )
    return bt


# ===========================================================================
# 1. TestShouldHeatWithTolerance
# ===========================================================================


class TestShouldHeatWithTolerance:
    """Tests for _should_heat_with_tolerance (L2799-2811)."""

    def _call(self, bt, previous_action, tol):
        return BetterThermostat._should_heat_with_tolerance(bt, previous_action, tol)

    def test_target_temp_none(self, mock_bt):
        mock_bt.bt_target_temp = None
        assert self._call(mock_bt, HVACAction.IDLE, 0.5) is False

    def test_cur_temp_none(self, mock_bt):
        mock_bt.cur_temp = None
        assert self._call(mock_bt, HVACAction.IDLE, 0.5) is False

    def test_heating_cur_below_target(self, mock_bt):
        mock_bt.cur_temp = 21.5
        mock_bt.bt_target_temp = 22.0
        assert self._call(mock_bt, HVACAction.HEATING, 0.5) is True

    def test_heating_cur_equals_target(self, mock_bt):
        mock_bt.cur_temp = 22.0
        mock_bt.bt_target_temp = 22.0
        assert self._call(mock_bt, HVACAction.HEATING, 0.5) is False

    def test_heating_cur_above_target(self, mock_bt):
        mock_bt.cur_temp = 22.5
        mock_bt.bt_target_temp = 22.0
        assert self._call(mock_bt, HVACAction.HEATING, 0.5) is False

    def test_idle_cur_below_threshold(self, mock_bt):
        mock_bt.cur_temp = 21.0
        mock_bt.bt_target_temp = 22.0
        assert self._call(mock_bt, HVACAction.IDLE, 0.5) is True

    def test_idle_cur_equals_threshold(self, mock_bt):
        mock_bt.cur_temp = 21.5
        mock_bt.bt_target_temp = 22.0
        assert self._call(mock_bt, HVACAction.IDLE, 0.5) is False

    def test_idle_cur_above_threshold(self, mock_bt):
        mock_bt.cur_temp = 21.8
        mock_bt.bt_target_temp = 22.0
        assert self._call(mock_bt, HVACAction.IDLE, 0.5) is False

    def test_negative_tolerance_clamped_to_zero(self, mock_bt):
        """Negative tolerance → clamped to 0 → threshold == target."""
        mock_bt.cur_temp = 21.9
        mock_bt.bt_target_temp = 22.0
        # With tol=0, IDLE threshold is target itself → 21.9 < 22.0 → True
        assert self._call(mock_bt, HVACAction.IDLE, -1.0) is True

    def test_zero_tolerance_no_hysteresis(self, mock_bt):
        """Tolerance 0 → IDLE threshold == target (no hysteresis band)."""
        mock_bt.cur_temp = 21.9
        mock_bt.bt_target_temp = 22.0
        assert self._call(mock_bt, HVACAction.IDLE, 0.0) is True
        mock_bt.cur_temp = 22.0
        assert self._call(mock_bt, HVACAction.IDLE, 0.0) is False


# ===========================================================================
# 2. TestComputeHvacAction
# ===========================================================================


class TestComputeHvacAction:
    """Tests for _compute_hvac_action (L2812-2978)."""

    def _call(self, bt):
        return BetterThermostat._compute_hvac_action(bt)

    def test_target_temp_none_returns_idle(self, mock_bt):
        mock_bt.bt_target_temp = None
        assert self._call(mock_bt) == HVACAction.IDLE

    def test_cur_temp_none_returns_idle(self, mock_bt):
        mock_bt.cur_temp = None
        assert self._call(mock_bt) == HVACAction.IDLE

    def test_hvac_mode_off_returns_off(self, mock_bt):
        mock_bt.hvac_mode = HVACMode.OFF
        assert self._call(mock_bt) == HVACAction.OFF

    def test_bt_hvac_mode_off_returns_off(self, mock_bt):
        mock_bt.bt_hvac_mode = HVACMode.OFF
        assert self._call(mock_bt) == HVACAction.OFF

    def test_window_open_returns_idle(self, mock_bt):
        mock_bt.window_open = True
        assert self._call(mock_bt) == HVACAction.IDLE

    def test_heat_mode_cur_below_threshold(self, mock_bt):
        """HEAT mode, cur < target - tol → HEATING."""
        mock_bt.cur_temp = 21.0
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._tolerance_last_action = HVACAction.IDLE
        assert self._call(mock_bt) == HVACAction.HEATING

    def test_heat_mode_cur_at_target(self, mock_bt):
        """HEAT mode, cur >= target → IDLE."""
        mock_bt.cur_temp = 22.0
        mock_bt.bt_target_temp = 22.0
        mock_bt._tolerance_last_action = HVACAction.IDLE
        assert self._call(mock_bt) == HVACAction.IDLE

    def test_heat_cool_cooling_above_cooltemp(self, mock_bt):
        """HEAT_COOL, cur > cooltemp + tol → COOLING."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_hvac_mode = HVACMode.HEAT_COOL
        mock_bt.cur_temp = 27.0
        mock_bt.bt_target_temp = 22.0
        mock_bt.bt_target_cooltemp = 26.0
        mock_bt.tolerance = 0.5
        assert self._call(mock_bt) == HVACAction.COOLING

    def test_trv_override_hvac_action_heating(self, mock_bt):
        """TRV reports hvac_action='heating' → override to HEATING."""
        mock_bt.cur_temp = 22.0  # at target → base decision is IDLE
        mock_bt.bt_target_temp = 22.0
        mock_bt._tolerance_last_action = HVACAction.IDLE
        mock_bt.real_trvs = {"climate.trv1": {"hvac_action": "heating"}}
        assert self._call(mock_bt) == HVACAction.HEATING

    def test_trv_override_valve_position(self, mock_bt):
        """TRV valve_position=50 → override to HEATING."""
        mock_bt.cur_temp = 22.0
        mock_bt.bt_target_temp = 22.0
        mock_bt._tolerance_last_action = HVACAction.IDLE
        mock_bt.real_trvs = {"climate.trv1": {"valve_position": 50}}
        assert self._call(mock_bt) == HVACAction.HEATING

    def test_trv_override_last_valve_percent_0_1_range(self, mock_bt):
        """TRV last_valve_percent=0.8 (0-1 range) → normalized to 80% → HEATING."""
        mock_bt.cur_temp = 22.0
        mock_bt.bt_target_temp = 22.0
        mock_bt._tolerance_last_action = HVACAction.IDLE
        mock_bt.real_trvs = {"climate.trv1": {"last_valve_percent": 0.8}}
        assert self._call(mock_bt) == HVACAction.HEATING

    def test_ignore_states_no_trv_override(self, mock_bt):
        """ignore_states=True → TRV override skipped, returns IDLE."""
        mock_bt.cur_temp = 22.0
        mock_bt.bt_target_temp = 22.0
        mock_bt._tolerance_last_action = HVACAction.IDLE
        mock_bt.ignore_states = True
        mock_bt.real_trvs = {"climate.trv1": {"hvac_action": "heating"}}
        assert self._call(mock_bt) == HVACAction.IDLE

    def test_ignore_trv_states_per_trv(self, mock_bt):
        """ignore_trv_states=True on specific TRV → that TRV is skipped."""
        mock_bt.cur_temp = 22.0
        mock_bt.bt_target_temp = 22.0
        mock_bt._tolerance_last_action = HVACAction.IDLE
        mock_bt.real_trvs = {
            "climate.trv1": {"hvac_action": "heating", "ignore_trv_states": True},
        }
        assert self._call(mock_bt) == HVACAction.IDLE

    def test_tolerance_decision_saved_before_trv_override(self, mock_bt):
        """Hysteresis state uses tolerance decision, not TRV-overridden action."""
        mock_bt.cur_temp = 22.0  # at target → tolerance says IDLE
        mock_bt.bt_target_temp = 22.0
        mock_bt._tolerance_last_action = HVACAction.IDLE
        mock_bt.real_trvs = {"climate.trv1": {"hvac_action": "heating"}}
        self._call(mock_bt)
        # Tolerance last action should be IDLE (tolerance decision), not HEATING
        assert mock_bt._tolerance_last_action == HVACAction.IDLE

    def test_tolerance_hold_active_set(self, mock_bt):
        """_tolerance_hold_active is True when tolerance says no-heat but not cooling."""
        mock_bt.cur_temp = 21.8  # in band: target-tol(21.5) < cur < target(22.0)
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._tolerance_last_action = HVACAction.IDLE
        self._call(mock_bt)
        assert mock_bt._tolerance_hold_active is True


# ===========================================================================
# 3. TestCalculateHeatingPower
# ===========================================================================


class TestCalculateHeatingPower:
    """Tests for calculate_heating_power (L2139-2391)."""

    async def _call(self, bt):
        return await BetterThermostat.calculate_heating_power(bt)

    @pytest.mark.asyncio
    async def test_cur_temp_none_early_return(self, mock_bt):
        mock_bt.cur_temp = None
        old_power = mock_bt.heating_power
        await self._call(mock_bt)
        assert mock_bt.heating_power == old_power

    @pytest.mark.asyncio
    async def test_heating_start_transition(self, mock_bt):
        """Transition to HEATING sets start_temp and start_timestamp."""
        mock_bt.cur_temp = 20.0
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._tolerance_last_action = HVACAction.IDLE
        mock_bt.old_attr_hvac_action = HVACAction.IDLE
        # Make _compute_hvac_action return HEATING
        mock_bt.hvac_mode = HVACMode.HEAT
        mock_bt.bt_hvac_mode = HVACMode.HEAT
        mock_bt.window_open = False
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        with patch("custom_components.better_thermostat.climate.dt_util") as mock_dt:
            now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
            mock_dt.utcnow.return_value = now
            await self._call(mock_bt)

        assert mock_bt.heating_start_temp == 20.0
        assert mock_bt.heating_start_timestamp == now

    @pytest.mark.asyncio
    async def test_heating_stop_sets_end(self, mock_bt):
        """Transition from HEATING → IDLE sets end_temp/timestamp."""
        now = datetime(2025, 1, 1, 12, 10, 0, tzinfo=timezone.utc)
        mock_bt.cur_temp = 22.0
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._tolerance_last_action = HVACAction.HEATING
        mock_bt.old_attr_hvac_action = HVACAction.HEATING
        mock_bt.heating_start_temp = 20.0
        mock_bt.heating_start_timestamp = now - timedelta(minutes=10)
        mock_bt.heating_end_temp = None
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        with patch("custom_components.better_thermostat.climate.dt_util") as mock_dt:
            mock_dt.utcnow.return_value = now
            await self._call(mock_bt)

        assert mock_bt.heating_end_temp == 22.0
        assert mock_bt.heating_end_timestamp == now

    @pytest.mark.asyncio
    async def test_peak_tracking(self, mock_bt):
        """Temperature still rising after heating stopped → end_temp updated."""
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        mock_bt.cur_temp = 22.5  # above previous end_temp
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._tolerance_last_action = HVACAction.IDLE
        mock_bt.old_attr_hvac_action = HVACAction.IDLE
        mock_bt.heating_start_temp = 20.0
        mock_bt.heating_start_timestamp = base - timedelta(minutes=15)
        mock_bt.heating_end_temp = 22.0
        mock_bt.heating_end_timestamp = base - timedelta(minutes=5)
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        with patch("custom_components.better_thermostat.climate.dt_util") as mock_dt:
            mock_dt.utcnow.return_value = base
            await self._call(mock_bt)

        assert mock_bt.heating_end_temp == 22.5

    @pytest.mark.asyncio
    async def test_finalization_on_temp_drop(self, mock_bt):
        """Temperature falls below peak → cycle finalized, power updated."""
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        mock_bt.cur_temp = 21.8  # below peak of 22.5
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._tolerance_last_action = HVACAction.IDLE
        mock_bt.old_attr_hvac_action = HVACAction.IDLE
        mock_bt.heating_start_temp = 20.0
        mock_bt.heating_start_timestamp = base - timedelta(minutes=10)
        mock_bt.heating_end_temp = 22.5
        mock_bt.heating_end_timestamp = base - timedelta(minutes=2)
        mock_bt.heating_power = 0.05
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        with patch("custom_components.better_thermostat.climate.dt_util") as mock_dt:
            mock_dt.utcnow.return_value = base
            await self._call(mock_bt)

        # Cycle reset after finalization
        assert mock_bt.heating_start_temp is None
        assert mock_bt.heating_end_temp is None
        # Power was updated (EMA smoothing)
        assert mock_bt.heating_power != 0.05
        assert len(mock_bt.last_heating_power_stats) == 1

    @pytest.mark.asyncio
    async def test_finalization_on_timeout(self, mock_bt):
        """30-minute timeout triggers finalization even without temp drop."""
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        mock_bt.cur_temp = 22.5  # still at peak (no drop)
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._tolerance_last_action = HVACAction.IDLE
        mock_bt.old_attr_hvac_action = HVACAction.IDLE
        mock_bt.heating_start_temp = 20.0
        mock_bt.heating_start_timestamp = base - timedelta(minutes=40)
        mock_bt.heating_end_temp = 22.5
        mock_bt.heating_end_timestamp = base - timedelta(minutes=31)
        mock_bt.heating_power = 0.05
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        with patch("custom_components.better_thermostat.climate.dt_util") as mock_dt:
            mock_dt.utcnow.return_value = base
            await self._call(mock_bt)

        assert mock_bt.heating_start_temp is None
        assert len(mock_bt.last_heating_power_stats) == 1

    @pytest.mark.asyncio
    async def test_short_cycle_discarded(self, mock_bt):
        """Cycles shorter than 1 minute are discarded."""
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        mock_bt.cur_temp = 21.8
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._tolerance_last_action = HVACAction.IDLE
        mock_bt.old_attr_hvac_action = HVACAction.IDLE
        mock_bt.heating_start_temp = 20.0
        mock_bt.heating_start_timestamp = base - timedelta(seconds=30)  # 0.5 min
        mock_bt.heating_end_temp = 22.5
        mock_bt.heating_end_timestamp = base - timedelta(seconds=5)
        old_power = mock_bt.heating_power
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        with patch("custom_components.better_thermostat.climate.dt_util") as mock_dt:
            mock_dt.utcnow.return_value = base
            await self._call(mock_bt)

        assert mock_bt.heating_power == old_power
        assert len(mock_bt.last_heating_power_stats) == 0

    @pytest.mark.asyncio
    async def test_negative_temp_diff_discarded(self, mock_bt):
        """Negative temperature diff (end < start) is discarded."""
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        mock_bt.cur_temp = 19.0  # below peak → finalize
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._tolerance_last_action = HVACAction.IDLE
        mock_bt.old_attr_hvac_action = HVACAction.IDLE
        mock_bt.heating_start_temp = 21.0
        mock_bt.heating_start_timestamp = base - timedelta(minutes=10)
        mock_bt.heating_end_temp = 20.0  # end < start → negative diff
        mock_bt.heating_end_timestamp = base - timedelta(minutes=2)
        old_power = mock_bt.heating_power
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        with patch("custom_components.better_thermostat.climate.dt_util") as mock_dt:
            mock_dt.utcnow.return_value = base
            await self._call(mock_bt)

        assert mock_bt.heating_power == old_power
        assert len(mock_bt.last_heating_power_stats) == 0

    @pytest.mark.asyncio
    async def test_ema_smoothing(self, mock_bt):
        """EMA: new = old * (1-alpha) + rate * alpha."""
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        mock_bt.cur_temp = 21.8  # above tol threshold (21.5) so action=IDLE, below end_temp
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._tolerance_last_action = HVACAction.IDLE
        mock_bt.old_attr_hvac_action = HVACAction.IDLE
        mock_bt.heating_start_temp = 20.0
        mock_bt.heating_start_timestamp = base - timedelta(minutes=10)
        mock_bt.heating_end_temp = 22.0
        mock_bt.heating_end_timestamp = base - timedelta(minutes=2)
        mock_bt.heating_power = 0.05
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        with patch("custom_components.better_thermostat.climate.dt_util") as mock_dt:
            mock_dt.utcnow.return_value = base
            await self._call(mock_bt)

        # Power should have moved towards the observed rate via EMA
        # rate = 2.0/10.0 = 0.2 °C/min, old = 0.05, alpha ~0.10
        # new ≈ 0.05 * 0.9 + 0.2 * 0.1 = 0.045 + 0.02 = 0.065
        assert mock_bt.heating_power > 0.05
        assert mock_bt.heating_power <= 0.2

    @pytest.mark.asyncio
    async def test_outdoor_normalization(self, mock_bt):
        """Outdoor sensor present → normalized_power is calculated."""
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        mock_bt.cur_temp = 21.8  # above tol threshold so action=IDLE, below end_temp
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._tolerance_last_action = HVACAction.IDLE
        mock_bt.old_attr_hvac_action = HVACAction.IDLE
        mock_bt.heating_start_temp = 20.0
        mock_bt.heating_start_timestamp = base - timedelta(minutes=10)
        mock_bt.heating_end_temp = 22.0
        mock_bt.heating_end_timestamp = base - timedelta(minutes=2)
        mock_bt.outdoor_sensor = "sensor.outdoor"
        outdoor_state = MagicMock()
        outdoor_state.state = "5.0"
        mock_bt.hass.states.get.return_value = outdoor_state
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        with patch("custom_components.better_thermostat.climate.dt_util") as mock_dt:
            mock_dt.utcnow.return_value = base
            await self._call(mock_bt)

        assert mock_bt.heating_power_normalized is not None
        stats = mock_bt.last_heating_power_stats[-1]
        assert stats["norm"] is not None

    @pytest.mark.asyncio
    async def test_min_max_clamping(self, mock_bt):
        """Power is clamped to [MIN_HEATING_POWER, MAX_HEATING_POWER]."""
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        mock_bt.cur_temp = 21.8  # above tol threshold so action=IDLE, below end_temp
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._tolerance_last_action = HVACAction.IDLE
        mock_bt.old_attr_hvac_action = HVACAction.IDLE
        mock_bt.heating_start_temp = 20.0
        mock_bt.heating_start_timestamp = base - timedelta(minutes=10)
        mock_bt.heating_end_temp = 22.0
        mock_bt.heating_end_timestamp = base - timedelta(minutes=2)
        mock_bt.heating_power = 0.0001  # very low → EMA result may be low
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        with patch("custom_components.better_thermostat.climate.dt_util") as mock_dt:
            mock_dt.utcnow.return_value = base
            await self._call(mock_bt)

        # MIN_HEATING_POWER = 0.005, MAX_HEATING_POWER = 0.2
        assert mock_bt.heating_power >= 0.005
        assert mock_bt.heating_power <= 0.2

    @pytest.mark.asyncio
    async def test_cycle_telemetry_appended(self, mock_bt):
        """Finalized cycle appends to heating_cycles deque."""
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        mock_bt.cur_temp = 21.8  # above tol threshold so action=IDLE, below end_temp
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._tolerance_last_action = HVACAction.IDLE
        mock_bt.old_attr_hvac_action = HVACAction.IDLE
        mock_bt.heating_start_temp = 20.0
        mock_bt.heating_start_timestamp = base - timedelta(minutes=10)
        mock_bt.heating_end_temp = 22.0
        mock_bt.heating_end_timestamp = base - timedelta(minutes=2)
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        with patch("custom_components.better_thermostat.climate.dt_util") as mock_dt:
            mock_dt.utcnow.return_value = base
            await self._call(mock_bt)

        assert len(mock_bt.heating_cycles) == 1
        cycle = mock_bt.heating_cycles[0]
        assert "delta_t" in cycle
        assert "rate_c_min" in cycle


# ===========================================================================
# 4. TestCalculateHeatLoss
# ===========================================================================


class TestCalculateHeatLoss:
    """Tests for calculate_heat_loss (L2392-2522)."""

    async def _call(self, bt):
        return await BetterThermostat.calculate_heat_loss(bt)

    @pytest.mark.asyncio
    async def test_cur_temp_none_early_return(self, mock_bt):
        mock_bt.cur_temp = None
        await self._call(mock_bt)
        assert mock_bt.loss_start_temp is None

    @pytest.mark.asyncio
    async def test_window_open_resets_tracking(self, mock_bt):
        """Window open → all tracking values reset."""
        mock_bt.window_open = True
        mock_bt.loss_start_temp = 21.0
        mock_bt.loss_start_timestamp = datetime(2025, 1, 1, tzinfo=timezone.utc)
        mock_bt.loss_end_temp = 20.5
        mock_bt.loss_end_timestamp = datetime(2025, 1, 1, tzinfo=timezone.utc)
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        with patch("custom_components.better_thermostat.climate.dt_util") as mock_dt:
            mock_dt.utcnow.return_value = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
            await self._call(mock_bt)

        assert mock_bt.loss_start_temp is None
        assert mock_bt.loss_end_temp is None

    @pytest.mark.asyncio
    async def test_idle_starts_tracking(self, mock_bt):
        """Entering IDLE starts tracking (loss_start_temp set)."""
        mock_bt.cur_temp = 22.0
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._tolerance_last_action = HVACAction.IDLE
        mock_bt.loss_start_temp = None
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        with patch("custom_components.better_thermostat.climate.dt_util") as mock_dt:
            now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
            mock_dt.utcnow.return_value = now
            await self._call(mock_bt)

        assert mock_bt.loss_start_temp == 22.0
        assert mock_bt.loss_start_timestamp == now

    @pytest.mark.asyncio
    async def test_tracks_lowest_temp(self, mock_bt):
        """While idle, end_temp tracks the lowest temperature."""
        now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        # cur_temp must yield IDLE (>= target - tol) AND be below loss_end_temp
        mock_bt.cur_temp = 21.6
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5  # threshold = 21.5, 21.6 >= 21.5 → IDLE
        mock_bt._tolerance_last_action = HVACAction.IDLE
        mock_bt.loss_start_temp = 22.0
        mock_bt.loss_start_timestamp = now - timedelta(minutes=10)
        mock_bt.loss_end_temp = 21.8  # current (21.6) is lower
        mock_bt.loss_end_timestamp = now - timedelta(minutes=5)

        with patch("custom_components.better_thermostat.climate.dt_util") as mock_dt:
            mock_dt.utcnow.return_value = now
            await self._call(mock_bt)

        assert mock_bt.loss_end_temp == 21.6

    @pytest.mark.asyncio
    async def test_finalization_on_heating_restart(self, mock_bt):
        """Heating starts again → cycle finalized, heat_loss updated."""
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        # Set up a completed idle period
        mock_bt.cur_temp = 20.0  # below target-tol → HEATING
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._tolerance_last_action = HVACAction.IDLE
        mock_bt.loss_start_temp = 22.0
        mock_bt.loss_start_timestamp = base - timedelta(minutes=10)
        mock_bt.loss_end_temp = 20.5
        mock_bt.loss_end_timestamp = base - timedelta(minutes=2)
        mock_bt.heat_loss_rate = 0.01
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        with patch("custom_components.better_thermostat.climate.dt_util") as mock_dt:
            mock_dt.utcnow.return_value = base
            await self._call(mock_bt)

        # Cycle finalized (reset)
        assert mock_bt.loss_start_temp is None
        assert mock_bt.loss_end_temp is None
        assert len(mock_bt.last_heat_loss_stats) == 1

    @pytest.mark.asyncio
    async def test_short_loss_cycle_discarded(self, mock_bt):
        """Loss cycles shorter than 1 minute are discarded."""
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        mock_bt.cur_temp = 20.0
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._tolerance_last_action = HVACAction.IDLE
        mock_bt.loss_start_temp = 22.0
        mock_bt.loss_start_timestamp = base - timedelta(seconds=30)
        mock_bt.loss_end_temp = 21.0
        mock_bt.loss_end_timestamp = base - timedelta(seconds=10)
        old_rate = mock_bt.heat_loss_rate
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        with patch("custom_components.better_thermostat.climate.dt_util") as mock_dt:
            mock_dt.utcnow.return_value = base
            await self._call(mock_bt)

        assert mock_bt.heat_loss_rate == old_rate
        assert len(mock_bt.last_heat_loss_stats) == 0

    @pytest.mark.asyncio
    async def test_ema_smoothing(self, mock_bt):
        """EMA smoothing applied to heat_loss_rate."""
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        mock_bt.cur_temp = 20.0
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._tolerance_last_action = HVACAction.IDLE
        mock_bt.loss_start_temp = 22.0
        mock_bt.loss_start_timestamp = base - timedelta(minutes=10)
        mock_bt.loss_end_temp = 20.0  # 2°C drop in 10 min
        mock_bt.loss_end_timestamp = base - timedelta(minutes=2)
        mock_bt.heat_loss_rate = 0.01
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        with patch("custom_components.better_thermostat.climate.dt_util") as mock_dt:
            mock_dt.utcnow.return_value = base
            await self._call(mock_bt)

        # rate = 2.0/10.0 = 0.2, old = 0.01, alpha = 0.10
        # new ≈ 0.01 * 0.9 + 0.2 * 0.1 = 0.009 + 0.02 = 0.029
        assert mock_bt.heat_loss_rate > 0.01

    @pytest.mark.asyncio
    async def test_min_max_clamping(self, mock_bt):
        """Loss rate is clamped to [MIN_HEAT_LOSS, MAX_HEAT_LOSS]."""
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        mock_bt.cur_temp = 20.0
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._tolerance_last_action = HVACAction.IDLE
        mock_bt.loss_start_temp = 22.0
        mock_bt.loss_start_timestamp = base - timedelta(minutes=5)
        mock_bt.loss_end_temp = 20.0
        mock_bt.loss_end_timestamp = base - timedelta(minutes=2)
        mock_bt.heat_loss_rate = 0.0001  # very low
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        with patch("custom_components.better_thermostat.climate.dt_util") as mock_dt:
            mock_dt.utcnow.return_value = base
            await self._call(mock_bt)

        # MIN_HEAT_LOSS = 0.001, MAX_HEAT_LOSS = 0.05
        assert mock_bt.heat_loss_rate >= 0.001
        assert mock_bt.heat_loss_rate <= 0.05

    @pytest.mark.asyncio
    async def test_loss_cycle_telemetry(self, mock_bt):
        """Finalized loss cycle appends telemetry to loss_cycles deque."""
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        mock_bt.cur_temp = 20.0
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._tolerance_last_action = HVACAction.IDLE
        mock_bt.loss_start_temp = 22.0
        mock_bt.loss_start_timestamp = base - timedelta(minutes=10)
        mock_bt.loss_end_temp = 20.5
        mock_bt.loss_end_timestamp = base - timedelta(minutes=2)
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        with patch("custom_components.better_thermostat.climate.dt_util") as mock_dt:
            mock_dt.utcnow.return_value = base
            await self._call(mock_bt)

        assert len(mock_bt.loss_cycles) == 1
        cycle = mock_bt.loss_cycles[0]
        assert "rate" in cycle
        assert "temp_start" in cycle


# ===========================================================================
# 5. TestAsyncSetPresetMode
# ===========================================================================


class TestAsyncSetPresetMode:
    """Tests for async_set_preset_mode (L3404-3509)."""

    async def _call(self, bt, preset_mode):
        return await BetterThermostat.async_set_preset_mode(bt, preset_mode)

    @pytest.mark.asyncio
    async def test_invalid_preset_no_change(self, mock_bt):
        """Invalid preset → warning, no state change."""
        # preset_modes returns [PRESET_NONE] + _enabled_presets
        mock_bt.preset_modes = [PRESET_NONE, PRESET_COMFORT, PRESET_ECO, PRESET_AWAY]
        old_preset = mock_bt._preset_mode
        old_temp = mock_bt.bt_target_temp
        await self._call(mock_bt, "nonexistent")
        assert mock_bt._preset_mode == old_preset
        assert mock_bt.bt_target_temp == old_temp

    @pytest.mark.asyncio
    async def test_none_to_comfort(self, mock_bt):
        """NONE → Comfort: saves current temp, applies configured comfort temp."""
        mock_bt.preset_modes = [PRESET_NONE, PRESET_COMFORT, PRESET_ECO, PRESET_AWAY]
        mock_bt._preset_mode = PRESET_NONE
        mock_bt.bt_target_temp = 20.0
        mock_bt._preset_temperature = None
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        await self._call(mock_bt, PRESET_COMFORT)
        assert mock_bt._preset_mode == PRESET_COMFORT
        assert mock_bt._preset_temperature == 20.0  # saved original
        assert mock_bt.bt_target_temp == 21.0  # configured comfort temp

    @pytest.mark.asyncio
    async def test_comfort_to_none_restores(self, mock_bt):
        """Comfort → NONE: bt_target_temp restored, _preset_temperature cleared."""
        mock_bt.preset_modes = [PRESET_NONE, PRESET_COMFORT, PRESET_ECO, PRESET_AWAY]
        mock_bt._preset_mode = PRESET_COMFORT
        mock_bt._preset_temperature = 20.0
        mock_bt.bt_target_temp = 21.0
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        await self._call(mock_bt, PRESET_NONE)
        assert mock_bt._preset_mode == PRESET_NONE
        assert mock_bt.bt_target_temp == 20.0  # restored
        assert mock_bt._preset_temperature is None

    @pytest.mark.asyncio
    async def test_comfort_to_eco(self, mock_bt):
        """Comfort → Eco: bt_target_temp = eco config, _preset_temperature unchanged."""
        mock_bt.preset_modes = [PRESET_NONE, PRESET_COMFORT, PRESET_ECO, PRESET_AWAY]
        mock_bt._preset_mode = PRESET_COMFORT
        mock_bt._preset_temperature = 20.0  # saved from initial manual temp
        mock_bt.bt_target_temp = 21.0
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        await self._call(mock_bt, PRESET_ECO)
        assert mock_bt._preset_mode == PRESET_ECO
        assert mock_bt.bt_target_temp == 19.0  # eco configured
        assert mock_bt._preset_temperature == 20.0  # still saved

    @pytest.mark.asyncio
    async def test_eco_to_none_restores_original(self, mock_bt):
        """Eco → NONE: bt_target_temp = saved original temp."""
        mock_bt.preset_modes = [PRESET_NONE, PRESET_COMFORT, PRESET_ECO, PRESET_AWAY]
        mock_bt._preset_mode = PRESET_ECO
        mock_bt._preset_temperature = 20.0
        mock_bt.bt_target_temp = 19.0
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        await self._call(mock_bt, PRESET_NONE)
        assert mock_bt.bt_target_temp == 20.0

    @pytest.mark.asyncio
    async def test_preset_temp_clamped_to_max(self, mock_bt):
        """Preset temp above max → clamped to max_temp."""
        mock_bt.preset_modes = [PRESET_NONE, PRESET_COMFORT, PRESET_ECO, PRESET_AWAY]
        mock_bt._preset_mode = PRESET_NONE
        mock_bt._preset_temperatures[PRESET_COMFORT] = 35.0  # above max
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp  # 30.0
        await self._call(mock_bt, PRESET_COMFORT)
        assert mock_bt.bt_target_temp == 30.0

    @pytest.mark.asyncio
    async def test_control_queue_put_called(self, mock_bt):
        """control_queue_task.put is called after preset change."""
        mock_bt.preset_modes = [PRESET_NONE, PRESET_COMFORT, PRESET_ECO, PRESET_AWAY]
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        await self._call(mock_bt, PRESET_COMFORT)
        mock_bt.control_queue_task.put.assert_awaited_once_with(mock_bt)


# ===========================================================================
# 6. TestAsyncSetTemperature
# ===========================================================================


class TestAsyncSetTemperature:
    """Tests for async_set_temperature (L3041-3282)."""

    async def _call(self, bt, **kwargs):
        return await BetterThermostat.async_set_temperature(bt, **kwargs)

    @pytest.mark.asyncio
    async def test_simple_setpoint(self, mock_bt):
        """Simple temperature set: {ATTR_TEMPERATURE: 22.0}."""
        mock_bt._preset_mode = PRESET_NONE
        mock_bt.bt_hvac_mode = HVACMode.HEAT
        await self._call(mock_bt, **{ATTR_TEMPERATURE: 22.0})
        assert mock_bt.bt_target_temp == 22.0

    @pytest.mark.asyncio
    async def test_hvac_mode_change_in_kwargs(self, mock_bt):
        """HVAC mode change passed in kwargs."""
        mock_bt._preset_mode = PRESET_NONE
        mock_bt.bt_hvac_mode = HVACMode.HEAT
        await self._call(
            mock_bt, **{ATTR_TEMPERATURE: 22.0, ATTR_HVAC_MODE: HVACMode.OFF}
        )
        assert mock_bt.bt_hvac_mode == HVACMode.OFF

    @pytest.mark.asyncio
    async def test_heat_cool_low_high_setpoints(self, mock_bt):
        """HEAT_COOL with low/high setpoints."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_hvac_mode = HVACMode.HEAT_COOL
        mock_bt._preset_mode = PRESET_NONE
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        await self._call(
            mock_bt,
            **{ATTR_TARGET_TEMP_LOW: 20.0, ATTR_TARGET_TEMP_HIGH: 26.0},
        )
        assert mock_bt.bt_target_temp == 20.0
        assert mock_bt.bt_target_cooltemp == 26.0

    @pytest.mark.asyncio
    async def test_cool_target_enforced_above_heat(self, mock_bt):
        """Cool target adjusted to be above heat target in HEAT_COOL mode."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_hvac_mode = HVACMode.HEAT_COOL
        mock_bt._preset_mode = PRESET_NONE
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        mock_bt.bt_target_temp = 22.0
        mock_bt.bt_target_cooltemp = 20.0  # below heat target → should be adjusted
        await self._call(mock_bt, **{ATTR_TEMPERATURE: 22.0})
        assert mock_bt.bt_target_cooltemp > mock_bt.bt_target_temp

    @pytest.mark.asyncio
    async def test_min_max_clamping(self, mock_bt):
        """Temperature clamped to min/max bounds."""
        mock_bt._preset_mode = PRESET_NONE
        mock_bt.min_temp = mock_bt.bt_min_temp  # 5.0
        mock_bt.max_temp = mock_bt.bt_max_temp  # 30.0
        await self._call(mock_bt, **{ATTR_TEMPERATURE: 50.0})
        assert mock_bt.bt_target_temp == 30.0

    @pytest.mark.asyncio
    async def test_min_clamping(self, mock_bt):
        """Temperature below min → clamped to min."""
        mock_bt._preset_mode = PRESET_NONE
        mock_bt.min_temp = mock_bt.bt_min_temp  # 5.0
        mock_bt.max_temp = mock_bt.bt_max_temp  # 30.0
        await self._call(mock_bt, **{ATTR_TEMPERATURE: 1.0})
        assert mock_bt.bt_target_temp == 5.0

    @pytest.mark.asyncio
    async def test_preset_none_stored_temp_updated(self, mock_bt):
        """In PRESET_NONE, stored temp is updated on manual change."""
        mock_bt._preset_mode = PRESET_NONE
        mock_bt.bt_target_temp = 20.0
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        await self._call(mock_bt, **{ATTR_TEMPERATURE: 23.0})
        assert mock_bt._preset_temperatures[PRESET_NONE] == 23.0

    @pytest.mark.asyncio
    async def test_off_mode_no_queue_put(self, mock_bt):
        """In OFF mode, queue.put is NOT called."""
        mock_bt._preset_mode = PRESET_NONE
        mock_bt.bt_hvac_mode = HVACMode.OFF
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        await self._call(mock_bt, **{ATTR_TEMPERATURE: 22.0})
        mock_bt.control_queue_task.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_maintenance_no_queue_put(self, mock_bt):
        """During maintenance, _control_needed_after_maintenance set, no queue.put."""
        mock_bt._preset_mode = PRESET_NONE
        mock_bt.bt_hvac_mode = HVACMode.HEAT
        mock_bt.in_maintenance = True
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        await self._call(mock_bt, **{ATTR_TEMPERATURE: 22.0})
        assert mock_bt._control_needed_after_maintenance is True
        mock_bt.control_queue_task.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_queue_put_called_in_heat_mode(self, mock_bt):
        """In HEAT mode, queue.put IS called."""
        mock_bt._preset_mode = PRESET_NONE
        mock_bt.bt_hvac_mode = HVACMode.HEAT
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        await self._call(mock_bt, **{ATTR_TEMPERATURE: 22.0})
        mock_bt.control_queue_task.put.assert_awaited_once_with(mock_bt)
