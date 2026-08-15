"""Baseline tests for climate.py.

Tests the 6 most important methods using unbound-method calls with a shared
mock_bt fixture (MagicMock with explicit attributes).
"""

from datetime import UTC, datetime, timedelta
import logging
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.climate.const import (
    ATTR_HVAC_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    PRESET_AWAY,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_NONE,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE
import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.helpers import InboundSetpoint
from custom_components.better_thermostat.utils.hvac_action import ToleranceHysteresis
from custom_components.better_thermostat.utils.thermal_learning import (
    HeatingPowerTracker,
    HeatLossTracker,
)

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
    bt.hvac_mode = HVACMode.HEAT
    bt.window_open = False
    bt.contact_open = False
    bt.ignore_states = False
    # Hysteresis
    bt._hysteresis = ToleranceHysteresis()
    # Thermal trackers (real objects – new thin-wrapper methods delegate to these)
    bt._heating_tracker = HeatingPowerTracker(
        heating_power=0.05, min_target=18.0, max_target=24.0
    )
    bt._loss_tracker = HeatLossTracker()
    bt.old_attr_hvac_action = None
    bt.attr_hvac_action = None
    bt.outdoor_sensor = None
    # Cooling channel: off unless a test configures one
    bt.cooler_entity_id = None
    bt._preset_cool_temperature = None
    # Thermal tracker property delegates
    type(bt).heating_power = property(
        lambda self: self._heating_tracker.heating_power,
        lambda self, v: setattr(self._heating_tracker, "heating_power", v),
    )
    type(bt).heating_power_normalized = property(
        lambda self: self._heating_tracker.normalized_power,
        lambda self, v: setattr(self._heating_tracker, "normalized_power", v),
    )
    type(bt).last_heating_power_stats = property(
        lambda self: self._heating_tracker.stats
    )
    type(bt).heating_cycles = property(lambda self: self._heating_tracker.cycles)
    type(bt).heat_loss_rate = property(
        lambda self: self._loss_tracker.heat_loss_rate,
        lambda self, v: setattr(self._loss_tracker, "heat_loss_rate", v),
    )
    type(bt).last_heat_loss_stats = property(lambda self: self._loss_tracker.stats)
    type(bt).loss_cycles = property(lambda self: self._loss_tracker.cycles)
    # Presets
    from custom_components.better_thermostat.utils.preset_manager import PresetManager

    bt.preset_mgr = PresetManager(
        temperatures={
            PRESET_NONE: 20.0,
            PRESET_COMFORT: 21.0,
            PRESET_ECO: 19.0,
            PRESET_AWAY: 16.0,
        },
        enabled_presets=[PRESET_COMFORT, PRESET_ECO, PRESET_AWAY],
    )
    bt.bt_update_lock = False
    # TRVs
    bt.real_trvs = {}
    # HA callbacks
    bt.control_queue_task = AsyncMock()
    bt.async_write_ha_state = MagicMock()
    bt.schedule_save_state = MagicMock()
    bt.in_maintenance = False
    bt._control_needed_after_maintenance = False
    # min_temp / max_temp
    bt.min_temp = bt.bt_min_temp
    bt.max_temp = bt.bt_max_temp
    # Real method bindings
    bt._should_heat_with_tolerance = lambda prev, tol: (
        BetterThermostat._should_heat_with_tolerance(bt, prev, tol)
    )
    bt._compute_hvac_action = lambda: BetterThermostat._compute_hvac_action(bt)
    bt._compute_hvac_action_pure = lambda: BetterThermostat._compute_hvac_action_pure(
        bt
    )
    bt._build_trv_snapshots = lambda: BetterThermostat._build_trv_snapshots(bt)
    bt._commit_hvac_action = lambda result: BetterThermostat._commit_hvac_action(
        bt, result
    )
    bt._get_outdoor_temp = lambda: BetterThermostat._get_outdoor_temp(bt)
    bt._enforce_cool_above_heat = lambda **kwargs: (
        BetterThermostat._enforce_cool_above_heat(bt, **kwargs)
    )
    bt._bound_target_to_range = lambda value: BetterThermostat._bound_target_to_range(
        bt, value
    )
    bt._seed_cool_target = lambda setpoint, entity_id: (
        BetterThermostat._seed_cool_target(bt, setpoint, entity_id)
    )
    return bt


# ===========================================================================
# 1. TestShouldHeatWithTolerance
# ===========================================================================


class TestShouldHeatWithTolerance:
    """Tests for _should_heat_with_tolerance."""

    def _call(self, bt, previous_action, tol):
        return BetterThermostat._should_heat_with_tolerance(bt, previous_action, tol)

    def test_target_temp_none(self, mock_bt):
        """Return False when target temp is None."""
        mock_bt.bt_target_temp = None
        assert self._call(mock_bt, HVACAction.IDLE, 0.5) is False

    def test_cur_temp_none(self, mock_bt):
        """Return False when current temp is None."""
        mock_bt.cur_temp = None
        assert self._call(mock_bt, HVACAction.IDLE, 0.5) is False

    def test_heating_cur_below_target(self, mock_bt):
        """Continue heating when current temp is below target."""
        mock_bt.cur_temp = 21.5
        mock_bt.bt_target_temp = 22.0
        assert self._call(mock_bt, HVACAction.HEATING, 0.5) is True

    def test_heating_cur_equals_target(self, mock_bt):
        """Stop heating when current temp equals target."""
        mock_bt.cur_temp = 22.0
        mock_bt.bt_target_temp = 22.0
        assert self._call(mock_bt, HVACAction.HEATING, 0.5) is False

    def test_heating_cur_above_target(self, mock_bt):
        """Stop heating when current temp exceeds target."""
        mock_bt.cur_temp = 22.5
        mock_bt.bt_target_temp = 22.0
        assert self._call(mock_bt, HVACAction.HEATING, 0.5) is False

    def test_idle_cur_below_threshold(self, mock_bt):
        """Start heating when idle and temp is below threshold."""
        mock_bt.cur_temp = 21.0
        mock_bt.bt_target_temp = 22.0
        assert self._call(mock_bt, HVACAction.IDLE, 0.5) is True

    def test_idle_cur_equals_threshold(self, mock_bt):
        """Stay idle when current temp equals threshold."""
        mock_bt.cur_temp = 21.5
        mock_bt.bt_target_temp = 22.0
        assert self._call(mock_bt, HVACAction.IDLE, 0.5) is False

    def test_idle_cur_above_threshold(self, mock_bt):
        """Stay idle when current temp is above threshold."""
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
    """Tests for _compute_hvac_action."""

    def _call(self, bt):
        return BetterThermostat._compute_hvac_action(bt)

    def test_target_temp_none_returns_idle(self, mock_bt):
        """Return IDLE when target temp is None."""
        mock_bt.bt_target_temp = None
        assert self._call(mock_bt) == HVACAction.IDLE

    def test_cur_temp_none_returns_idle(self, mock_bt):
        """Return IDLE when current temp is None."""
        mock_bt.cur_temp = None
        assert self._call(mock_bt) == HVACAction.IDLE

    def test_hvac_mode_off_returns_off(self, mock_bt):
        """Return OFF when HVAC mode is OFF."""
        mock_bt.hvac_mode = HVACMode.OFF
        assert self._call(mock_bt) == HVACAction.OFF

    def test_bt_hvac_mode_off_returns_off(self, mock_bt):
        """Return OFF when BT HVAC mode is OFF."""
        mock_bt.bt_hvac_mode = HVACMode.OFF
        assert self._call(mock_bt) == HVACAction.OFF

    def test_window_open_returns_idle(self, mock_bt):
        """Return IDLE when window is open."""
        mock_bt.window_open = True
        mock_bt.contact_open = True
        assert self._call(mock_bt) == HVACAction.IDLE

    def test_heat_mode_cur_below_threshold(self, mock_bt):
        """HEAT mode, cur < target - tol → HEATING."""
        mock_bt.cur_temp = 21.0
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        assert self._call(mock_bt) == HVACAction.HEATING

    def test_heat_mode_cur_at_target(self, mock_bt):
        """HEAT mode, cur >= target → IDLE."""
        mock_bt.cur_temp = 22.0
        mock_bt.bt_target_temp = 22.0
        mock_bt._hysteresis.last_action = HVACAction.IDLE
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
        """TRV reports hvac_action='heating' in band → override to HEATING."""
        mock_bt.cur_temp = 21.7  # in band: target-tol(21.5) < cur < target(22.0)
        mock_bt.bt_target_temp = 22.0
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt.real_trvs = {
            "climate.trv1": Trv.from_legacy_dict(
                "climate.trv1", {"hvac_action": "heating"}
            )
        }
        assert self._call(mock_bt) == HVACAction.HEATING

    def test_trv_override_valve_position(self, mock_bt):
        """TRV valve_position=50 in band → override to HEATING."""
        mock_bt.cur_temp = 21.7
        mock_bt.bt_target_temp = 22.0
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt.real_trvs = {
            "climate.trv1": Trv.from_legacy_dict("climate.trv1", {"valve_position": 50})
        }
        assert self._call(mock_bt) == HVACAction.HEATING

    def test_trv_override_last_valve_percent_0_1_range(self, mock_bt):
        """TRV last_valve_percent=0.8 (0-1 range) → normalized to 80% → HEATING."""
        mock_bt.cur_temp = 21.7
        mock_bt.bt_target_temp = 22.0
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt.real_trvs = {
            "climate.trv1": Trv.from_legacy_dict(
                "climate.trv1", {"last_valve_percent": 0.8}
            )
        }
        assert self._call(mock_bt) == HVACAction.HEATING

    def test_trv_override_suppressed_above_target(self, mock_bt):
        """Above target with TRV still reporting heating → action stays IDLE."""
        mock_bt.cur_temp = 22.3  # above target → BT has decided IDLE
        mock_bt.bt_target_temp = 22.0
        mock_bt._hysteresis.last_action = HVACAction.HEATING
        mock_bt.real_trvs = {
            "climate.trv1": Trv.from_legacy_dict(
                "climate.trv1", {"hvac_action": "heating"}
            )
        }
        assert self._call(mock_bt) == HVACAction.IDLE

    def test_ignore_states_no_trv_override(self, mock_bt):
        """ignore_states=True in band → TRV override still skipped, returns IDLE."""
        mock_bt.cur_temp = 21.7
        mock_bt.bt_target_temp = 22.0
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt.ignore_states = True
        mock_bt.real_trvs = {
            "climate.trv1": Trv.from_legacy_dict(
                "climate.trv1", {"hvac_action": "heating"}
            )
        }
        assert self._call(mock_bt) == HVACAction.IDLE

    def test_ignore_trv_states_per_trv(self, mock_bt):
        """ignore_trv_states=True on specific TRV in band → that TRV is skipped."""
        mock_bt.cur_temp = 21.7
        mock_bt.bt_target_temp = 22.0
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt.real_trvs = {
            "climate.trv1": Trv.from_legacy_dict(
                "climate.trv1", {"hvac_action": "heating", "ignore_trv_states": True}
            )
        }
        assert self._call(mock_bt) == HVACAction.IDLE

    def test_tolerance_decision_saved_before_trv_override(self, mock_bt):
        """Hysteresis state uses tolerance decision, not TRV-overridden action."""
        mock_bt.cur_temp = 21.7  # in band → tolerance says IDLE
        mock_bt.bt_target_temp = 22.0
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt.real_trvs = {
            "climate.trv1": Trv.from_legacy_dict(
                "climate.trv1", {"hvac_action": "heating"}
            )
        }
        self._call(mock_bt)
        # Tolerance last action should be IDLE (tolerance decision), not HEATING
        assert mock_bt._hysteresis.last_action == HVACAction.IDLE

    def test_tolerance_hold_active_set(self, mock_bt):
        """_tolerance_hold_active is True when tolerance says no-heat but not cooling."""
        mock_bt.cur_temp = 21.8  # in band: target-tol(21.5) < cur < target(22.0)
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        self._call(mock_bt)
        assert mock_bt._hysteresis.hold_active is True


# ===========================================================================
# 3. TestCalculateHeatingPower
# ===========================================================================


class TestCalculateHeatingPower:
    """Tests for calculate_heating_power."""

    async def _call(self, bt):
        return await BetterThermostat.calculate_heating_power(bt)

    @pytest.mark.asyncio
    async def test_cur_temp_none_early_return(self, mock_bt):
        """Skip update when current temp is None."""
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
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt.old_attr_hvac_action = HVACAction.IDLE
        mock_bt._heating_tracker._prev_action = HVACAction.IDLE
        # Make _compute_hvac_action return HEATING
        mock_bt.hvac_mode = HVACMode.HEAT
        mock_bt.bt_hvac_mode = HVACMode.HEAT
        mock_bt.window_open = False
        mock_bt.contact_open = False
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        with patch("custom_components.better_thermostat.climate.dt_util") as mock_dt:
            now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
            mock_dt.utcnow.return_value = now
            await self._call(mock_bt)

        assert mock_bt._heating_tracker.start_temp == 20.0
        assert mock_bt._heating_tracker.start_ts == now

    @pytest.mark.asyncio
    async def test_heating_stop_sets_end(self, mock_bt):
        """Transition from HEATING → IDLE sets end_temp/timestamp."""
        now = datetime(2025, 1, 1, 12, 10, 0, tzinfo=UTC)
        mock_bt.cur_temp = 22.0
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._hysteresis.last_action = HVACAction.HEATING
        mock_bt.old_attr_hvac_action = HVACAction.HEATING
        mock_bt._heating_tracker._prev_action = HVACAction.HEATING
        mock_bt._heating_tracker.start_temp = 20.0
        mock_bt._heating_tracker.start_ts = now - timedelta(minutes=10)
        mock_bt._heating_tracker.end_temp = None
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        with patch("custom_components.better_thermostat.climate.dt_util") as mock_dt:
            mock_dt.utcnow.return_value = now
            await self._call(mock_bt)

        assert mock_bt._heating_tracker.end_temp == 22.0
        assert mock_bt._heating_tracker.end_ts == now

    @pytest.mark.asyncio
    async def test_peak_tracking(self, mock_bt):
        """Temperature still rising after heating stopped → end_temp updated."""
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        mock_bt.cur_temp = 22.5  # above previous end_temp
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt.old_attr_hvac_action = HVACAction.IDLE
        mock_bt._heating_tracker._prev_action = HVACAction.IDLE
        mock_bt._heating_tracker.start_temp = 20.0
        mock_bt._heating_tracker.start_ts = base - timedelta(minutes=15)
        mock_bt._heating_tracker.end_temp = 22.0
        mock_bt._heating_tracker.end_ts = base - timedelta(minutes=5)
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        with patch("custom_components.better_thermostat.climate.dt_util") as mock_dt:
            mock_dt.utcnow.return_value = base
            await self._call(mock_bt)

        assert mock_bt._heating_tracker.end_temp == 22.5

    @pytest.mark.asyncio
    async def test_finalization_on_temp_drop(self, mock_bt):
        """Temperature falls below peak → cycle finalized, power updated."""
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        mock_bt.cur_temp = 21.8  # below peak of 22.5
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt.old_attr_hvac_action = HVACAction.IDLE
        mock_bt._heating_tracker._prev_action = HVACAction.IDLE
        mock_bt._heating_tracker.start_temp = 20.0
        mock_bt._heating_tracker.start_ts = base - timedelta(minutes=10)
        mock_bt._heating_tracker.end_temp = 22.5
        mock_bt._heating_tracker.end_ts = base - timedelta(minutes=2)
        mock_bt.heating_power = 0.05
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        with patch("custom_components.better_thermostat.climate.dt_util") as mock_dt:
            mock_dt.utcnow.return_value = base
            await self._call(mock_bt)

        # Cycle reset after finalization
        assert mock_bt._heating_tracker.start_temp is None
        assert mock_bt._heating_tracker.end_temp is None
        # Power was updated (EMA smoothing)
        assert mock_bt.heating_power != 0.05
        assert len(mock_bt.last_heating_power_stats) == 1

    @pytest.mark.asyncio
    async def test_finalization_on_timeout(self, mock_bt):
        """30-minute timeout triggers finalization even without temp drop."""
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        mock_bt.cur_temp = 22.5  # still at peak (no drop)
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt.old_attr_hvac_action = HVACAction.IDLE
        mock_bt._heating_tracker._prev_action = HVACAction.IDLE
        mock_bt._heating_tracker.start_temp = 20.0
        mock_bt._heating_tracker.start_ts = base - timedelta(minutes=40)
        mock_bt._heating_tracker.end_temp = 22.5
        mock_bt._heating_tracker.end_ts = base - timedelta(minutes=31)
        mock_bt.heating_power = 0.05
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        with patch("custom_components.better_thermostat.climate.dt_util") as mock_dt:
            mock_dt.utcnow.return_value = base
            await self._call(mock_bt)

        assert mock_bt._heating_tracker.start_temp is None
        assert len(mock_bt.last_heating_power_stats) == 1

    @pytest.mark.asyncio
    async def test_short_cycle_discarded(self, mock_bt):
        """Cycles shorter than 1 minute are discarded."""
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        mock_bt.cur_temp = 21.8
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt.old_attr_hvac_action = HVACAction.IDLE
        mock_bt._heating_tracker._prev_action = HVACAction.IDLE
        mock_bt._heating_tracker.start_temp = 20.0
        mock_bt._heating_tracker.start_ts = base - timedelta(seconds=30)  # 0.5 min
        mock_bt._heating_tracker.end_temp = 22.5
        mock_bt._heating_tracker.end_ts = base - timedelta(seconds=5)
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
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        mock_bt.cur_temp = 19.0  # below peak → finalize
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt.old_attr_hvac_action = HVACAction.IDLE
        mock_bt._heating_tracker._prev_action = HVACAction.IDLE
        mock_bt._heating_tracker.start_temp = 21.0
        mock_bt._heating_tracker.start_ts = base - timedelta(minutes=10)
        mock_bt._heating_tracker.end_temp = 20.0  # end < start → negative diff
        mock_bt._heating_tracker.end_ts = base - timedelta(minutes=2)
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
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        mock_bt.cur_temp = (
            21.8  # above tol threshold (21.5) so action=IDLE, below end_temp
        )
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt.old_attr_hvac_action = HVACAction.IDLE
        mock_bt._heating_tracker._prev_action = HVACAction.IDLE
        mock_bt._heating_tracker.start_temp = 20.0
        mock_bt._heating_tracker.start_ts = base - timedelta(minutes=10)
        mock_bt._heating_tracker.end_temp = 22.0
        mock_bt._heating_tracker.end_ts = base - timedelta(minutes=2)
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
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        mock_bt.cur_temp = 21.8  # above tol threshold so action=IDLE, below end_temp
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt.old_attr_hvac_action = HVACAction.IDLE
        mock_bt._heating_tracker._prev_action = HVACAction.IDLE
        mock_bt._heating_tracker.start_temp = 20.0
        mock_bt._heating_tracker.start_ts = base - timedelta(minutes=10)
        mock_bt._heating_tracker.end_temp = 22.0
        mock_bt._heating_tracker.end_ts = base - timedelta(minutes=2)
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
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        mock_bt.cur_temp = 21.8  # above tol threshold so action=IDLE, below end_temp
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt.old_attr_hvac_action = HVACAction.IDLE
        mock_bt._heating_tracker._prev_action = HVACAction.IDLE
        mock_bt._heating_tracker.start_temp = 20.0
        mock_bt._heating_tracker.start_ts = base - timedelta(minutes=10)
        mock_bt._heating_tracker.end_temp = 22.0
        mock_bt._heating_tracker.end_ts = base - timedelta(minutes=2)
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
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        mock_bt.cur_temp = 21.8  # above tol threshold so action=IDLE, below end_temp
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt.old_attr_hvac_action = HVACAction.IDLE
        mock_bt._heating_tracker._prev_action = HVACAction.IDLE
        mock_bt._heating_tracker.start_temp = 20.0
        mock_bt._heating_tracker.start_ts = base - timedelta(minutes=10)
        mock_bt._heating_tracker.end_temp = 22.0
        mock_bt._heating_tracker.end_ts = base - timedelta(minutes=2)
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
    """Tests for calculate_heat_loss."""

    async def _call(self, bt):
        return await BetterThermostat.calculate_heat_loss(bt)

    @pytest.mark.asyncio
    async def test_cur_temp_none_early_return(self, mock_bt):
        """Skip update when current temp is None."""
        mock_bt.cur_temp = None
        await self._call(mock_bt)
        assert mock_bt._loss_tracker.start_temp is None

    @pytest.mark.asyncio
    async def test_window_open_resets_tracking(self, mock_bt):
        """Window open → all tracking values reset."""
        mock_bt.window_open = True
        mock_bt.contact_open = True
        mock_bt._loss_tracker.start_temp = 21.0
        mock_bt._loss_tracker.start_ts = datetime(2025, 1, 1, tzinfo=UTC)
        mock_bt._loss_tracker.end_temp = 20.5
        mock_bt._loss_tracker.end_ts = datetime(2025, 1, 1, tzinfo=UTC)
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        with patch("custom_components.better_thermostat.climate.dt_util") as mock_dt:
            mock_dt.utcnow.return_value = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
            await self._call(mock_bt)

        assert mock_bt._loss_tracker.start_temp is None
        assert mock_bt._loss_tracker.end_temp is None

    @pytest.mark.asyncio
    async def test_idle_starts_tracking(self, mock_bt):
        """Entering IDLE starts tracking (loss_start_temp set)."""
        mock_bt.cur_temp = 22.0
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt._loss_tracker.start_temp = None
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        with patch("custom_components.better_thermostat.climate.dt_util") as mock_dt:
            now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
            mock_dt.utcnow.return_value = now
            await self._call(mock_bt)

        assert mock_bt._loss_tracker.start_temp == 22.0
        assert mock_bt._loss_tracker.start_ts == now

    @pytest.mark.asyncio
    async def test_tracks_lowest_temp(self, mock_bt):
        """While idle, end_temp tracks the lowest temperature."""
        now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        # cur_temp must yield IDLE (>= target - tol) AND be below loss_end_temp
        mock_bt.cur_temp = 21.6
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5  # threshold = 21.5, 21.6 >= 21.5 → IDLE
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt._loss_tracker.start_temp = 22.0
        mock_bt._loss_tracker.start_ts = now - timedelta(minutes=10)
        mock_bt._loss_tracker.end_temp = 21.8  # current (21.6) is lower
        mock_bt._loss_tracker.end_ts = now - timedelta(minutes=5)

        with patch("custom_components.better_thermostat.climate.dt_util") as mock_dt:
            mock_dt.utcnow.return_value = now
            await self._call(mock_bt)

        assert mock_bt._loss_tracker.end_temp == 21.6

    @pytest.mark.asyncio
    async def test_finalization_on_heating_restart(self, mock_bt):
        """Heating starts again → cycle finalized, heat_loss updated."""
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        # Set up a completed idle period
        mock_bt.cur_temp = 20.0  # below target-tol → HEATING
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt._loss_tracker.start_temp = 22.0
        mock_bt._loss_tracker.start_ts = base - timedelta(minutes=10)
        mock_bt._loss_tracker.end_temp = 20.5
        mock_bt._loss_tracker.end_ts = base - timedelta(minutes=2)
        mock_bt.heat_loss_rate = 0.01
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        with patch("custom_components.better_thermostat.climate.dt_util") as mock_dt:
            mock_dt.utcnow.return_value = base
            await self._call(mock_bt)

        # Cycle finalized (reset)
        assert mock_bt._loss_tracker.start_temp is None
        assert mock_bt._loss_tracker.end_temp is None
        assert len(mock_bt.last_heat_loss_stats) == 1

    @pytest.mark.asyncio
    async def test_short_loss_cycle_discarded(self, mock_bt):
        """Loss cycles shorter than 1 minute are discarded."""
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        mock_bt.cur_temp = 20.0
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt._loss_tracker.start_temp = 22.0
        mock_bt._loss_tracker.start_ts = base - timedelta(seconds=30)
        mock_bt._loss_tracker.end_temp = 21.0
        mock_bt._loss_tracker.end_ts = base - timedelta(seconds=10)
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
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        mock_bt.cur_temp = 20.0
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt._loss_tracker.start_temp = 22.0
        mock_bt._loss_tracker.start_ts = base - timedelta(minutes=10)
        mock_bt._loss_tracker.end_temp = 20.0  # 2°C drop in 10 min
        mock_bt._loss_tracker.end_ts = base - timedelta(minutes=2)
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
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        mock_bt.cur_temp = 20.0
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt._loss_tracker.start_temp = 22.0
        mock_bt._loss_tracker.start_ts = base - timedelta(minutes=5)
        mock_bt._loss_tracker.end_temp = 20.0
        mock_bt._loss_tracker.end_ts = base - timedelta(minutes=2)
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
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        mock_bt.cur_temp = 20.0
        mock_bt.bt_target_temp = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt._loss_tracker.start_temp = 22.0
        mock_bt._loss_tracker.start_ts = base - timedelta(minutes=10)
        mock_bt._loss_tracker.end_temp = 20.5
        mock_bt._loss_tracker.end_ts = base - timedelta(minutes=2)
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
    """Tests for async_set_preset_mode."""

    async def _call(self, bt, preset_mode):
        return await BetterThermostat.async_set_preset_mode(bt, preset_mode)

    @pytest.mark.asyncio
    async def test_invalid_preset_no_change(self, mock_bt):
        """Invalid preset → warning, no state change."""
        # preset_modes returns [PRESET_NONE] + _enabled_presets
        mock_bt.preset_modes = [PRESET_NONE, PRESET_COMFORT, PRESET_ECO, PRESET_AWAY]
        old_preset = mock_bt.preset_mgr.mode
        old_temp = mock_bt.bt_target_temp
        await self._call(mock_bt, "nonexistent")
        assert mock_bt.preset_mgr.mode == old_preset
        assert mock_bt.bt_target_temp == old_temp

    @pytest.mark.asyncio
    async def test_none_to_comfort(self, mock_bt):
        """NONE → Comfort: saves current temp, applies configured comfort temp."""
        mock_bt.preset_modes = [PRESET_NONE, PRESET_COMFORT, PRESET_ECO, PRESET_AWAY]
        mock_bt.preset_mgr.mode = PRESET_NONE
        mock_bt.bt_target_temp = 20.0
        mock_bt.preset_mgr.saved_temperature = None
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        await self._call(mock_bt, PRESET_COMFORT)
        assert mock_bt.preset_mgr.mode == PRESET_COMFORT
        assert mock_bt.preset_mgr.saved_temperature == 20.0  # saved original
        assert mock_bt.bt_target_temp == 21.0  # configured comfort temp

    @pytest.mark.asyncio
    async def test_comfort_to_none_restores(self, mock_bt):
        """Comfort → NONE: bt_target_temp restored, _preset_temperature cleared."""
        mock_bt.preset_modes = [PRESET_NONE, PRESET_COMFORT, PRESET_ECO, PRESET_AWAY]
        mock_bt.preset_mgr.mode = PRESET_COMFORT
        mock_bt.preset_mgr.saved_temperature = 20.0
        mock_bt.bt_target_temp = 21.0
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        await self._call(mock_bt, PRESET_NONE)
        assert mock_bt.preset_mgr.mode == PRESET_NONE
        assert mock_bt.bt_target_temp == 20.0  # restored
        assert mock_bt.preset_mgr.saved_temperature is None

    @pytest.mark.asyncio
    async def test_comfort_to_eco(self, mock_bt):
        """Comfort → Eco: bt_target_temp = eco config, _preset_temperature unchanged."""
        mock_bt.preset_modes = [PRESET_NONE, PRESET_COMFORT, PRESET_ECO, PRESET_AWAY]
        mock_bt.preset_mgr.mode = PRESET_COMFORT
        mock_bt.preset_mgr.saved_temperature = 20.0  # saved from initial manual temp
        mock_bt.bt_target_temp = 21.0
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        await self._call(mock_bt, PRESET_ECO)
        assert mock_bt.preset_mgr.mode == PRESET_ECO
        assert mock_bt.bt_target_temp == 19.0  # eco configured
        assert mock_bt.preset_mgr.saved_temperature == 20.0  # still saved

    @pytest.mark.asyncio
    async def test_eco_to_none_restores_original(self, mock_bt):
        """Eco → NONE: bt_target_temp = saved original temp."""
        mock_bt.preset_modes = [PRESET_NONE, PRESET_COMFORT, PRESET_ECO, PRESET_AWAY]
        mock_bt.preset_mgr.mode = PRESET_ECO
        mock_bt.preset_mgr.saved_temperature = 20.0
        mock_bt.bt_target_temp = 19.0
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        await self._call(mock_bt, PRESET_NONE)
        assert mock_bt.bt_target_temp == 20.0

    @pytest.mark.asyncio
    async def test_manual_cool_temp_preserved_across_preset(self, mock_bt):
        """NONE→Comfort→NONE restores the manual cooling target, not the preset's."""
        mock_bt.preset_modes = [PRESET_NONE, PRESET_COMFORT, PRESET_ECO, PRESET_AWAY]
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.cooler_entity_id = "switch.ac"
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        mock_bt._preset_cool_temperatures = {
            PRESET_NONE: 24.0,
            PRESET_COMFORT: 24.0,
            PRESET_ECO: 27.0,
            PRESET_AWAY: 28.0,
        }
        mock_bt._preset_cool_temperature = None
        mock_bt.preset_mgr.mode = PRESET_NONE
        mock_bt.bt_target_temp = 20.0
        mock_bt.bt_target_cooltemp = 26.0  # manual cooling target
        # Entering a preset stashes the manual cool target and applies the preset's.
        await self._call(mock_bt, PRESET_COMFORT)
        assert mock_bt._preset_cool_temperature == 26.0
        assert mock_bt.bt_target_cooltemp == 24.0
        # Returning to NONE restores the manual cool target, not Comfort's.
        await self._call(mock_bt, PRESET_NONE)
        assert mock_bt.bt_target_cooltemp == 26.0

    @pytest.mark.asyncio
    async def test_restored_manual_cool_target_is_ordered_while_off(self, mock_bt):
        """Returning to PRESET_NONE while off still orders the pair.

        The manual cooling target is re-injected, not chosen: nothing looks at
        the pair again before the first cooling cycle, because the mode change
        that enables cooling does not re-enforce the ordering.
        """
        mock_bt.preset_modes = [PRESET_NONE, PRESET_COMFORT, PRESET_ECO, PRESET_AWAY]
        mock_bt.bt_hvac_mode = HVACMode.OFF
        mock_bt.hvac_mode = HVACMode.OFF
        mock_bt.cooler_entity_id = "switch.ac"
        mock_bt.bt_min_temp = 16.0
        mock_bt.bt_max_temp = 30.0
        mock_bt.min_temp = 16.0
        mock_bt.max_temp = 30.0
        mock_bt.bt_target_temp_step = 0.5
        mock_bt.preset_mgr.mode = PRESET_COMFORT
        mock_bt.preset_mgr.saved_temperature = None
        mock_bt._preset_cool_temperatures = {PRESET_NONE: 24.0, PRESET_COMFORT: 26.0}
        mock_bt._preset_cool_temperature = 22.0
        mock_bt.bt_target_temp = 24.0
        mock_bt.bt_target_cooltemp = 26.0

        await self._call(mock_bt, PRESET_NONE)

        assert mock_bt.bt_target_temp == 24.0
        assert mock_bt.bt_target_cooltemp == 24.5
        assert mock_bt.bt_min_temp <= mock_bt.bt_target_temp <= mock_bt.bt_max_temp
        assert mock_bt.bt_min_temp <= mock_bt.bt_target_cooltemp <= mock_bt.bt_max_temp
        assert mock_bt.bt_target_cooltemp > mock_bt.bt_target_temp

    @pytest.mark.asyncio
    async def test_restored_manual_cool_target_above_the_maximum_is_bounded(
        self, mock_bt
    ):
        """A stashed cooling target over the maximum comes back bounded.

        The ordering leaves it alone because it already clears the heating
        target, so the range bound is the only thing that holds it.
        """
        mock_bt.preset_modes = [PRESET_NONE, PRESET_COMFORT, PRESET_ECO, PRESET_AWAY]
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.cooler_entity_id = "switch.ac"
        mock_bt.bt_min_temp = 16.0
        mock_bt.bt_max_temp = 26.0
        mock_bt.min_temp = 16.0
        mock_bt.max_temp = 26.0
        mock_bt.preset_mgr.mode = PRESET_COMFORT
        mock_bt.preset_mgr.saved_temperature = None
        mock_bt._preset_cool_temperatures = {PRESET_NONE: 24.0, PRESET_COMFORT: 25.0}
        mock_bt._preset_cool_temperature = 35.0
        mock_bt.bt_target_temp = 20.0
        mock_bt.bt_target_cooltemp = 25.0

        await self._call(mock_bt, PRESET_NONE)

        assert mock_bt.bt_target_cooltemp == 26.0
        assert mock_bt.bt_min_temp <= mock_bt.bt_target_cooltemp <= mock_bt.bt_max_temp
        assert mock_bt.bt_target_cooltemp > mock_bt.bt_target_temp

    @pytest.mark.asyncio
    async def test_preset_temp_clamped_to_max(self, mock_bt):
        """Preset temp above max → clamped to max_temp."""
        mock_bt.preset_modes = [PRESET_NONE, PRESET_COMFORT, PRESET_ECO, PRESET_AWAY]
        mock_bt.preset_mgr.mode = PRESET_NONE
        mock_bt.preset_mgr.temperatures[PRESET_COMFORT] = 35.0  # above max
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
    """Tests for async_set_temperature."""

    async def _call(self, bt, **kwargs):
        return await BetterThermostat.async_set_temperature(bt, **kwargs)

    @pytest.mark.asyncio
    async def test_simple_setpoint(self, mock_bt):
        """Simple temperature set: {ATTR_TEMPERATURE: 22.0}."""
        mock_bt.preset_mgr.mode = PRESET_NONE
        mock_bt.bt_hvac_mode = HVACMode.HEAT
        await self._call(mock_bt, **{ATTR_TEMPERATURE: 22.0})
        assert mock_bt.bt_target_temp == 22.0

    @pytest.mark.asyncio
    async def test_hvac_mode_change_in_kwargs(self, mock_bt):
        """HVAC mode change passed in kwargs."""
        mock_bt.preset_mgr.mode = PRESET_NONE
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
        mock_bt.preset_mgr.mode = PRESET_NONE
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        await self._call(
            mock_bt, **{ATTR_TARGET_TEMP_LOW: 20.0, ATTR_TARGET_TEMP_HIGH: 26.0}
        )
        assert mock_bt.bt_target_temp == 20.0
        assert mock_bt.bt_target_cooltemp == 26.0

    @pytest.mark.asyncio
    async def test_cool_target_enforced_above_heat(self, mock_bt):
        """Cool target adjusted to be above heat target in HEAT_COOL mode."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_hvac_mode = HVACMode.HEAT_COOL
        mock_bt.preset_mgr.mode = PRESET_NONE
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        mock_bt.bt_target_temp = 22.0
        mock_bt.bt_target_cooltemp = 20.0  # below heat target → should be adjusted
        await self._call(mock_bt, **{ATTR_TEMPERATURE: 22.0})
        assert mock_bt.bt_target_cooltemp > mock_bt.bt_target_temp

    @pytest.mark.asyncio
    async def test_min_max_clamping(self, mock_bt):
        """Temperature clamped to min/max bounds."""
        mock_bt.preset_mgr.mode = PRESET_NONE
        mock_bt.min_temp = mock_bt.bt_min_temp  # 5.0
        mock_bt.max_temp = mock_bt.bt_max_temp  # 30.0
        await self._call(mock_bt, **{ATTR_TEMPERATURE: 50.0})
        assert mock_bt.bt_target_temp == 30.0

    @pytest.mark.asyncio
    async def test_min_clamping(self, mock_bt):
        """Temperature below min → clamped to min."""
        mock_bt.preset_mgr.mode = PRESET_NONE
        mock_bt.min_temp = mock_bt.bt_min_temp  # 5.0
        mock_bt.max_temp = mock_bt.bt_max_temp  # 30.0
        await self._call(mock_bt, **{ATTR_TEMPERATURE: 1.0})
        assert mock_bt.bt_target_temp == 5.0

    @pytest.mark.asyncio
    async def test_preset_none_stored_temp_updated(self, mock_bt):
        """In PRESET_NONE, stored temp is updated on manual change."""
        mock_bt.preset_mgr.mode = PRESET_NONE
        mock_bt.bt_target_temp = 20.0
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        await self._call(mock_bt, **{ATTR_TEMPERATURE: 23.0})
        assert mock_bt.preset_mgr.temperatures[PRESET_NONE] == 23.0

    @pytest.mark.asyncio
    async def test_off_mode_no_queue_put(self, mock_bt):
        """In OFF mode, queue.put is NOT called."""
        mock_bt.preset_mgr.mode = PRESET_NONE
        mock_bt.bt_hvac_mode = HVACMode.OFF
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        await self._call(mock_bt, **{ATTR_TEMPERATURE: 22.0})
        mock_bt.control_queue_task.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_maintenance_no_queue_put(self, mock_bt):
        """During maintenance, _control_needed_after_maintenance set, no queue.put."""
        mock_bt.preset_mgr.mode = PRESET_NONE
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
        mock_bt.preset_mgr.mode = PRESET_NONE
        mock_bt.bt_hvac_mode = HVACMode.HEAT
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        await self._call(mock_bt, **{ATTR_TEMPERATURE: 22.0})
        mock_bt.control_queue_task.put.assert_awaited_once_with(mock_bt)

    @pytest.mark.asyncio
    async def test_active_preset_deactivated_on_manual_change(self, mock_bt):
        """Changing target temp while a preset is active deactivates it (back to NONE)."""
        mock_bt.preset_mgr.mode = PRESET_COMFORT
        mock_bt.preset_mgr.saved_temperature = 20.0
        mock_bt.preset_mgr.temperatures[PRESET_COMFORT] = 21.0
        mock_bt.bt_target_temp = 21.0
        mock_bt.bt_hvac_mode = HVACMode.HEAT
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        await self._call(mock_bt, **{ATTR_TEMPERATURE: 19.0})
        assert mock_bt.preset_mgr.mode == PRESET_NONE
        assert mock_bt.preset_mgr.saved_temperature is None
        assert mock_bt.bt_target_temp == 19.0
        # New manual value is stored as the PRESET_NONE temperature
        assert mock_bt.preset_mgr.temperatures[PRESET_NONE] == 19.0
        # Stored Comfort preset temperature is left untouched
        assert mock_bt.preset_mgr.temperatures[PRESET_COMFORT] == 21.0

    @pytest.mark.asyncio
    async def test_active_preset_kept_when_new_temp_matches_stored(self, mock_bt):
        """Setting temp to the preset's stored value (e.g. from its Number entity) keeps the preset active."""
        mock_bt.preset_mgr.mode = PRESET_COMFORT
        mock_bt.preset_mgr.saved_temperature = 20.0
        mock_bt.preset_mgr.temperatures[PRESET_COMFORT] = 22.5
        mock_bt.bt_target_temp = 21.0
        mock_bt.bt_hvac_mode = HVACMode.HEAT
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        await self._call(mock_bt, **{ATTR_TEMPERATURE: 22.5})
        assert mock_bt.preset_mgr.mode == PRESET_COMFORT
        assert mock_bt.preset_mgr.saved_temperature == 20.0
        assert mock_bt.bt_target_temp == 22.5

    @pytest.mark.asyncio
    async def test_preset_none_change_does_not_trigger_deactivation_path(self, mock_bt):
        """In PRESET_NONE, the deactivation branch is skipped and stored temp is updated."""
        mock_bt.preset_mgr.mode = PRESET_NONE
        mock_bt.preset_mgr.saved_temperature = None
        mock_bt.bt_target_temp = 20.0
        mock_bt.bt_hvac_mode = HVACMode.HEAT
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        await self._call(mock_bt, **{ATTR_TEMPERATURE: 23.0})
        assert mock_bt.preset_mgr.mode == PRESET_NONE
        assert mock_bt.preset_mgr.saved_temperature is None
        assert mock_bt.preset_mgr.temperatures[PRESET_NONE] == 23.0


# ===========================================================================
# 7. TestEnforceCoolAboveHeat
# ===========================================================================


class TestEnforceCoolAboveHeat:
    """_enforce_cool_above_heat keeps the cool target strictly above the heat target."""

    def _call(self, bt):
        return BetterThermostat._enforce_cool_above_heat(bt)

    def test_not_heat_cool_mode_is_noop(self, mock_bt):
        """Outside HEAT_COOL the cool target is left untouched even if below heat."""
        mock_bt.hvac_mode = HVACMode.HEAT
        mock_bt.bt_target_temp = 22.0
        mock_bt.bt_target_cooltemp = 20.0
        self._call(mock_bt)
        assert mock_bt.bt_target_cooltemp == 20.0

    def test_cool_above_heat_is_noop(self, mock_bt):
        """A cool target already above the heat target is unchanged."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_target_temp = 22.0
        mock_bt.bt_target_cooltemp = 26.0
        self._call(mock_bt)
        assert mock_bt.bt_target_cooltemp == 26.0

    def test_cool_below_heat_is_bumped_by_step(self, mock_bt):
        """A cool target below the heat target is bumped up by one step."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_target_temp = 22.0
        mock_bt.bt_target_temp_step = 0.5
        mock_bt.bt_target_cooltemp = 20.0
        self._call(mock_bt)
        assert mock_bt.bt_target_cooltemp == 22.5

    def test_cool_equal_heat_is_bumped(self, mock_bt):
        """A cool target equal to the heat target is bumped above it."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_target_temp = 22.0
        mock_bt.bt_target_temp_step = 0.5
        mock_bt.bt_target_cooltemp = 22.0
        self._call(mock_bt)
        assert mock_bt.bt_target_cooltemp == 22.5

    def test_step_falls_back_to_half_degree(self, mock_bt):
        """A missing/zero step falls back to 0.5."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_target_temp = 22.0
        mock_bt.bt_target_temp_step = 0
        mock_bt.bt_target_cooltemp = 21.0
        self._call(mock_bt)
        assert mock_bt.bt_target_cooltemp == 22.5

    def test_none_cool_target_is_noop(self, mock_bt):
        """A None cool target does not raise and stays None."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_target_temp = 22.0
        mock_bt.bt_target_cooltemp = None
        self._call(mock_bt)
        assert mock_bt.bt_target_cooltemp is None

    def test_regardless_of_hvac_mode_bumps_outside_heat_cool(self, mock_bt):
        """The mode gate is skipped on request so an off BT is ordered too."""
        mock_bt.hvac_mode = HVACMode.OFF
        mock_bt.bt_target_temp = 22.0
        mock_bt.bt_target_temp_step = 0.5
        mock_bt.bt_target_cooltemp = 20.0
        BetterThermostat._enforce_cool_above_heat(mock_bt, regardless_of_hvac_mode=True)
        assert mock_bt.bt_target_cooltemp == 22.5

    def test_regardless_of_hvac_mode_still_needs_both_targets(self, mock_bt):
        """Skipping the mode gate does not invent a cool target out of None."""
        mock_bt.hvac_mode = HVACMode.OFF
        mock_bt.bt_target_temp = 22.0
        mock_bt.bt_target_cooltemp = None
        BetterThermostat._enforce_cool_above_heat(mock_bt, regardless_of_hvac_mode=True)
        assert mock_bt.bt_target_cooltemp is None

    def test_regardless_of_hvac_mode_still_needs_a_heat_target(self, mock_bt):
        """Without a heat target there is nothing to order the cool target against."""
        mock_bt.hvac_mode = HVACMode.OFF
        mock_bt.bt_target_temp = None
        mock_bt.bt_target_cooltemp = 20.0
        BetterThermostat._enforce_cool_above_heat(mock_bt, regardless_of_hvac_mode=True)
        assert mock_bt.bt_target_cooltemp == 20.0

    def test_regardless_of_hvac_mode_keeps_an_already_ordered_pair(self, mock_bt):
        """Only the mode gate is skipped, so an ordered pair is still left alone."""
        mock_bt.hvac_mode = HVACMode.OFF
        mock_bt.bt_target_temp = 22.0
        mock_bt.bt_target_cooltemp = 26.0
        BetterThermostat._enforce_cool_above_heat(mock_bt, regardless_of_hvac_mode=True)
        assert mock_bt.bt_target_cooltemp == 26.0

    def test_default_is_mode_gated(self, mock_bt):
        """A caller that omits the flag is gated on HEAT_COOL."""
        mock_bt.hvac_mode = HVACMode.OFF
        mock_bt.bt_target_temp = 22.0
        mock_bt.bt_target_temp_step = 0.5
        mock_bt.bt_target_cooltemp = 20.0
        self._call(mock_bt)
        assert mock_bt.bt_target_cooltemp == 20.0

    def test_bump_is_capped_at_the_configured_maximum(self, mock_bt):
        """A step that would overshoot the maximum stops at it.

        The cool target is written to the cooler, so a value the configured
        range does not contain must never be stored. A maximum above the heat
        target holds both invariants at once: the capped value is inside the
        range and still strictly above the heat target.
        """
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_max_temp = 30.0
        mock_bt.bt_target_temp = 29.8
        mock_bt.bt_target_temp_step = 0.5
        mock_bt.bt_target_cooltemp = 29.0
        self._call(mock_bt)
        assert mock_bt.bt_target_cooltemp == 30.0
        assert mock_bt.bt_target_cooltemp > mock_bt.bt_target_temp
        assert mock_bt.bt_target_cooltemp <= mock_bt.bt_max_temp

    def test_no_maximum_leaves_the_bump_uncapped(self, mock_bt):
        """Without a maximum there is no upper bound to stop the bump at."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_max_temp = None
        mock_bt.bt_target_temp = 29.8
        mock_bt.bt_target_temp_step = 0.5
        mock_bt.bt_target_cooltemp = 29.0
        self._call(mock_bt)
        assert mock_bt.bt_target_cooltemp == 30.3

    def test_maximum_below_the_heat_target_does_not_cap(self, mock_bt, caplog):
        """A maximum under the heat target is no bound for the cool target.

        The heat target itself is outside the range there, so capping the cool
        target to the maximum would leave it below the heat target: the very
        inversion this method exists to prevent. The ordering decides instead.
        """
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_max_temp = 18.0
        mock_bt.bt_target_temp = 22.0
        mock_bt.bt_target_temp_step = 0.5
        mock_bt.bt_target_cooltemp = 17.0

        with caplog.at_level(logging.WARNING):
            self._call(mock_bt)

        assert mock_bt.bt_target_cooltemp == 22.5
        assert mock_bt.bt_target_cooltemp > mock_bt.bt_target_temp
        assert "raised to the configured maximum" not in caplog.text

    def test_out_of_range_cool_target_is_raised_not_lowered(self, mock_bt):
        """A cool target above the maximum but under the heat target moves up.

        Pulling it down to the maximum would put it under the heat target, so
        it is bumped above the heat target and stays out of range.
        """
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_max_temp = 30.0
        mock_bt.bt_target_temp = 31.0
        mock_bt.bt_target_temp_step = 0.5
        mock_bt.bt_target_cooltemp = 30.5
        self._call(mock_bt)
        assert mock_bt.bt_target_cooltemp == 31.5

    def test_heat_target_at_the_maximum_lifts_cool_to_the_maximum(
        self, mock_bt, caplog
    ):
        """With no room above the heat target the cool target goes to the maximum.

        Leaving it below the heat target would cool a room the TRVs are heating,
        so it moves as far up as the range allows and the remaining overlap is
        annunciated.
        """
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_max_temp = 30.0
        mock_bt.bt_target_temp = 30.0
        mock_bt.bt_target_temp_step = 0.5
        mock_bt.bt_target_cooltemp = 28.0

        with caplog.at_level(logging.WARNING):
            self._call(mock_bt)

        assert mock_bt.bt_target_cooltemp == 30.0
        assert (
            "cooling target 28.00 raised to the configured maximum 30.00, which "
            "the heating target occupies as well, because the range holds no "
            "value above it" in caplog.text
        )

    def test_non_positive_step_falls_back_to_the_default_step(self, mock_bt, caplog):
        """A step that is not positive cannot be the distance the target moves.

        The configured step reaches this method as the operator entered it, and
        a value of zero or below would subtract instead of add: the cool target
        would land at or under the heat target, still inverted, and be
        annunciated as if it had been lifted.
        """
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_max_temp = 30.0
        mock_bt.bt_target_temp = 22.0
        mock_bt.bt_target_temp_step = -1.0
        mock_bt.bt_target_cooltemp = 20.0

        with caplog.at_level(logging.WARNING):
            self._call(mock_bt)

        assert mock_bt.bt_target_cooltemp == 22.5
        assert mock_bt.bt_target_cooltemp > mock_bt.bt_target_temp
        assert (
            "cooling target 20.00 adjusted to 22.50 to stay above heating "
            "target 22.00" in caplog.text
        )

    def test_pair_already_at_the_maximum_is_left_alone(self, mock_bt, caplog):
        """Both targets resting on the maximum leaves nothing to adjust or report."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_max_temp = 30.0
        mock_bt.bt_target_temp = 30.0
        mock_bt.bt_target_temp_step = 0.5
        mock_bt.bt_target_cooltemp = 30.0

        with caplog.at_level(logging.WARNING):
            self._call(mock_bt)

        assert mock_bt.bt_target_cooltemp == 30.0
        assert "cooling target" not in caplog.text


# ===========================================================================
# 8. TestEnforceHeatBelowCool
# ===========================================================================


class TestEnforceHeatBelowCool:
    """_enforce_heat_below_cool keeps the heat target strictly below the cool target."""

    def _call(self, bt):
        return BetterThermostat._enforce_heat_below_cool(bt)

    def test_not_heat_cool_mode_is_noop(self, mock_bt):
        """Outside HEAT_COOL the heat target is left untouched even if above cool."""
        mock_bt.hvac_mode = HVACMode.HEAT
        mock_bt.bt_target_temp = 22.0
        mock_bt.bt_target_cooltemp = 20.0
        self._call(mock_bt)
        assert mock_bt.bt_target_temp == 22.0

    def test_heat_below_cool_is_noop(self, mock_bt):
        """A heat target already below the cool target is unchanged."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_target_temp = 20.0
        mock_bt.bt_target_cooltemp = 24.0
        self._call(mock_bt)
        assert mock_bt.bt_target_temp == 20.0

    def test_heat_above_cool_is_pushed_down_by_step(self, mock_bt):
        """A heat target above the cool target drops one step below it."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_target_temp = 24.0
        mock_bt.bt_target_temp_step = 0.5
        mock_bt.bt_target_cooltemp = 22.0
        self._call(mock_bt)
        assert mock_bt.bt_target_temp == 21.5

    def test_heat_equal_cool_is_pushed_down(self, mock_bt):
        """A heat target equal to the cool target is pushed below it."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_target_temp = 22.0
        mock_bt.bt_target_temp_step = 0.5
        mock_bt.bt_target_cooltemp = 22.0
        self._call(mock_bt)
        assert mock_bt.bt_target_temp == 21.5

    def test_step_falls_back_to_half_degree(self, mock_bt):
        """A missing/zero step falls back to 0.5."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_target_temp = 22.0
        mock_bt.bt_target_temp_step = 0
        mock_bt.bt_target_cooltemp = 22.0
        self._call(mock_bt)
        assert mock_bt.bt_target_temp == 21.5

    def test_result_is_clamped_to_min_temp(self, mock_bt, caplog):
        """The heat target never drops below the configured minimum.

        The clamp lands the heat target on the cool target rather than below
        it, so the line that reports the move says what it did instead of
        claiming an ordering the stored pair does not have.
        """
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_target_temp = 6.0
        mock_bt.bt_target_temp_step = 0.5
        mock_bt.bt_min_temp = 5.0
        mock_bt.bt_target_cooltemp = 5.0

        with caplog.at_level(logging.WARNING):
            self._call(mock_bt)

        assert mock_bt.bt_target_temp == 5.0
        assert (
            "heating target 6.00 set to the configured minimum 5.00, which is "
            "not below the cooling target 5.00" in caplog.text
        )
        assert "to stay below cooling target" not in caplog.text

    def test_minimum_above_the_cool_target_is_reported_as_an_overlap(
        self, mock_bt, caplog
    ):
        """A minimum above the cool target leaves the stored heat target above it.

        The clamp is the only bound applied, so the heat target comes to rest
        on the minimum even though the cool target sits below it, and the line
        that reports the move names the minimum rather than an ordering.
        """
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_target_temp = 22.0
        mock_bt.bt_target_temp_step = 0.5
        mock_bt.bt_min_temp = 20.0
        mock_bt.bt_target_cooltemp = 15.0

        with caplog.at_level(logging.WARNING):
            self._call(mock_bt)

        assert mock_bt.bt_target_temp == 20.0
        assert (
            "heating target 22.00 set to the configured minimum 20.00, which is "
            "not below the cooling target 15.00" in caplog.text
        )

    def test_ordered_result_is_reported_as_ordered(self, mock_bt, caplog):
        """A heat target that ends up below the cool target reports the ordering."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_target_temp = 24.0
        mock_bt.bt_target_temp_step = 0.5
        mock_bt.bt_min_temp = 5.0
        mock_bt.bt_target_cooltemp = 22.0

        with caplog.at_level(logging.WARNING):
            self._call(mock_bt)

        assert mock_bt.bt_target_temp == 21.5
        assert (
            "heating target 24.00 adjusted to 21.50 to stay below cooling "
            "target 22.00" in caplog.text
        )
        assert "configured minimum" not in caplog.text

    def test_heat_target_already_at_the_minimum_is_left_alone(self, mock_bt, caplog):
        """A pair already resting on the minimum leaves nothing to adjust or report."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_target_temp = 5.0
        mock_bt.bt_target_temp_step = 0.5
        mock_bt.bt_min_temp = 5.0
        mock_bt.bt_target_cooltemp = 5.0

        with caplog.at_level(logging.WARNING):
            self._call(mock_bt)

        assert mock_bt.bt_target_temp == 5.0
        assert "heating target" not in caplog.text

    def test_negative_step_falls_back_to_the_default_step(self, mock_bt, caplog):
        """A negative step must not raise the heat target above the cool target.

        A child that publishes ``target_temp_step: -0.5`` reaches
        ``bt_target_temp_step`` unfiltered, and subtracting a negative step
        would invert the pair this method exists to order while reporting the
        move as if it had stayed below.
        """
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_target_temp = 22.0
        mock_bt.bt_target_temp_step = -0.5
        mock_bt.bt_min_temp = 5.0
        mock_bt.bt_target_cooltemp = 22.0

        with caplog.at_level(logging.WARNING):
            self._call(mock_bt)

        assert mock_bt.bt_target_temp == 21.5
        assert mock_bt.bt_target_temp < mock_bt.bt_target_cooltemp
        assert (
            "heating target 22.00 adjusted to 21.50 to stay below cooling "
            "target 22.00" in caplog.text
        )

    def test_non_finite_step_falls_back_to_the_default_step(self, mock_bt):
        """A NaN step must not turn the heat target into NaN."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_target_temp = 22.0
        mock_bt.bt_target_temp_step = float("nan")
        mock_bt.bt_min_temp = 5.0
        mock_bt.bt_target_cooltemp = 22.0
        self._call(mock_bt)
        assert mock_bt.bt_target_temp == 21.5

    def test_none_heat_target_is_noop(self, mock_bt):
        """A None heat target does not raise and stays None."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_target_temp = None
        mock_bt.bt_target_cooltemp = 22.0
        self._call(mock_bt)
        assert mock_bt.bt_target_temp is None


# ===========================================================================
# 9. TestBoundTargetToRange
# ===========================================================================


class TestBoundTargetToRange:
    """_bound_target_to_range holds a re-injected target inside the range."""

    def _call(self, bt, value):
        return BetterThermostat._bound_target_to_range(bt, value)

    @pytest.mark.parametrize("value", [16.0, 21.0, 26.0])
    def test_a_target_the_range_contains_is_returned_unchanged(self, mock_bt, value):
        """Both bounds are inclusive, so only a target outside the range moves."""
        mock_bt.bt_min_temp = 16.0
        mock_bt.bt_max_temp = 26.0
        assert self._call(mock_bt, value) == value

    def test_value_below_the_minimum_is_raised_to_it(self, mock_bt):
        """A target under the minimum is not a setpoint BT can hold."""
        mock_bt.bt_min_temp = 20.0
        mock_bt.bt_max_temp = 30.0
        assert self._call(mock_bt, 9.0) == 20.0

    def test_value_above_the_maximum_is_lowered_to_it(self, mock_bt):
        """A target over the maximum is not a setpoint BT can hold either."""
        mock_bt.bt_min_temp = 16.0
        mock_bt.bt_max_temp = 26.0
        assert self._call(mock_bt, 35.0) == 26.0

    def test_an_unknown_minimum_leaves_the_lower_side_unbounded(self, mock_bt):
        """A bound stays None until a child entity reports one."""
        mock_bt.bt_min_temp = None
        mock_bt.bt_max_temp = 26.0
        assert self._call(mock_bt, 9.0) == 9.0

    def test_an_unknown_maximum_leaves_the_upper_side_unbounded(self, mock_bt):
        """The upper side is enforced only once it is known."""
        mock_bt.bt_min_temp = 16.0
        mock_bt.bt_max_temp = None
        assert self._call(mock_bt, 35.0) == 35.0

    def test_a_non_overlapping_range_is_decided_by_the_maximum(self, mock_bt):
        """A minimum above the maximum leaves the upper bound the last word.

        Heater and cooler ranges that do not overlap put bt_min_temp above
        bt_max_temp, which _resolve_temperature_range permits. Applying the two
        bounds in sequence rather than exclusively is what makes the outcome
        defined there.
        """
        mock_bt.bt_min_temp = 25.0
        mock_bt.bt_max_temp = 20.0
        assert self._call(mock_bt, 10.0) == 20.0
        assert self._call(mock_bt, 30.0) == 20.0


# ===========================================================================
# 10. TestClampInboundCoolTarget
# ===========================================================================


class TestClampInboundCoolTarget:
    """_clamp_inbound_cool_target raises a reported cool setpoint above heat."""

    def _call(self, bt, value):
        return BetterThermostat._clamp_inbound_cool_target(bt, value)

    def test_without_a_cooler_is_noop(self, mock_bt):
        """Without a cooling channel there is no second bound."""
        mock_bt.cooler_entity_id = None
        mock_bt.bt_target_temp = 22.0
        assert self._call(mock_bt, 18.0) == 18.0

    def test_none_heat_target_is_noop(self, mock_bt):
        """An unknown heat target is no bound to clear."""
        mock_bt.cooler_entity_id = "switch.ac"
        mock_bt.bt_target_temp = None
        assert self._call(mock_bt, 18.0) == 18.0

    def test_value_already_above_heat_target_is_kept(self, mock_bt):
        """A reported setpoint that already clears the heat target is adopted."""
        mock_bt.cooler_entity_id = "switch.ac"
        mock_bt.bt_target_temp = 20.0
        mock_bt.bt_target_temp_step = 0.5
        assert self._call(mock_bt, 24.0) == 24.0

    def test_value_below_heat_target_is_raised(self, mock_bt):
        """A reported setpoint below the heat target is raised one step above it."""
        mock_bt.cooler_entity_id = "switch.ac"
        mock_bt.bt_target_temp = 20.0
        mock_bt.bt_target_temp_step = 0.5
        assert self._call(mock_bt, 18.0) == 20.5

    def test_value_equal_to_heat_target_is_raised(self, mock_bt):
        """The two targets must not coincide, so an equal report is raised."""
        mock_bt.cooler_entity_id = "switch.ac"
        mock_bt.bt_target_temp = 20.0
        mock_bt.bt_target_temp_step = 0.5
        assert self._call(mock_bt, 20.0) == 20.5

    def test_step_falls_back_to_half_degree(self, mock_bt):
        """A missing/zero step falls back to 0.5."""
        mock_bt.cooler_entity_id = "switch.ac"
        mock_bt.bt_target_temp = 20.0
        mock_bt.bt_target_temp_step = 0
        assert self._call(mock_bt, 20.0) == 20.5

    @pytest.mark.parametrize("step", [-1.0, float("nan"), float("inf")])
    def test_unusable_step_falls_back_to_half_degree(self, mock_bt, step):
        """A step that is not a positive finite number falls back to 0.5.

        ``bt_target_temp_step`` carries whatever the child entities report. A
        negative step would put the floor below the heating target, and a NaN
        one would lose every comparison and leave the value unbounded, so both
        would hand back a cooling target at or under the heating target.
        """
        mock_bt.cooler_entity_id = "switch.ac"
        mock_bt.bt_target_temp = 20.0
        mock_bt.bt_target_temp_step = step
        adopted = self._call(mock_bt, 18.0)
        assert adopted == 20.5
        assert adopted > mock_bt.bt_target_temp

    def test_floor_is_capped_at_max_temp(self, mock_bt):
        """With the heat target at the maximum the floor stops at the maximum."""
        mock_bt.cooler_entity_id = "switch.ac"
        mock_bt.bt_target_temp = 30.0
        mock_bt.bt_max_temp = 30.0
        mock_bt.bt_target_temp_step = 0.5
        assert self._call(mock_bt, 22.0) == 30.0

    def test_bound_holds_while_bt_reads_off(self, mock_bt):
        """A configured cooling channel bounds the report even while BT is off.

        The two targets have to be ordered whenever the mode resolves, so the
        bound does not wait for the mode to be HEAT_COOL.
        """
        mock_bt.cooler_entity_id = "switch.ac"
        mock_bt.hvac_mode = HVACMode.OFF
        mock_bt.bt_target_temp = 20.0
        mock_bt.bt_target_temp_step = 0.5
        assert self._call(mock_bt, 18.0) == 20.5


# ===========================================================================
# 11. TestClampInboundHeatTarget
# ===========================================================================


class TestClampInboundHeatTarget:
    """_clamp_inbound_heat_target lowers a reported heat setpoint below cool."""

    def _call(self, bt, value):
        return BetterThermostat._clamp_inbound_heat_target(bt, value)

    def test_without_a_cooler_is_noop(self, mock_bt):
        """Without a cooling channel there is no second bound."""
        mock_bt.cooler_entity_id = None
        mock_bt.bt_target_cooltemp = 22.0
        assert self._call(mock_bt, 26.0) == 26.0

    def test_none_cool_target_is_noop(self, mock_bt):
        """An unknown cool target is no bound to clear."""
        mock_bt.cooler_entity_id = "switch.ac"
        mock_bt.bt_target_cooltemp = None
        assert self._call(mock_bt, 26.0) == 26.0

    def test_value_already_below_cool_target_is_kept(self, mock_bt):
        """A reported setpoint that already clears the cool target is adopted."""
        mock_bt.cooler_entity_id = "switch.ac"
        mock_bt.bt_target_cooltemp = 24.0
        mock_bt.bt_target_temp_step = 0.5
        assert self._call(mock_bt, 20.0) == 20.0

    def test_value_above_cool_target_is_lowered(self, mock_bt):
        """A reported setpoint above the cool target drops one step below it."""
        mock_bt.cooler_entity_id = "switch.ac"
        mock_bt.bt_target_cooltemp = 24.0
        mock_bt.bt_target_temp_step = 0.5
        assert self._call(mock_bt, 26.0) == 23.5

    def test_value_equal_to_cool_target_is_lowered(self, mock_bt):
        """The two targets must not coincide, so an equal report is lowered."""
        mock_bt.cooler_entity_id = "switch.ac"
        mock_bt.bt_target_cooltemp = 24.0
        mock_bt.bt_target_temp_step = 0.5
        assert self._call(mock_bt, 24.0) == 23.5

    def test_step_falls_back_to_half_degree(self, mock_bt):
        """A missing/zero step falls back to 0.5."""
        mock_bt.cooler_entity_id = "switch.ac"
        mock_bt.bt_target_cooltemp = 24.0
        mock_bt.bt_target_temp_step = 0
        assert self._call(mock_bt, 24.0) == 23.5

    @pytest.mark.parametrize("step", [-1.0, float("nan"), float("inf")])
    def test_unusable_step_falls_back_to_half_degree(self, mock_bt, step):
        """A step that is not a positive finite number falls back to 0.5.

        The mirror of the cooling case: a negative step would lift the ceiling
        above the cooling target and a NaN one would drop the bound, so both
        would hand back a heating target at or over the cooling target.
        """
        mock_bt.cooler_entity_id = "switch.ac"
        mock_bt.bt_target_cooltemp = 24.0
        mock_bt.bt_target_temp_step = step
        adopted = self._call(mock_bt, 26.0)
        assert adopted == 23.5
        assert adopted < mock_bt.bt_target_cooltemp

    def test_ceiling_is_held_at_min_temp(self, mock_bt):
        """With the cool target at the minimum the ceiling stops at the minimum."""
        mock_bt.cooler_entity_id = "switch.ac"
        mock_bt.bt_target_cooltemp = 5.0
        mock_bt.bt_min_temp = 5.0
        mock_bt.bt_target_temp_step = 0.5
        assert self._call(mock_bt, 22.0) == 5.0

    def test_bound_holds_while_bt_reads_off(self, mock_bt):
        """A configured cooling channel bounds the report even while BT is off.

        A valve that cannot be switched off reports its knob turn while the mode
        is still OFF, and the same event resolves the mode to HEAT afterwards.
        """
        mock_bt.cooler_entity_id = "switch.ac"
        mock_bt.hvac_mode = HVACMode.OFF
        mock_bt.bt_target_cooltemp = 24.0
        mock_bt.bt_target_temp_step = 0.5
        assert self._call(mock_bt, 26.0) == 23.5


# ===========================================================================
# 12. TestSeedCoolTarget
# ===========================================================================


class TestSeedCoolTarget:
    """_seed_cool_target adopts a cooler's own setpoint as the cool target."""

    def _call(self, bt, setpoint, entity_id="climate.cooler"):
        return BetterThermostat._seed_cool_target(bt, setpoint, entity_id)

    def test_in_range_setpoint_is_stored_and_reported_at_info(self, mock_bt, caplog):
        """An unremarkable seed is traceable without being flagged as a problem."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_target_temp = 20.0
        mock_bt.bt_target_cooltemp = None
        setpoint = InboundSetpoint(raw=24.0, value=24.0, clamped=False, is_echo=False)

        with caplog.at_level(logging.INFO):
            self._call(mock_bt, setpoint)

        assert mock_bt.bt_target_cooltemp == 24.0
        assert "reports setpoint 24.0 while the cool target is unknown" in caplog.text
        assert "outside of range" not in caplog.text

    def test_clamped_setpoint_warns_and_stores_the_clamped_value(self, mock_bt, caplog):
        """The clamped value is written back to the cooler, so it is annunciated."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_target_temp = 15.0
        mock_bt.bt_target_cooltemp = None
        setpoint = InboundSetpoint(raw=16.0, value=18.0, clamped=True, is_echo=False)

        with caplog.at_level(logging.WARNING):
            self._call(mock_bt, setpoint)

        assert mock_bt.bt_target_cooltemp == 18.0
        assert (
            "reported setpoint 16.0 outside of range while the cool target is "
            "unknown, taking 18.0 as the cool target" in caplog.text
        )

    def test_seed_colliding_with_the_heat_target_is_lifted(self, mock_bt):
        """The observation yields, so the heating target the user set survives."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_target_temp = 22.0
        mock_bt.bt_target_temp_step = 0.5
        mock_bt.bt_target_cooltemp = None
        setpoint = InboundSetpoint(raw=21.0, value=21.0, clamped=False, is_echo=False)

        self._call(mock_bt, setpoint)

        assert mock_bt.bt_target_cooltemp == 22.5
        assert mock_bt.bt_target_temp == 22.0

    def test_seed_is_lifted_while_bt_is_off(self, mock_bt):
        """A seed taken while BT is off is the one the first cooling cycle uses."""
        mock_bt.hvac_mode = HVACMode.OFF
        mock_bt.bt_target_temp = 22.0
        mock_bt.bt_target_temp_step = 0.5
        mock_bt.bt_target_cooltemp = None
        setpoint = InboundSetpoint(raw=20.0, value=20.0, clamped=False, is_echo=False)

        self._call(mock_bt, setpoint)

        assert mock_bt.bt_target_cooltemp == 22.5
        assert mock_bt.bt_target_temp == 22.0
