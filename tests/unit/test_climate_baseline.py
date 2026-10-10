"""Baseline tests for climate.py.

Tests the 6 most important methods using unbound-method calls with a shared
mock_bt fixture (a ThermostatStandIn with explicit attributes).
"""

from datetime import UTC, datetime, timedelta
import logging
from unittest.mock import MagicMock

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
from homeassistant.exceptions import ServiceValidationError
import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.helpers import InboundSetpoint
from custom_components.better_thermostat.utils.hvac_action import ToleranceHysteresis
from custom_components.better_thermostat.utils.thermal_learning import (
    HeatingPowerTracker,
    HeatLossTracker,
)
from tests.factories import ThermostatStandIn, make_state

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_bt():
    """Create a mock BetterThermostat with sensible defaults."""
    bt = ThermostatStandIn()
    bt.hass = MagicMock()
    bt.device_name = "Test BT"
    # Temperature
    bt.room_temperature = 20.0
    bt.heat_target_temperature = 22.0
    bt.cool_target_temperature = 26.0
    bt.bt_min_temp = 5.0
    bt.bt_max_temp = 30.0
    bt.cool_min_temperature = None
    bt.cool_max_temperature = None
    bt.bt_target_temperature_step = 0.5
    bt.tolerance = 0.5
    # HVAC
    bt.bt_hvac_mode = HVACMode.HEAT
    bt.hvac_mode = HVACMode.HEAT
    bt.hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    bt.window_open = False
    bt.contact_open = False
    bt.ignore_states = False
    # Real kernel state: production updates it via dataclasses.replace,
    # which rejects a MagicMock stand-in.
    bt.kernel_state = make_state()
    bt.clock = FakeClock()
    # Hysteresis
    bt._hysteresis = ToleranceHysteresis()
    # Thermal trackers (real objects – new thin-wrapper methods delegate to these)
    bt._heating_tracker = HeatingPowerTracker(
        heating_power=0.05, min_target=18.0, max_target=24.0
    )
    bt._loss_tracker = HeatLossTracker()
    bt.old_attr_hvac_action = None
    bt.attr_hvac_action = None
    bt.outdoor_sensor_entity_id = None
    # Cooling channel: off unless a test configures one
    bt.cooler_entity_id = None
    bt._preset_cool_temperature = None
    bt._preset_cool_temperatures = dict[str, float]()
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
    bt.real_trvs = dict[str, Trv]()
    # HA callbacks
    bt.control_queue_task = MagicMock()
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
    bt._cooler_previously_active = lambda: BetterThermostat._cooler_previously_active(
        bt
    )
    bt._commit_hvac_action = lambda result: BetterThermostat._commit_hvac_action(
        bt, result
    )
    bt._get_outdoor_temperature = lambda: BetterThermostat._get_outdoor_temperature(bt)
    bt._enforce_cool_above_heat = lambda **kwargs: (
        BetterThermostat._enforce_cool_above_heat(bt, **kwargs)
    )
    bt._enforce_heat_below_cool = lambda **kwargs: (
        BetterThermostat._enforce_heat_below_cool(bt, **kwargs)
    )
    bt._bound_target_to_range = lambda value: BetterThermostat._bound_target_to_range(
        bt, value
    )
    bt._bound_cool_target_to_range = lambda value: (
        BetterThermostat._bound_cool_target_to_range(bt, value)
    )
    bt._configured_temperature_step = None
    bt._onto_target_grid = lambda value: BetterThermostat._onto_target_grid(bt, value)
    bt._applied_target = lambda value, **kwargs: BetterThermostat._applied_target(
        bt, value, **kwargs
    )
    return bt


# A step that is not a positive real number cannot be the distance a target
# moves, so every ordering helper replaces it with the 0.5 default.
UNUSABLE_STEPS = (-1.0, -0.5, float("nan"), float("inf"))


# ===========================================================================
# 1. TestShouldHeatWithTolerance
# ===========================================================================


class TestShouldHeatWithTolerance:
    """Tests for _should_heat_with_tolerance."""

    def _call(self, bt, previous_action, tol):
        return BetterThermostat._should_heat_with_tolerance(bt, previous_action, tol)

    def test_target_temperature_none(self, mock_bt):
        """Return False when target temperature is None."""
        mock_bt.heat_target_temperature = None
        assert self._call(mock_bt, HVACAction.IDLE, 0.5) is False

    def test_room_temperature_none(self, mock_bt):
        """Return False when current temperature is None."""
        mock_bt.room_temperature = None
        assert self._call(mock_bt, HVACAction.IDLE, 0.5) is False

    def test_heating_cur_below_target(self, mock_bt):
        """Continue heating when current temperature is below target."""
        mock_bt.room_temperature = 21.5
        mock_bt.heat_target_temperature = 22.0
        assert self._call(mock_bt, HVACAction.HEATING, 0.5) is True

    def test_heating_cur_equals_target(self, mock_bt):
        """Stop heating when current temperature equals target."""
        mock_bt.room_temperature = 22.0
        mock_bt.heat_target_temperature = 22.0
        assert self._call(mock_bt, HVACAction.HEATING, 0.5) is False

    def test_heating_cur_above_target(self, mock_bt):
        """Stop heating when current temperature exceeds target."""
        mock_bt.room_temperature = 22.5
        mock_bt.heat_target_temperature = 22.0
        assert self._call(mock_bt, HVACAction.HEATING, 0.5) is False

    def test_idle_cur_below_threshold(self, mock_bt):
        """Start heating when idle and temperature is below threshold."""
        mock_bt.room_temperature = 21.0
        mock_bt.heat_target_temperature = 22.0
        assert self._call(mock_bt, HVACAction.IDLE, 0.5) is True

    def test_idle_cur_equals_threshold(self, mock_bt):
        """Stay idle when current temperature equals threshold."""
        mock_bt.room_temperature = 21.5
        mock_bt.heat_target_temperature = 22.0
        assert self._call(mock_bt, HVACAction.IDLE, 0.5) is False

    def test_idle_cur_above_threshold(self, mock_bt):
        """Stay idle when current temperature is above threshold."""
        mock_bt.room_temperature = 21.8
        mock_bt.heat_target_temperature = 22.0
        assert self._call(mock_bt, HVACAction.IDLE, 0.5) is False

    def test_negative_tolerance_clamped_to_zero(self, mock_bt):
        """Negative tolerance → clamped to 0 → threshold == target."""
        mock_bt.room_temperature = 21.9
        mock_bt.heat_target_temperature = 22.0
        # With tol=0, IDLE threshold is target itself → 21.9 < 22.0 → True
        assert self._call(mock_bt, HVACAction.IDLE, -1.0) is True

    def test_zero_tolerance_no_hysteresis(self, mock_bt):
        """Tolerance 0 → IDLE threshold == target (no hysteresis band)."""
        mock_bt.room_temperature = 21.9
        mock_bt.heat_target_temperature = 22.0
        assert self._call(mock_bt, HVACAction.IDLE, 0.0) is True
        mock_bt.room_temperature = 22.0
        assert self._call(mock_bt, HVACAction.IDLE, 0.0) is False


# ===========================================================================
# 2. TestComputeHvacAction
# ===========================================================================


class TestComputeHvacAction:
    """Tests for _compute_hvac_action."""

    def _call(self, bt):
        return BetterThermostat._compute_hvac_action(bt)

    def test_target_temperature_none_returns_idle(self, mock_bt):
        """Return IDLE when target temperature is None."""
        mock_bt.heat_target_temperature = None
        assert self._call(mock_bt) == HVACAction.IDLE

    def test_room_temperature_none_returns_idle(self, mock_bt):
        """Return IDLE when current temperature is None."""
        mock_bt.room_temperature = None
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
        mock_bt.room_temperature = 21.0
        mock_bt.heat_target_temperature = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        assert self._call(mock_bt) == HVACAction.HEATING

    def test_heat_mode_cur_at_target(self, mock_bt):
        """HEAT mode, cur >= target → IDLE."""
        mock_bt.room_temperature = 22.0
        mock_bt.heat_target_temperature = 22.0
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        assert self._call(mock_bt) == HVACAction.IDLE

    def test_heat_cool_cooling_above_cooltemp(self, mock_bt):
        """HEAT_COOL, cur > cooltemp + tol → COOLING."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_hvac_mode = HVACMode.HEAT
        mock_bt.room_temperature = 27.0
        mock_bt.heat_target_temperature = 22.0
        mock_bt.cool_target_temperature = 26.0
        mock_bt.tolerance = 0.5
        assert self._call(mock_bt) == HVACAction.COOLING

    def test_trv_override_hvac_action_heating(self, mock_bt):
        """TRV reports hvac_action='heating' in band → override to HEATING."""
        mock_bt.room_temperature = (
            21.7  # in band: target-tol(21.5) < cur < target(22.0)
        )
        mock_bt.heat_target_temperature = 22.0
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt.real_trvs = {
            "climate.trv1": Trv(entity_id="climate.trv1", hvac_action="heating")
        }
        assert self._call(mock_bt) == HVACAction.HEATING

    def test_trv_override_valve_position(self, mock_bt):
        """TRV valve_position=50 in band → override to HEATING."""
        mock_bt.room_temperature = 21.7
        mock_bt.heat_target_temperature = 22.0
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt.real_trvs = {
            "climate.trv1": Trv(entity_id="climate.trv1", valve_position=50)
        }
        assert self._call(mock_bt) == HVACAction.HEATING

    def test_trv_override_last_valve_percent_0_1_range(self, mock_bt):
        """TRV last_valve_percent=0.8 (0-1 range) → normalized to 80% → HEATING."""
        mock_bt.room_temperature = 21.7
        mock_bt.heat_target_temperature = 22.0
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt.real_trvs = {
            "climate.trv1": Trv(entity_id="climate.trv1", last_valve_percent=0.8)
        }
        assert self._call(mock_bt) == HVACAction.HEATING

    def test_trv_override_suppressed_above_target(self, mock_bt):
        """Above target with TRV still reporting heating → action stays IDLE."""
        mock_bt.room_temperature = 22.3  # above target → BT has decided IDLE
        mock_bt.heat_target_temperature = 22.0
        mock_bt._hysteresis.last_action = HVACAction.HEATING
        mock_bt.real_trvs = {
            "climate.trv1": Trv(entity_id="climate.trv1", hvac_action="heating")
        }
        assert self._call(mock_bt) == HVACAction.IDLE

    def test_ignore_states_no_trv_override(self, mock_bt):
        """ignore_states=True in band → TRV override still skipped, returns IDLE."""
        mock_bt.room_temperature = 21.7
        mock_bt.heat_target_temperature = 22.0
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt.ignore_states = True
        mock_bt.real_trvs = {
            "climate.trv1": Trv(entity_id="climate.trv1", hvac_action="heating")
        }
        assert self._call(mock_bt) == HVACAction.IDLE

    def test_ignore_trv_states_per_trv(self, mock_bt):
        """ignore_trv_states=True on specific TRV in band → that TRV is skipped."""
        mock_bt.room_temperature = 21.7
        mock_bt.heat_target_temperature = 22.0
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt.real_trvs = {
            "climate.trv1": Trv(
                entity_id="climate.trv1", hvac_action="heating", ignore_trv_states=True
            )
        }
        assert self._call(mock_bt) == HVACAction.IDLE

    def test_tolerance_decision_saved_before_trv_override(self, mock_bt):
        """Hysteresis state uses tolerance decision, not TRV-overridden action."""
        mock_bt.room_temperature = 21.7  # in band → tolerance says IDLE
        mock_bt.heat_target_temperature = 22.0
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt.real_trvs = {
            "climate.trv1": Trv(entity_id="climate.trv1", hvac_action="heating")
        }
        self._call(mock_bt)
        # Tolerance last action should be IDLE (tolerance decision), not HEATING
        assert mock_bt._hysteresis.last_action == HVACAction.IDLE

    def test_tolerance_hold_active_set(self, mock_bt):
        """_tolerance_hold_active is True when tolerance says no-heat but not cooling."""
        mock_bt.room_temperature = (
            21.8  # in band: target-tol(21.5) < cur < target(22.0)
        )
        mock_bt.heat_target_temperature = 22.0
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
    async def test_room_temperature_none_early_return(self, mock_bt):
        """Skip update when current temperature is None."""
        mock_bt.room_temperature = None
        old_power = mock_bt.heating_power
        await self._call(mock_bt)
        assert mock_bt.heating_power == old_power

    @pytest.mark.asyncio
    async def test_heating_start_transition(self, mock_bt):
        """Transition to HEATING sets start_temperature and start_timestamp."""
        mock_bt.room_temperature = 20.0
        mock_bt.heat_target_temperature = 22.0
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

        now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        mock_bt.clock = FakeClock(now_value=now)
        await self._call(mock_bt)

        assert mock_bt._heating_tracker.start_temperature == 20.0
        assert mock_bt._heating_tracker.start_ts == now

    @pytest.mark.asyncio
    async def test_heating_stop_sets_end(self, mock_bt):
        """Transition from HEATING → IDLE sets end_temperature/timestamp."""
        now = datetime(2025, 1, 1, 12, 10, 0, tzinfo=UTC)
        mock_bt.room_temperature = 22.0
        mock_bt.heat_target_temperature = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._hysteresis.last_action = HVACAction.HEATING
        mock_bt.old_attr_hvac_action = HVACAction.HEATING
        mock_bt._heating_tracker._prev_action = HVACAction.HEATING
        mock_bt._heating_tracker.start_temperature = 20.0
        mock_bt._heating_tracker.start_ts = now - timedelta(minutes=10)
        mock_bt._heating_tracker.end_temperature = None
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        mock_bt.clock = FakeClock(now_value=now)
        await self._call(mock_bt)

        assert mock_bt._heating_tracker.end_temperature == 22.0
        assert mock_bt._heating_tracker.end_ts == now

    @pytest.mark.asyncio
    async def test_peak_tracking(self, mock_bt):
        """Temperature still rising after heating stopped → end_temperature updated."""
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        mock_bt.room_temperature = 22.5  # above previous end_temperature
        mock_bt.heat_target_temperature = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt.old_attr_hvac_action = HVACAction.IDLE
        mock_bt._heating_tracker._prev_action = HVACAction.IDLE
        mock_bt._heating_tracker.start_temperature = 20.0
        mock_bt._heating_tracker.start_ts = base - timedelta(minutes=15)
        mock_bt._heating_tracker.end_temperature = 22.0
        mock_bt._heating_tracker.end_ts = base - timedelta(minutes=5)
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        mock_bt.clock = FakeClock(now_value=base)
        await self._call(mock_bt)

        assert mock_bt._heating_tracker.end_temperature == 22.5

    @pytest.mark.asyncio
    async def test_finalization_on_temperature_drop(self, mock_bt):
        """Temperature falls below peak → cycle finalized, power updated."""
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        mock_bt.room_temperature = 21.8  # below peak of 22.5
        mock_bt.heat_target_temperature = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt.old_attr_hvac_action = HVACAction.IDLE
        mock_bt._heating_tracker._prev_action = HVACAction.IDLE
        mock_bt._heating_tracker.start_temperature = 20.0
        mock_bt._heating_tracker.start_ts = base - timedelta(minutes=10)
        mock_bt._heating_tracker.end_temperature = 22.5
        mock_bt._heating_tracker.end_ts = base - timedelta(minutes=2)
        mock_bt.heating_power = 0.05
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        mock_bt.clock = FakeClock(now_value=base)
        await self._call(mock_bt)

        # Cycle reset after finalization
        assert mock_bt._heating_tracker.start_temperature is None
        assert mock_bt._heating_tracker.end_temperature is None
        # Power was updated (EMA smoothing)
        assert mock_bt.heating_power != 0.05
        assert len(mock_bt.last_heating_power_stats) == 1

    @pytest.mark.asyncio
    async def test_finalization_on_timeout(self, mock_bt):
        """30-minute timeout triggers finalization even without temperature drop."""
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        mock_bt.room_temperature = 22.5  # still at peak (no drop)
        mock_bt.heat_target_temperature = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt.old_attr_hvac_action = HVACAction.IDLE
        mock_bt._heating_tracker._prev_action = HVACAction.IDLE
        mock_bt._heating_tracker.start_temperature = 20.0
        mock_bt._heating_tracker.start_ts = base - timedelta(minutes=40)
        mock_bt._heating_tracker.end_temperature = 22.5
        mock_bt._heating_tracker.end_ts = base - timedelta(minutes=31)
        mock_bt.heating_power = 0.05
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        mock_bt.clock = FakeClock(now_value=base)
        await self._call(mock_bt)

        assert mock_bt._heating_tracker.start_temperature is None
        assert len(mock_bt.last_heating_power_stats) == 1

    @pytest.mark.asyncio
    async def test_short_cycle_discarded(self, mock_bt):
        """Cycles shorter than 1 minute are discarded."""
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        mock_bt.room_temperature = 21.8
        mock_bt.heat_target_temperature = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt.old_attr_hvac_action = HVACAction.IDLE
        mock_bt._heating_tracker._prev_action = HVACAction.IDLE
        mock_bt._heating_tracker.start_temperature = 20.0
        mock_bt._heating_tracker.start_ts = base - timedelta(seconds=30)  # 0.5 min
        mock_bt._heating_tracker.end_temperature = 22.5
        mock_bt._heating_tracker.end_ts = base - timedelta(seconds=5)
        old_power = mock_bt.heating_power
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        mock_bt.clock = FakeClock(now_value=base)
        await self._call(mock_bt)

        assert mock_bt.heating_power == old_power
        assert len(mock_bt.last_heating_power_stats) == 0

    @pytest.mark.asyncio
    async def test_negative_temperature_diff_discarded(self, mock_bt):
        """Negative temperature diff (end < start) is discarded."""
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        mock_bt.room_temperature = 19.0  # below peak → finalize
        mock_bt.heat_target_temperature = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt.old_attr_hvac_action = HVACAction.IDLE
        mock_bt._heating_tracker._prev_action = HVACAction.IDLE
        mock_bt._heating_tracker.start_temperature = 21.0
        mock_bt._heating_tracker.start_ts = base - timedelta(minutes=10)
        mock_bt._heating_tracker.end_temperature = 20.0  # end < start → negative diff
        mock_bt._heating_tracker.end_ts = base - timedelta(minutes=2)
        old_power = mock_bt.heating_power
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        mock_bt.clock = FakeClock(now_value=base)
        await self._call(mock_bt)

        assert mock_bt.heating_power == old_power
        assert len(mock_bt.last_heating_power_stats) == 0

    @pytest.mark.asyncio
    async def test_ema_smoothing(self, mock_bt):
        """EMA: new = old * (1-alpha) + rate * alpha."""
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        mock_bt.room_temperature = (
            21.8  # above tol threshold (21.5) so action=IDLE, below end_temperature
        )
        mock_bt.heat_target_temperature = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt.old_attr_hvac_action = HVACAction.IDLE
        mock_bt._heating_tracker._prev_action = HVACAction.IDLE
        mock_bt._heating_tracker.start_temperature = 20.0
        mock_bt._heating_tracker.start_ts = base - timedelta(minutes=10)
        mock_bt._heating_tracker.end_temperature = 22.0
        mock_bt._heating_tracker.end_ts = base - timedelta(minutes=2)
        mock_bt.heating_power = 0.05
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        mock_bt.clock = FakeClock(now_value=base)
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
        mock_bt.room_temperature = (
            21.8  # above tol threshold so action=IDLE, below end_temperature
        )
        mock_bt.heat_target_temperature = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt.old_attr_hvac_action = HVACAction.IDLE
        mock_bt._heating_tracker._prev_action = HVACAction.IDLE
        mock_bt._heating_tracker.start_temperature = 20.0
        mock_bt._heating_tracker.start_ts = base - timedelta(minutes=10)
        mock_bt._heating_tracker.end_temperature = 22.0
        mock_bt._heating_tracker.end_ts = base - timedelta(minutes=2)
        mock_bt.outdoor_sensor_entity_id = "sensor.outdoor"
        outdoor_state = MagicMock()
        outdoor_state.state = "5.0"
        mock_bt.hass.states.get.return_value = outdoor_state
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        mock_bt.clock = FakeClock(now_value=base)
        await self._call(mock_bt)

        assert mock_bt.heating_power_normalized is not None
        stats = mock_bt.last_heating_power_stats[-1]
        assert stats["norm"] is not None

    @pytest.mark.asyncio
    async def test_min_max_clamping(self, mock_bt):
        """Power is clamped to [MIN_HEATING_POWER, MAX_HEATING_POWER]."""
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        mock_bt.room_temperature = (
            21.8  # above tol threshold so action=IDLE, below end_temperature
        )
        mock_bt.heat_target_temperature = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt.old_attr_hvac_action = HVACAction.IDLE
        mock_bt._heating_tracker._prev_action = HVACAction.IDLE
        mock_bt._heating_tracker.start_temperature = 20.0
        mock_bt._heating_tracker.start_ts = base - timedelta(minutes=10)
        mock_bt._heating_tracker.end_temperature = 22.0
        mock_bt._heating_tracker.end_ts = base - timedelta(minutes=2)
        mock_bt.heating_power = 0.0001  # very low → EMA result may be low
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        mock_bt.clock = FakeClock(now_value=base)
        await self._call(mock_bt)

        # MIN_HEATING_POWER = 0.005, MAX_HEATING_POWER = 0.2
        assert mock_bt.heating_power >= 0.005
        assert mock_bt.heating_power <= 0.2

    @pytest.mark.asyncio
    async def test_cycle_telemetry_appended(self, mock_bt):
        """Finalized cycle appends to heating_cycles deque."""
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        mock_bt.room_temperature = (
            21.8  # above tol threshold so action=IDLE, below end_temperature
        )
        mock_bt.heat_target_temperature = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt.old_attr_hvac_action = HVACAction.IDLE
        mock_bt._heating_tracker._prev_action = HVACAction.IDLE
        mock_bt._heating_tracker.start_temperature = 20.0
        mock_bt._heating_tracker.start_ts = base - timedelta(minutes=10)
        mock_bt._heating_tracker.end_temperature = 22.0
        mock_bt._heating_tracker.end_ts = base - timedelta(minutes=2)
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        mock_bt.clock = FakeClock(now_value=base)
        await self._call(mock_bt)

        assert len(mock_bt.heating_cycles) == 1
        cycle = mock_bt.heating_cycles[0]
        assert "delta_kelvin" in cycle
        assert "rate_kelvin_per_min" in cycle


# ===========================================================================
# 4. TestCalculateHeatLoss
# ===========================================================================


class TestCalculateHeatLoss:
    """Tests for calculate_heat_loss."""

    async def _call(self, bt):
        return await BetterThermostat.calculate_heat_loss(bt)

    @pytest.mark.asyncio
    async def test_room_temperature_none_early_return(self, mock_bt):
        """Skip update when current temperature is None."""
        mock_bt.room_temperature = None
        await self._call(mock_bt)
        assert mock_bt._loss_tracker.start_temperature is None

    @pytest.mark.asyncio
    async def test_window_open_resets_tracking(self, mock_bt):
        """Window open → all tracking values reset."""
        mock_bt.window_open = True
        mock_bt.contact_open = True
        mock_bt._loss_tracker.start_temperature = 21.0
        mock_bt._loss_tracker.start_ts = datetime(2025, 1, 1, tzinfo=UTC)
        mock_bt._loss_tracker.end_temperature = 20.5
        mock_bt._loss_tracker.end_ts = datetime(2025, 1, 1, tzinfo=UTC)
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        mock_bt.clock = FakeClock(now_value=datetime(2025, 1, 1, 12, 0, tzinfo=UTC))
        await self._call(mock_bt)

        assert mock_bt._loss_tracker.start_temperature is None
        assert mock_bt._loss_tracker.end_temperature is None

    @pytest.mark.asyncio
    async def test_idle_starts_tracking(self, mock_bt):
        """Entering IDLE starts tracking (loss_start_temp set)."""
        mock_bt.room_temperature = 22.0
        mock_bt.heat_target_temperature = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt._loss_tracker.start_temperature = None
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        mock_bt.clock = FakeClock(now_value=now)
        await self._call(mock_bt)

        assert mock_bt._loss_tracker.start_temperature == 22.0
        assert mock_bt._loss_tracker.start_ts == now

    @pytest.mark.asyncio
    async def test_tracks_lowest_temperature(self, mock_bt):
        """While idle, end_temperature tracks the lowest temperature."""
        now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        # room_temperature must yield IDLE (>= target - tol) AND be below loss_end_temp
        mock_bt.room_temperature = 21.6
        mock_bt.heat_target_temperature = 22.0
        mock_bt.tolerance = 0.5  # threshold = 21.5, 21.6 >= 21.5 → IDLE
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt._loss_tracker.start_temperature = 22.0
        mock_bt._loss_tracker.start_ts = now - timedelta(minutes=10)
        mock_bt._loss_tracker.end_temperature = 21.8  # current (21.6) is lower
        mock_bt._loss_tracker.end_ts = now - timedelta(minutes=5)

        mock_bt.clock = FakeClock(now_value=now)
        await self._call(mock_bt)

        assert mock_bt._loss_tracker.end_temperature == 21.6

    @pytest.mark.asyncio
    async def test_finalization_on_heating_restart(self, mock_bt):
        """Heating starts again → cycle finalized, heat_loss updated."""
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        # Set up a completed idle period
        mock_bt.room_temperature = 20.0  # below target-tol → HEATING
        mock_bt.heat_target_temperature = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt._loss_tracker.start_temperature = 22.0
        mock_bt._loss_tracker.start_ts = base - timedelta(minutes=10)
        mock_bt._loss_tracker.end_temperature = 20.5
        mock_bt._loss_tracker.end_ts = base - timedelta(minutes=2)
        mock_bt.heat_loss_rate = 0.01
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        mock_bt.clock = FakeClock(now_value=base)
        await self._call(mock_bt)

        # Cycle finalized (reset)
        assert mock_bt._loss_tracker.start_temperature is None
        assert mock_bt._loss_tracker.end_temperature is None
        assert len(mock_bt.last_heat_loss_stats) == 1

    @pytest.mark.asyncio
    async def test_short_loss_cycle_discarded(self, mock_bt):
        """Loss cycles shorter than 1 minute are discarded."""
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        mock_bt.room_temperature = 20.0
        mock_bt.heat_target_temperature = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt._loss_tracker.start_temperature = 22.0
        mock_bt._loss_tracker.start_ts = base - timedelta(seconds=30)
        mock_bt._loss_tracker.end_temperature = 21.0
        mock_bt._loss_tracker.end_ts = base - timedelta(seconds=10)
        old_rate = mock_bt.heat_loss_rate
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        mock_bt.clock = FakeClock(now_value=base)
        await self._call(mock_bt)

        assert mock_bt.heat_loss_rate == old_rate
        assert len(mock_bt.last_heat_loss_stats) == 0

    @pytest.mark.asyncio
    async def test_ema_smoothing(self, mock_bt):
        """EMA smoothing applied to heat_loss_rate."""
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        mock_bt.room_temperature = 20.0
        mock_bt.heat_target_temperature = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt._loss_tracker.start_temperature = 22.0
        mock_bt._loss_tracker.start_ts = base - timedelta(minutes=10)
        mock_bt._loss_tracker.end_temperature = 20.0  # 2°C drop in 10 min
        mock_bt._loss_tracker.end_ts = base - timedelta(minutes=2)
        mock_bt.heat_loss_rate = 0.01
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        mock_bt.clock = FakeClock(now_value=base)
        await self._call(mock_bt)

        # rate = 2.0/10.0 = 0.2, old = 0.01, alpha = 0.10
        # new ≈ 0.01 * 0.9 + 0.2 * 0.1 = 0.009 + 0.02 = 0.029
        assert mock_bt.heat_loss_rate > 0.01

    @pytest.mark.asyncio
    async def test_min_max_clamping(self, mock_bt):
        """Loss rate is clamped to [MIN_HEAT_LOSS, MAX_HEAT_LOSS]."""
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        mock_bt.room_temperature = 20.0
        mock_bt.heat_target_temperature = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt._loss_tracker.start_temperature = 22.0
        mock_bt._loss_tracker.start_ts = base - timedelta(minutes=5)
        mock_bt._loss_tracker.end_temperature = 20.0
        mock_bt._loss_tracker.end_ts = base - timedelta(minutes=2)
        mock_bt.heat_loss_rate = 0.0001  # very low
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        mock_bt.clock = FakeClock(now_value=base)
        await self._call(mock_bt)

        # MIN_HEAT_LOSS = 0.001, MAX_HEAT_LOSS = 0.05
        assert mock_bt.heat_loss_rate >= 0.001
        assert mock_bt.heat_loss_rate <= 0.05

    @pytest.mark.asyncio
    async def test_loss_cycle_telemetry(self, mock_bt):
        """Finalized loss cycle appends telemetry to loss_cycles deque."""
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        mock_bt.room_temperature = 20.0
        mock_bt.heat_target_temperature = 22.0
        mock_bt.tolerance = 0.5
        mock_bt._hysteresis.last_action = HVACAction.IDLE
        mock_bt._loss_tracker.start_temperature = 22.0
        mock_bt._loss_tracker.start_ts = base - timedelta(minutes=10)
        mock_bt._loss_tracker.end_temperature = 20.5
        mock_bt._loss_tracker.end_ts = base - timedelta(minutes=2)
        mock_bt._should_heat_with_tolerance = lambda prev, tol: (
            BetterThermostat._should_heat_with_tolerance(mock_bt, prev, tol)
        )

        mock_bt.clock = FakeClock(now_value=base)
        await self._call(mock_bt)

        assert len(mock_bt.loss_cycles) == 1
        cycle = mock_bt.loss_cycles[0]
        assert "rate" in cycle
        assert "start_temperature" in cycle


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
        old_temperature = mock_bt.heat_target_temperature
        await self._call(mock_bt, "nonexistent")
        assert mock_bt.preset_mgr.mode == old_preset
        assert mock_bt.heat_target_temperature == old_temperature

    @pytest.mark.asyncio
    async def test_none_to_comfort(self, mock_bt):
        """NONE → Comfort: saves current temperature, applies configured comfort temperature."""
        mock_bt.preset_modes = [PRESET_NONE, PRESET_COMFORT, PRESET_ECO, PRESET_AWAY]
        mock_bt.preset_mgr.mode = PRESET_NONE
        mock_bt.heat_target_temperature = 20.0
        mock_bt.preset_mgr.saved_temperature = None
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        await self._call(mock_bt, PRESET_COMFORT)
        assert mock_bt.preset_mgr.mode == PRESET_COMFORT
        assert mock_bt.preset_mgr.saved_temperature == 20.0  # saved original
        assert mock_bt.heat_target_temperature == 21.0  # configured comfort temperature

    @pytest.mark.asyncio
    async def test_a_preset_off_the_configured_step_applies_the_same_target_either_way(
        self, mock_bt
    ):
        """A stored preset gives one target, whether selected or set by its number.

        Comfort is stored as the 72 °F a user typed, 22.22 °C. Selecting the
        preset and setting it through its number both put the target on the
        configured 0.5 °C step, 22 °C.
        """
        mock_bt.preset_modes = [PRESET_NONE, PRESET_COMFORT, PRESET_ECO, PRESET_AWAY]
        mock_bt.preset_mgr.mode = PRESET_NONE
        mock_bt.preset_mgr.temperatures[PRESET_COMFORT] = 22.222
        mock_bt.bt_hvac_mode = HVACMode.HEAT
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        mock_bt._configured_temperature_step = 0.5

        await self._call(mock_bt, PRESET_COMFORT)
        selected = mock_bt.heat_target_temperature
        await BetterThermostat.async_set_temperature(
            mock_bt, **{ATTR_TEMPERATURE: 22.222}
        )

        assert selected == 22.0
        assert mock_bt.heat_target_temperature == selected
        assert mock_bt.preset_mgr.mode == PRESET_COMFORT

    @pytest.mark.asyncio
    async def test_a_cooling_preset_is_rounded_onto_the_step(self, mock_bt):
        """A cooling preset lands on the configured step like its heating target.

        Comfort cools to the 77.5 °F a user typed, 25.28 °C; the configured
        0.5 °C step puts the cooling target on 25.5 °C, the value setting the
        same target directly gives.
        """
        mock_bt.preset_modes = [PRESET_NONE, PRESET_COMFORT, PRESET_ECO, PRESET_AWAY]
        mock_bt.preset_mgr.mode = PRESET_NONE
        mock_bt.preset_mgr.temperatures[PRESET_COMFORT] = 21.0
        mock_bt.bt_hvac_mode = HVACMode.HEAT
        mock_bt.cooler_entity_id = "switch.ac"
        mock_bt._preset_cool_temperatures = {PRESET_NONE: 24.0, PRESET_COMFORT: 25.28}
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        mock_bt._configured_temperature_step = 0.5

        await self._call(mock_bt, PRESET_COMFORT)

        assert mock_bt.cool_target_temperature == 25.5

    @pytest.mark.asyncio
    async def test_a_preset_rounded_onto_the_step_stays_inside_the_range(self, mock_bt):
        """A preset at a bound between two steps is not rounded past the bound.

        The range ends at 86.5 °F, 30.28 °C, and Comfort is stored there.
        The configured 0.5 °C step rounds that to 30.5 °C, outside the range;
        the applied target is the bound itself, as for a target set directly,
        and setting that same target again keeps Comfort active.
        """
        mock_bt.preset_modes = [PRESET_NONE, PRESET_COMFORT, PRESET_ECO, PRESET_AWAY]
        mock_bt.preset_mgr.mode = PRESET_NONE
        mock_bt.preset_mgr.temperatures[PRESET_COMFORT] = 30.28
        mock_bt.bt_hvac_mode = HVACMode.HEAT
        mock_bt.bt_max_temp = 30.28
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        mock_bt._configured_temperature_step = 0.5

        await self._call(mock_bt, PRESET_COMFORT)
        selected = mock_bt.heat_target_temperature
        await BetterThermostat.async_set_temperature(
            mock_bt, **{ATTR_TEMPERATURE: 30.28}
        )

        assert selected == 30.28
        assert mock_bt.heat_target_temperature == 30.28
        assert mock_bt.preset_mgr.mode == PRESET_COMFORT

    @pytest.mark.asyncio
    async def test_comfort_to_none_restores(self, mock_bt):
        """Comfort → NONE: heat_target_temperature restored, saved temperature cleared."""
        mock_bt.preset_modes = [PRESET_NONE, PRESET_COMFORT, PRESET_ECO, PRESET_AWAY]
        mock_bt.preset_mgr.mode = PRESET_COMFORT
        mock_bt.preset_mgr.saved_temperature = 20.0
        mock_bt.heat_target_temperature = 21.0
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        await self._call(mock_bt, PRESET_NONE)
        assert mock_bt.preset_mgr.mode == PRESET_NONE
        assert mock_bt.heat_target_temperature == 20.0  # restored
        assert mock_bt.preset_mgr.saved_temperature is None

    @pytest.mark.asyncio
    async def test_comfort_to_eco(self, mock_bt):
        """Comfort → Eco: heat_target_temperature = eco config, saved temperature kept."""
        mock_bt.preset_modes = [PRESET_NONE, PRESET_COMFORT, PRESET_ECO, PRESET_AWAY]
        mock_bt.preset_mgr.mode = PRESET_COMFORT
        mock_bt.preset_mgr.saved_temperature = (
            20.0  # saved from initial manual temperature
        )
        mock_bt.heat_target_temperature = 21.0
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        await self._call(mock_bt, PRESET_ECO)
        assert mock_bt.preset_mgr.mode == PRESET_ECO
        assert mock_bt.heat_target_temperature == 19.0  # eco configured
        assert mock_bt.preset_mgr.saved_temperature == 20.0  # still saved

    @pytest.mark.asyncio
    async def test_eco_to_none_restores_original(self, mock_bt):
        """Eco → NONE: heat_target_temperature = saved original temperature."""
        mock_bt.preset_modes = [PRESET_NONE, PRESET_COMFORT, PRESET_ECO, PRESET_AWAY]
        mock_bt.preset_mgr.mode = PRESET_ECO
        mock_bt.preset_mgr.saved_temperature = 20.0
        mock_bt.heat_target_temperature = 19.0
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        await self._call(mock_bt, PRESET_NONE)
        assert mock_bt.heat_target_temperature == 20.0

    @pytest.mark.asyncio
    async def test_manual_cool_temperature_preserved_across_preset(self, mock_bt):
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
        mock_bt.heat_target_temperature = 20.0
        mock_bt.cool_target_temperature = 26.0  # manual cooling target
        # Entering a preset stashes the manual cool target and applies the preset's.
        await self._call(mock_bt, PRESET_COMFORT)
        assert mock_bt._preset_cool_temperature == 26.0
        assert mock_bt.cool_target_temperature == 24.0
        # Returning to NONE restores the manual cool target, not Comfort's.
        await self._call(mock_bt, PRESET_NONE)
        assert mock_bt.cool_target_temperature == 26.0

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
        mock_bt.bt_target_temperature_step = 0.5
        mock_bt.preset_mgr.mode = PRESET_COMFORT
        mock_bt.preset_mgr.saved_temperature = None
        mock_bt._preset_cool_temperatures = {PRESET_NONE: 24.0, PRESET_COMFORT: 26.0}
        mock_bt._preset_cool_temperature = 22.0
        mock_bt.heat_target_temperature = 24.0
        mock_bt.cool_target_temperature = 26.0

        await self._call(mock_bt, PRESET_NONE)

        assert mock_bt.heat_target_temperature == 24.0
        assert mock_bt.cool_target_temperature == 24.5
        assert (
            mock_bt.bt_min_temp
            <= mock_bt.heat_target_temperature
            <= mock_bt.bt_max_temp
        )
        assert (
            mock_bt.bt_min_temp
            <= mock_bt.cool_target_temperature
            <= mock_bt.bt_max_temp
        )
        assert mock_bt.cool_target_temperature > mock_bt.heat_target_temperature

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
        mock_bt.heat_target_temperature = 20.0
        mock_bt.cool_target_temperature = 25.0

        await self._call(mock_bt, PRESET_NONE)

        assert mock_bt.cool_target_temperature == 26.0
        assert (
            mock_bt.bt_min_temp
            <= mock_bt.cool_target_temperature
            <= mock_bt.bt_max_temp
        )
        assert mock_bt.cool_target_temperature > mock_bt.heat_target_temperature

    @pytest.mark.asyncio
    async def test_preset_temperature_clamped_to_maximum(self, mock_bt):
        """Preset temperature above max → clamped to max_temp."""
        mock_bt.preset_modes = [PRESET_NONE, PRESET_COMFORT, PRESET_ECO, PRESET_AWAY]
        mock_bt.preset_mgr.mode = PRESET_NONE
        mock_bt.preset_mgr.temperatures[PRESET_COMFORT] = 35.0  # above max
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp  # 30.0
        await self._call(mock_bt, PRESET_COMFORT)
        assert mock_bt.heat_target_temperature == 30.0

    @pytest.mark.asyncio
    async def test_a_preset_change_is_stamped_as_a_user_change(self, mock_bt):
        """Choosing a preset marks the moment as the user's change of the target."""
        mock_bt.preset_modes = [PRESET_NONE, PRESET_COMFORT, PRESET_ECO, PRESET_AWAY]
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        mock_bt.clock = FakeClock(monotonic_value=123.0)
        await self._call(mock_bt, PRESET_COMFORT)
        assert mock_bt.last_user_change_monotonic == 123.0

    @pytest.mark.asyncio
    async def test_an_unsupported_preset_is_not_stamped_as_a_user_change(self, mock_bt):
        """A rejected preset changes nothing, so it is no user change either."""
        mock_bt.preset_modes = [PRESET_NONE, PRESET_COMFORT, PRESET_ECO, PRESET_AWAY]
        mock_bt.last_user_change_monotonic = None
        mock_bt.clock = FakeClock(monotonic_value=123.0)
        await self._call(mock_bt, "nonexistent")
        assert mock_bt.last_user_change_monotonic is None

    @pytest.mark.asyncio
    async def test_control_queue_put_called(self, mock_bt):
        """control_queue_task.put is called after preset change."""
        mock_bt.preset_modes = [PRESET_NONE, PRESET_COMFORT, PRESET_ECO, PRESET_AWAY]
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        await self._call(mock_bt, PRESET_COMFORT)
        mock_bt.control_queue_task.put_nowait.assert_called_once_with(mock_bt)


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
        assert mock_bt.heat_target_temperature == 22.0

    @pytest.mark.asyncio
    async def test_a_new_target_is_stamped_as_a_user_change(self, mock_bt):
        """Setting a target marks the moment as the user's change of the target."""
        mock_bt.preset_mgr.mode = PRESET_NONE
        mock_bt.bt_hvac_mode = HVACMode.HEAT
        mock_bt.clock = FakeClock(monotonic_value=123.0)
        await self._call(mock_bt, **{ATTR_TEMPERATURE: 22.0})
        assert mock_bt.last_user_change_monotonic == 123.0

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
    async def test_mode_only_payload_writes_state_and_requests_control(self, mock_bt):
        """A payload with only an hvac_mode publishes the state and queues a cycle."""
        mock_bt.preset_mgr.mode = PRESET_NONE
        mock_bt.bt_hvac_mode = HVACMode.HEAT
        await self._call(mock_bt, **{ATTR_HVAC_MODE: HVACMode.OFF})
        assert mock_bt.bt_hvac_mode == HVACMode.OFF
        mock_bt.async_write_ha_state.assert_called_once()
        mock_bt.control_queue_task.put_nowait.assert_called_once_with(mock_bt)

    @pytest.mark.asyncio
    async def test_mode_only_payload_during_maintenance_defers_control(self, mock_bt):
        """A mode-only payload during maintenance defers the cycle, no queue.put."""
        mock_bt.preset_mgr.mode = PRESET_NONE
        mock_bt.bt_hvac_mode = HVACMode.HEAT
        mock_bt.in_maintenance = True
        await self._call(mock_bt, **{ATTR_HVAC_MODE: HVACMode.OFF})
        assert mock_bt.bt_hvac_mode == HVACMode.OFF
        mock_bt.async_write_ha_state.assert_called_once()
        assert mock_bt._control_needed_after_maintenance is True
        mock_bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_payload_is_ignored(self, mock_bt):
        """A payload without temperature and without hvac_mode changes nothing."""
        mock_bt.preset_mgr.mode = PRESET_NONE
        mock_bt.bt_hvac_mode = HVACMode.HEAT
        await self._call(mock_bt)
        assert mock_bt.bt_hvac_mode == HVACMode.HEAT
        mock_bt.async_write_ha_state.assert_not_called()
        mock_bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_garbage_temperature_rejected(self, mock_bt):
        """A present but non-numeric temperature raises.

        Garbage input is a caller error, not something to drop silently.
        """
        mock_bt.preset_mgr.mode = PRESET_NONE
        mock_bt.bt_hvac_mode = HVACMode.HEAT
        mock_bt.heat_target_temperature = 21.0
        with pytest.raises(ServiceValidationError):
            await self._call(mock_bt, **{ATTR_TEMPERATURE: "warm"})
        assert mock_bt.heat_target_temperature == 21.0

    @pytest.mark.asyncio
    async def test_none_temperature_rejected(self, mock_bt):
        """An explicit None temperature raises a validation error."""
        mock_bt.preset_mgr.mode = PRESET_NONE
        mock_bt.bt_hvac_mode = HVACMode.HEAT
        with pytest.raises(ServiceValidationError):
            await self._call(mock_bt, **{ATTR_TEMPERATURE: None})

    @pytest.mark.asyncio
    async def test_unsupported_hvac_mode_in_kwargs_rejected(self, mock_bt):
        """An unsupported hvac_mode in kwargs raises and changes nothing."""
        mock_bt.preset_mgr.mode = PRESET_NONE
        mock_bt.bt_hvac_mode = HVACMode.HEAT
        with pytest.raises(ServiceValidationError):
            await self._call(mock_bt, **{ATTR_TEMPERATURE: 22.0, ATTR_HVAC_MODE: "dry"})
        assert mock_bt.bt_hvac_mode == HVACMode.HEAT

    @pytest.mark.asyncio
    async def test_valid_hvac_with_invalid_temperature_is_atomic(self, mock_bt):
        """Valid hvac_mode + invalid temperature must not partially apply state."""
        mock_bt.preset_mgr.mode = PRESET_NONE
        mock_bt.bt_hvac_mode = HVACMode.HEAT
        with pytest.raises(ServiceValidationError):
            await self._call(
                mock_bt, **{ATTR_HVAC_MODE: HVACMode.OFF, ATTR_TEMPERATURE: "warm"}
            )
        assert mock_bt.bt_hvac_mode == HVACMode.HEAT

    @pytest.mark.asyncio
    async def test_heat_cool_low_high_setpoints(self, mock_bt):
        """HEAT_COOL with low/high setpoints."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_hvac_mode = HVACMode.HEAT
        mock_bt.preset_mgr.mode = PRESET_NONE
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        await self._call(
            mock_bt, **{ATTR_TARGET_TEMP_LOW: 20.0, ATTR_TARGET_TEMP_HIGH: 26.0}
        )
        assert mock_bt.heat_target_temperature == 20.0
        assert mock_bt.cool_target_temperature == 26.0

    @pytest.mark.asyncio
    async def test_cool_target_enforced_above_heat(self, mock_bt):
        """Cool target adjusted to be above heat target in HEAT_COOL mode."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_hvac_mode = HVACMode.HEAT
        mock_bt.preset_mgr.mode = PRESET_NONE
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        mock_bt.heat_target_temperature = 22.0
        mock_bt.cool_target_temperature = 20.0  # below heat target → should be adjusted
        await self._call(mock_bt, **{ATTR_TEMPERATURE: 22.0})
        assert mock_bt.cool_target_temperature > mock_bt.heat_target_temperature

    @pytest.mark.asyncio
    async def test_a_cooling_only_target_moves_the_heating_target_below_it(
        self, mock_bt
    ):
        """A cooling target set on its own is kept; the heating target yields."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_hvac_mode = HVACMode.HEAT_COOL
        mock_bt.preset_mgr.mode = PRESET_NONE
        mock_bt.heat_target_temperature = 22.0
        mock_bt.cool_target_temperature = 26.0
        await self._call(mock_bt, **{ATTR_TARGET_TEMP_HIGH: 21.0})
        assert mock_bt.cool_target_temperature == 21.0
        assert mock_bt.heat_target_temperature == 20.5

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("cooling_target", "preset", "heating_target", "manual_target"),
        [
            pytest.param(21.0, PRESET_NONE, 20.5, 20.5, id="lowered_leaves_it"),
            pytest.param(25.0, PRESET_COMFORT, 22.0, 20.0, id="untouched_keeps_it"),
        ],
    )
    async def test_a_cooling_only_target_leaves_a_preset_only_by_moving_heating(
        self, mock_bt, cooling_target, preset, heating_target, manual_target
    ):
        """A preset is left when the ordering moves its heating target, not before.

        The lowered heating target is recorded as the manual one, which is the
        target the thermostat restores in PRESET_NONE.
        """
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_hvac_mode = HVACMode.HEAT_COOL
        mock_bt.preset_mgr.mode = PRESET_COMFORT
        mock_bt.preset_mgr.saved_temperature = 20.0
        mock_bt.preset_mgr.temperatures[PRESET_COMFORT] = 22.0
        mock_bt.heat_target_temperature = 22.0
        mock_bt.cool_target_temperature = 26.0
        await self._call(mock_bt, **{ATTR_TARGET_TEMP_HIGH: cooling_target})
        assert mock_bt.cool_target_temperature == cooling_target
        assert mock_bt.heat_target_temperature == heating_target
        assert mock_bt.preset_mgr.mode == preset
        assert mock_bt.preset_mgr.temperatures[PRESET_NONE] == manual_target
        assert mock_bt.preset_mgr.temperatures[PRESET_COMFORT] == 22.0

    @pytest.mark.asyncio
    async def test_min_max_clamping(self, mock_bt):
        """Temperature clamped to min/max bounds."""
        mock_bt.preset_mgr.mode = PRESET_NONE
        mock_bt.min_temp = mock_bt.bt_min_temp  # 5.0
        mock_bt.max_temp = mock_bt.bt_max_temp  # 30.0
        await self._call(mock_bt, **{ATTR_TEMPERATURE: 50.0})
        assert mock_bt.heat_target_temperature == 30.0

    @pytest.mark.asyncio
    async def test_min_clamping(self, mock_bt):
        """Temperature below min → clamped to min."""
        mock_bt.preset_mgr.mode = PRESET_NONE
        mock_bt.min_temp = mock_bt.bt_min_temp  # 5.0
        mock_bt.max_temp = mock_bt.bt_max_temp  # 30.0
        await self._call(mock_bt, **{ATTR_TEMPERATURE: 1.0})
        assert mock_bt.heat_target_temperature == 5.0

    @pytest.mark.asyncio
    async def test_preset_none_stored_temperature_updated(self, mock_bt):
        """In PRESET_NONE, stored temperature is updated on manual change."""
        mock_bt.preset_mgr.mode = PRESET_NONE
        mock_bt.heat_target_temperature = 20.0
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
        mock_bt.control_queue_task.put_nowait.assert_not_called()

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
        mock_bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_queue_put_called_in_heat_mode(self, mock_bt):
        """In HEAT mode, queue.put IS called."""
        mock_bt.preset_mgr.mode = PRESET_NONE
        mock_bt.bt_hvac_mode = HVACMode.HEAT
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        await self._call(mock_bt, **{ATTR_TEMPERATURE: 22.0})
        mock_bt.control_queue_task.put_nowait.assert_called_once_with(mock_bt)

    @pytest.mark.asyncio
    async def test_active_preset_deactivated_on_manual_change(self, mock_bt):
        """Changing target temperature while a preset is active deactivates it (back to NONE)."""
        mock_bt.preset_mgr.mode = PRESET_COMFORT
        mock_bt.preset_mgr.saved_temperature = 20.0
        mock_bt.preset_mgr.temperatures[PRESET_COMFORT] = 21.0
        mock_bt.heat_target_temperature = 21.0
        mock_bt.bt_hvac_mode = HVACMode.HEAT
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        await self._call(mock_bt, **{ATTR_TEMPERATURE: 19.0})
        assert mock_bt.preset_mgr.mode == PRESET_NONE
        assert mock_bt.preset_mgr.saved_temperature is None
        assert mock_bt.heat_target_temperature == 19.0
        # New manual value is stored as the PRESET_NONE temperature
        assert mock_bt.preset_mgr.temperatures[PRESET_NONE] == 19.0
        # Stored Comfort preset temperature is left untouched
        assert mock_bt.preset_mgr.temperatures[PRESET_COMFORT] == 21.0

    @pytest.mark.asyncio
    async def test_active_preset_kept_when_new_temperature_matches_stored(
        self, mock_bt
    ):
        """Setting temperature to the preset's stored value (e.g. from its Number entity) keeps the preset active."""
        mock_bt.preset_mgr.mode = PRESET_COMFORT
        mock_bt.preset_mgr.saved_temperature = 20.0
        mock_bt.preset_mgr.temperatures[PRESET_COMFORT] = 22.5
        mock_bt.heat_target_temperature = 21.0
        mock_bt.bt_hvac_mode = HVACMode.HEAT
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        await self._call(mock_bt, **{ATTR_TEMPERATURE: 22.5})
        assert mock_bt.preset_mgr.mode == PRESET_COMFORT
        assert mock_bt.preset_mgr.saved_temperature == 20.0
        assert mock_bt.heat_target_temperature == 22.5

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("configured_step", "requested", "applied"),
        [
            # 68.4 °F from a 0.9 °F frontend step, onto the 0.5 °C grid.
            pytest.param(0.5, 20.222, 20.0, id="off_grid_onto_the_step"),
            pytest.param(0.5, 20.5, 20.5, id="on_grid_unchanged"),
            pytest.param(0.1, 20.3, 20.3, id="tenths_on_grid_unchanged"),
            pytest.param(None, 20.22, 20.22, id="no_configured_step"),
            # Held as a clean grid value, not 21.200000000000003.
            pytest.param(0.2, 21.11, 21.2, id="clean_grid_value"),
        ],
    )
    async def test_a_target_is_rounded_onto_the_configured_step(
        self, mock_bt, configured_step, requested, applied
    ):
        """A requested target lands on the configured step, once, on the way in.

        Home Assistant converts a target set in Fahrenheit into Celsius before
        handing it over, so a target on the Fahrenheit slider arrives between
        two points of a Celsius step. A target already on the step, and every
        target when no step is configured, is kept exactly as requested.
        """
        mock_bt.preset_mgr.mode = PRESET_NONE
        mock_bt.bt_hvac_mode = HVACMode.HEAT
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        mock_bt._configured_temperature_step = configured_step
        await self._call(mock_bt, **{ATTR_TEMPERATURE: requested})
        assert mock_bt.heat_target_temperature == applied
        assert repr(mock_bt.heat_target_temperature) == repr(applied)

    @pytest.mark.asyncio
    async def test_a_preset_off_the_configured_step_stays_active_when_applied(
        self, mock_bt
    ):
        """A preset stored between two steps stays active when its number applies it.

        The preset number stores what the user typed, 72 °F = 22.22 °C, and
        applies it as a target, which lands on the configured step. The
        preset is still the one running.
        """
        mock_bt.preset_mgr.mode = PRESET_COMFORT
        mock_bt.preset_mgr.temperatures[PRESET_COMFORT] = 22.222
        mock_bt.bt_hvac_mode = HVACMode.HEAT
        mock_bt.min_temp = mock_bt.bt_min_temp
        mock_bt.max_temp = mock_bt.bt_max_temp
        mock_bt._configured_temperature_step = 0.5
        await self._call(mock_bt, **{ATTR_TEMPERATURE: 22.222})
        assert mock_bt.heat_target_temperature == 22.0
        assert mock_bt.preset_mgr.mode == PRESET_COMFORT

    @pytest.mark.asyncio
    async def test_preset_none_change_does_not_trigger_deactivation_path(self, mock_bt):
        """In PRESET_NONE, the deactivation branch is skipped and stored temperature is updated."""
        mock_bt.preset_mgr.mode = PRESET_NONE
        mock_bt.preset_mgr.saved_temperature = None
        mock_bt.heat_target_temperature = 20.0
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

    def _call(self, bt, **kwargs):
        return BetterThermostat._enforce_cool_above_heat(bt, **kwargs)

    def test_not_heat_cool_mode_is_noop(self, mock_bt):
        """Outside HEAT_COOL the cool target is left untouched even if below heat."""
        mock_bt.hvac_mode = HVACMode.HEAT
        mock_bt.heat_target_temperature = 22.0
        mock_bt.cool_target_temperature = 20.0
        self._call(mock_bt)
        assert mock_bt.cool_target_temperature == 20.0

    def test_regardless_of_hvac_mode_skips_the_mode_gate(self, mock_bt):
        """The flag orders the pair in a mode that does not cool at all."""
        mock_bt.hvac_mode = HVACMode.OFF
        mock_bt.heat_target_temperature = 22.0
        mock_bt.bt_target_temperature_step = 0.5
        mock_bt.cool_target_temperature = 20.0
        self._call(mock_bt, regardless_of_hvac_mode=True)
        assert mock_bt.cool_target_temperature == 22.5

    def test_regardless_of_hvac_mode_keeps_the_none_guards(self, mock_bt):
        """Without both targets there is no pair to order."""
        mock_bt.hvac_mode = HVACMode.OFF
        mock_bt.heat_target_temperature = 22.0
        mock_bt.cool_target_temperature = None
        self._call(mock_bt, regardless_of_hvac_mode=True)
        assert mock_bt.cool_target_temperature is None

        mock_bt.heat_target_temperature = None
        mock_bt.cool_target_temperature = 20.0
        self._call(mock_bt, regardless_of_hvac_mode=True)
        assert mock_bt.cool_target_temperature == 20.0

    def test_regardless_of_hvac_mode_keeps_the_ordering_guard(self, mock_bt):
        """A pair already in order is left alone whatever the mode is."""
        mock_bt.hvac_mode = HVACMode.OFF
        mock_bt.heat_target_temperature = 22.0
        mock_bt.cool_target_temperature = 26.0
        self._call(mock_bt, regardless_of_hvac_mode=True)
        assert mock_bt.cool_target_temperature == 26.0

    def test_default_is_mode_gated(self, mock_bt):
        """A caller that omits the flag is gated on HEAT_COOL."""
        mock_bt.hvac_mode = HVACMode.OFF
        mock_bt.heat_target_temperature = 22.0
        mock_bt.bt_target_temperature_step = 0.5
        mock_bt.cool_target_temperature = 20.0
        self._call(mock_bt)
        assert mock_bt.cool_target_temperature == 20.0

    def test_cool_above_heat_is_noop(self, mock_bt):
        """A cool target already above the heat target is unchanged."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.heat_target_temperature = 22.0
        mock_bt.cool_target_temperature = 26.0
        self._call(mock_bt)
        assert mock_bt.cool_target_temperature == 26.0

    def test_cool_below_heat_is_bumped_by_step(self, mock_bt):
        """A cool target below the heat target is bumped up by one step."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.heat_target_temperature = 22.0
        mock_bt.bt_target_temperature_step = 0.5
        mock_bt.cool_target_temperature = 20.0
        self._call(mock_bt)
        assert mock_bt.cool_target_temperature == 22.5

    def test_cool_equal_heat_is_bumped(self, mock_bt):
        """A cool target equal to the heat target is bumped above it."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.heat_target_temperature = 22.0
        mock_bt.bt_target_temperature_step = 0.5
        mock_bt.cool_target_temperature = 22.0
        self._call(mock_bt)
        assert mock_bt.cool_target_temperature == 22.5

    def test_step_falls_back_to_half_degree(self, mock_bt):
        """A missing/zero step falls back to 0.5."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.heat_target_temperature = 22.0
        mock_bt.bt_target_temperature_step = 0
        mock_bt.cool_target_temperature = 21.0
        self._call(mock_bt)
        assert mock_bt.cool_target_temperature == 22.5

    @pytest.mark.parametrize("step", UNUSABLE_STEPS)
    def test_unusable_step_falls_back_to_the_default_step(self, mock_bt, caplog, step):
        """An unusable step falls back to the default before the cool target moves.

        ``bt_target_temperature_step`` carries whatever the child entities report, and
        only a positive real step can lift the cool target above the heat
        target, so a negative or non-finite one is replaced by the 0.5 default.
        The warning names the value the cool target comes to rest on.
        """
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_max_temp = 30.0
        mock_bt.heat_target_temperature = 22.0
        mock_bt.bt_target_temperature_step = step
        mock_bt.cool_target_temperature = 21.0

        with caplog.at_level(logging.WARNING):
            self._call(mock_bt)

        assert mock_bt.cool_target_temperature == 22.5
        assert mock_bt.cool_target_temperature > mock_bt.heat_target_temperature
        assert (
            "cooling target 21.00 adjusted to 22.50 to stay above heating "
            "target 22.00" in caplog.text
        )

    def test_none_cool_target_is_noop(self, mock_bt):
        """A None cool target does not raise and stays None."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.heat_target_temperature = 22.0
        mock_bt.cool_target_temperature = None
        self._call(mock_bt)
        assert mock_bt.cool_target_temperature is None

    def test_bump_stops_at_the_configured_maximum(self, mock_bt):
        """A heat target within one step of the maximum shortens the bump.

        The bumped value is written to the cooler and published as the upper end
        of the range, so it has to stay inside the configured bounds; a step
        short of the maximum still clears the heat target.
        """
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_max_temp = 30.0
        mock_bt.heat_target_temperature = 29.8
        mock_bt.bt_target_temperature_step = 0.5
        mock_bt.cool_target_temperature = 29.0
        self._call(mock_bt)
        assert mock_bt.cool_target_temperature == 30.0
        assert mock_bt.cool_target_temperature > mock_bt.heat_target_temperature
        assert mock_bt.cool_target_temperature <= mock_bt.bt_max_temp

    def test_heat_target_at_the_maximum_meets_the_cool_target_there(
        self, mock_bt, caplog
    ):
        """At the maximum the range holds no value above the heat target.

        The pair cannot be ordered without leaving the range, and the maximum is
        the closest the cool target can come while staying writable, so the two
        targets come to rest on the same value.
        """
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_max_temp = 30.0
        mock_bt.heat_target_temperature = 30.0
        mock_bt.bt_target_temperature_step = 0.5
        mock_bt.cool_target_temperature = 28.0
        caplog.set_level(logging.WARNING)
        self._call(mock_bt)
        assert mock_bt.cool_target_temperature == 30.0
        assert (
            "cooling target 28.00 raised to the configured maximum 30.00, which "
            "the heating target occupies as well, because the range holds no "
            "value above it" in caplog.text
        )

    def test_pair_resting_on_the_maximum_is_left_alone(self, mock_bt, caplog):
        """Both targets at the maximum have nowhere left to move."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_max_temp = 30.0
        mock_bt.heat_target_temperature = 30.0
        mock_bt.bt_target_temperature_step = 0.5
        mock_bt.cool_target_temperature = 30.0
        caplog.set_level(logging.WARNING)
        self._call(mock_bt)
        assert mock_bt.cool_target_temperature == 30.0
        assert caplog.text == ""

    def test_maximum_below_the_heat_target_does_not_cap(self, mock_bt, caplog):
        """A maximum the heat target already exceeds cannot bound the bump.

        Capping there would put the cool target below the heat target and
        publish the inverted pair this method exists to prevent, so the ordering
        keeps precedence and the bump runs its full step.
        """
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_max_temp = 18.0
        mock_bt.heat_target_temperature = 22.0
        mock_bt.bt_target_temperature_step = 0.5
        mock_bt.cool_target_temperature = 17.0
        caplog.set_level(logging.WARNING)
        self._call(mock_bt)
        assert mock_bt.cool_target_temperature == 22.5
        assert mock_bt.cool_target_temperature > mock_bt.heat_target_temperature
        assert "adjusted to 22.50 to stay above heating target 22.00" in caplog.text
        assert "raised to the configured maximum" not in caplog.text

    def test_cool_target_above_the_maximum_is_raised_not_lowered(self, mock_bt):
        """A cool target outside the range is still ordered above the heat one.

        Both targets sit above the maximum, so pulling the cool one down to it
        would drop it below the heat target instead of bringing the pair back
        into range.
        """
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_max_temp = 30.0
        mock_bt.heat_target_temperature = 32.0
        mock_bt.bt_target_temperature_step = 0.5
        mock_bt.cool_target_temperature = 31.0
        self._call(mock_bt)
        assert mock_bt.cool_target_temperature == 32.5

    def test_without_a_maximum_the_bump_is_uncapped(self, mock_bt):
        """No maximum is known until a child entity reports one."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_max_temp = None
        mock_bt.heat_target_temperature = 29.8
        mock_bt.bt_target_temperature_step = 0.5
        mock_bt.cool_target_temperature = 29.0
        self._call(mock_bt)
        assert mock_bt.cool_target_temperature == 30.3


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
        mock_bt.heat_target_temperature = 22.0
        mock_bt.cool_target_temperature = 20.0
        self._call(mock_bt)
        assert mock_bt.heat_target_temperature == 22.0

    def test_heat_below_cool_is_noop(self, mock_bt):
        """A heat target already below the cool target is unchanged."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.heat_target_temperature = 20.0
        mock_bt.cool_target_temperature = 24.0
        self._call(mock_bt)
        assert mock_bt.heat_target_temperature == 20.0

    def test_heat_above_cool_is_pushed_down_by_step(self, mock_bt):
        """A heat target above the cool target drops one step below it."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.heat_target_temperature = 24.0
        mock_bt.bt_target_temperature_step = 0.5
        mock_bt.cool_target_temperature = 22.0
        self._call(mock_bt)
        assert mock_bt.heat_target_temperature == 21.5

    def test_heat_equal_cool_is_pushed_down(self, mock_bt):
        """A heat target equal to the cool target is pushed below it."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.heat_target_temperature = 22.0
        mock_bt.bt_target_temperature_step = 0.5
        mock_bt.cool_target_temperature = 22.0
        self._call(mock_bt)
        assert mock_bt.heat_target_temperature == 21.5

    def test_step_falls_back_to_half_degree(self, mock_bt):
        """A missing/zero step falls back to 0.5."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.heat_target_temperature = 22.0
        mock_bt.bt_target_temperature_step = 0
        mock_bt.cool_target_temperature = 22.0
        self._call(mock_bt)
        assert mock_bt.heat_target_temperature == 21.5

    @pytest.mark.parametrize("step", UNUSABLE_STEPS)
    def test_unusable_step_falls_back_to_the_default_step(self, mock_bt, caplog, step):
        """An unusable step falls back to the default before the heat target moves.

        ``bt_target_temperature_step`` carries whatever the child entities report, and
        only a positive real step can push the heat target below the cool
        target, so a negative or non-finite one is replaced by the 0.5 default.
        The warning names the value the heat target comes to rest on.
        """
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_min_temp = 5.0
        mock_bt.heat_target_temperature = 22.0
        mock_bt.bt_target_temperature_step = step
        mock_bt.cool_target_temperature = 22.0

        with caplog.at_level(logging.WARNING):
            self._call(mock_bt)

        assert mock_bt.heat_target_temperature == 21.5
        assert mock_bt.heat_target_temperature < mock_bt.cool_target_temperature
        assert (
            "heating target 22.00 adjusted to 21.50 to stay below cooling "
            "target 22.00" in caplog.text
        )

    def test_result_is_clamped_to_min_temperature(self, mock_bt, caplog):
        """The heat target never drops below the configured minimum.

        A cool target resting on the minimum leaves no value below it inside the
        range, so the two targets meet there and the annunciation says so rather
        than claiming an ordering the values do not have.
        """
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.heat_target_temperature = 6.0
        mock_bt.bt_target_temperature_step = 0.5
        mock_bt.bt_min_temp = 5.0
        mock_bt.cool_target_temperature = 5.0
        caplog.set_level(logging.WARNING)
        self._call(mock_bt)
        assert mock_bt.heat_target_temperature == 5.0
        assert (
            "heating target 6.00 set to the configured minimum 5.00, which is "
            "not below the cooling target 5.00" in caplog.text
        )
        assert "to stay below cooling target" not in caplog.text

    def test_minimum_above_the_cool_target_is_reported_as_an_overlap(
        self, mock_bt, caplog
    ):
        """A cool target under the minimum pins the heat target above it.

        The configured range is the overlap of what the child entities
        advertise, so children whose ranges do not overlap leave the minimum
        above the maximum. Both bounds are applied to an inbound setpoint in
        sequence and the maximum decides, which puts the adopted cool target on
        the maximum, below the minimum. The heat target stops on the minimum and
        the pair overlaps; the annunciation names the value that was stored
        rather than reporting an ordering it does not have.
        """
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.heat_target_temperature = 22.0
        mock_bt.bt_target_temperature_step = 0.5
        mock_bt.bt_min_temp = 20.0
        mock_bt.cool_target_temperature = 15.0
        caplog.set_level(logging.WARNING)
        self._call(mock_bt)
        assert mock_bt.heat_target_temperature == 20.0
        assert (
            "heating target 22.00 set to the configured minimum 20.00, which is "
            "not below the cooling target 15.00" in caplog.text
        )

    def test_ordered_result_is_reported_as_ordered(self, mock_bt, caplog):
        """A heat target that ends up below the cool target reports the ordering."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.heat_target_temperature = 24.0
        mock_bt.bt_target_temperature_step = 0.5
        mock_bt.bt_min_temp = 5.0
        mock_bt.cool_target_temperature = 22.0
        caplog.set_level(logging.WARNING)
        self._call(mock_bt)
        assert mock_bt.heat_target_temperature == 21.5
        assert (
            "heating target 24.00 adjusted to 21.50 to stay below cooling "
            "target 22.00" in caplog.text
        )
        assert "configured minimum" not in caplog.text

    def test_pair_resting_on_the_minimum_is_left_alone(self, mock_bt, caplog):
        """Both targets at the minimum have nowhere left to move."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.heat_target_temperature = 5.0
        mock_bt.bt_target_temperature_step = 0.5
        mock_bt.bt_min_temp = 5.0
        mock_bt.cool_target_temperature = 5.0
        caplog.set_level(logging.WARNING)
        self._call(mock_bt)
        assert mock_bt.heat_target_temperature == 5.0
        assert caplog.text == ""

    def test_none_heat_target_is_noop(self, mock_bt):
        """A None heat target does not raise and stays None."""
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.heat_target_temperature = None
        mock_bt.cool_target_temperature = 22.0
        self._call(mock_bt)
        assert mock_bt.heat_target_temperature is None


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
    """_clamp_inbound_cool_target raises a cooler report above the heat target."""

    def _call(self, bt, value):
        return BetterThermostat._clamp_inbound_cool_target(bt, value)

    def test_without_a_cooler_is_noop(self, mock_bt):
        """Without a cooling channel there is no second bound."""
        mock_bt.cooler_entity_id = None
        mock_bt.heat_target_temperature = 22.0
        assert self._call(mock_bt, 20.0) == 20.0

    def test_a_group_that_is_off_still_keeps_the_targets_apart(self, mock_bt):
        """The ordering is a property of the configuration, not of the mode.

        A crossing adopted while the group is off is never revisited, because
        the ordering fallbacks only run inside HEAT_COOL.
        """
        mock_bt.cooler_entity_id = "climate.ac"
        mock_bt.hvac_mode = HVACMode.OFF
        mock_bt.heat_target_temperature = 22.0
        assert self._call(mock_bt, 20.0) == 22.5

    def test_none_heat_target_is_noop(self, mock_bt):
        """Without a heating target there is no bound to clamp against."""
        mock_bt.cooler_entity_id = "climate.ac"
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.heat_target_temperature = None
        assert self._call(mock_bt, 20.0) == 20.0

    def test_value_above_heat_target_is_kept(self, mock_bt):
        """A value that already clears the heating target is returned unchanged."""
        mock_bt.cooler_entity_id = "climate.ac"
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.heat_target_temperature = 22.0
        assert self._call(mock_bt, 26.0) == 26.0

    def test_value_below_heat_target_is_raised_by_one_step(self, mock_bt):
        """A value below the heating target is raised just above it."""
        mock_bt.cooler_entity_id = "climate.ac"
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.heat_target_temperature = 22.0
        mock_bt.bt_target_temperature_step = 0.5
        assert self._call(mock_bt, 18.0) == 22.5

    def test_value_equal_to_heat_target_is_raised(self, mock_bt):
        """A value on the heating target still has to clear it."""
        mock_bt.cooler_entity_id = "climate.ac"
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.heat_target_temperature = 22.0
        mock_bt.bt_target_temperature_step = 0.5
        assert self._call(mock_bt, 22.0) == 22.5

    def test_step_falls_back_to_half_degree(self, mock_bt):
        """A missing/zero step falls back to 0.5."""
        mock_bt.cooler_entity_id = "climate.ac"
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.heat_target_temperature = 22.0
        mock_bt.bt_target_temperature_step = 0
        assert self._call(mock_bt, 22.0) == 22.5

    @pytest.mark.parametrize("step", UNUSABLE_STEPS)
    def test_unusable_step_falls_back_to_the_default_step(self, mock_bt, step):
        """An unusable step falls back to the default before the floor is built.

        ``bt_target_temperature_step`` carries whatever the child entities report, and
        only a positive real step puts the floor above the heating target, so a
        negative or non-finite one is replaced by the 0.5 default.
        """
        mock_bt.cooler_entity_id = "climate.ac"
        mock_bt.bt_max_temp = 30.0
        mock_bt.heat_target_temperature = 20.0
        mock_bt.bt_target_temperature_step = step
        adopted = self._call(mock_bt, 18.0)
        assert adopted == 20.5
        assert adopted > mock_bt.heat_target_temperature

    def test_floor_stays_inside_the_configured_range(self, mock_bt):
        """With the heating target at the maximum the floor stops there.

        An unbounded floor would leave the configured range, so the value stops
        at bt_max_temp and the ordering fallback settles the rest.
        """
        mock_bt.cooler_entity_id = "climate.ac"
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.heat_target_temperature = 30.0
        mock_bt.bt_max_temp = 30.0
        mock_bt.bt_target_temperature_step = 0.5
        assert self._call(mock_bt, 22.0) == 30.0


# ===========================================================================
# 11. TestClampInboundHeatTarget
# ===========================================================================


class TestClampInboundHeatTarget:
    """_clamp_inbound_heat_target lowers a TRV report below the cool target."""

    def _call(self, bt, value):
        return BetterThermostat._clamp_inbound_heat_target(bt, value)

    def test_without_a_cooler_is_noop(self, mock_bt):
        """Without a cooling channel there is no second bound."""
        mock_bt.cooler_entity_id = None
        mock_bt.cool_target_temperature = 22.0
        assert self._call(mock_bt, 24.0) == 24.0

    def test_a_group_that_is_off_still_keeps_the_targets_apart(self, mock_bt):
        """A valve reporting while the group is off must not cross the cool target.

        A valve with ``no_off_system_mode`` reports its knob turn while
        ``bt_hvac_mode`` is still OFF, and the same event then resolves the mode.
        """
        mock_bt.cooler_entity_id = "climate.ac"
        mock_bt.hvac_mode = HVACMode.OFF
        mock_bt.cool_target_temperature = 22.0
        assert self._call(mock_bt, 24.0) == 21.5

    def test_none_cool_target_is_noop(self, mock_bt):
        """Without a cooling target there is no bound to clamp against."""
        mock_bt.cooler_entity_id = "climate.ac"
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.cool_target_temperature = None
        assert self._call(mock_bt, 24.0) == 24.0

    def test_value_below_cool_target_is_kept(self, mock_bt):
        """A value that already clears the cooling target is returned unchanged."""
        mock_bt.cooler_entity_id = "climate.ac"
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.cool_target_temperature = 26.0
        assert self._call(mock_bt, 22.0) == 22.0

    def test_value_above_cool_target_is_lowered_by_one_step(self, mock_bt):
        """A value above the cooling target is lowered just below it."""
        mock_bt.cooler_entity_id = "climate.ac"
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.cool_target_temperature = 22.0
        mock_bt.bt_target_temperature_step = 0.5
        assert self._call(mock_bt, 26.0) == 21.5

    def test_value_equal_to_cool_target_is_lowered(self, mock_bt):
        """A value on the cooling target still has to clear it."""
        mock_bt.cooler_entity_id = "climate.ac"
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.cool_target_temperature = 22.0
        mock_bt.bt_target_temperature_step = 0.5
        assert self._call(mock_bt, 22.0) == 21.5

    def test_step_falls_back_to_half_degree(self, mock_bt):
        """A missing/zero step falls back to 0.5."""
        mock_bt.cooler_entity_id = "climate.ac"
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.cool_target_temperature = 22.0
        mock_bt.bt_target_temperature_step = 0
        assert self._call(mock_bt, 22.0) == 21.5

    @pytest.mark.parametrize("step", UNUSABLE_STEPS)
    def test_unusable_step_falls_back_to_the_default_step(self, mock_bt, step):
        """An unusable step falls back to the default before the ceiling is built.

        ``bt_target_temperature_step`` carries whatever the child entities report, and
        only a positive real step puts the ceiling below the cooling target, so
        a negative or non-finite one is replaced by the 0.5 default.
        """
        mock_bt.cooler_entity_id = "climate.ac"
        mock_bt.bt_min_temp = 5.0
        mock_bt.cool_target_temperature = 24.0
        mock_bt.bt_target_temperature_step = step
        adopted = self._call(mock_bt, 26.0)
        assert adopted == 23.5
        assert adopted < mock_bt.cool_target_temperature

    def test_ceiling_stays_inside_the_configured_range(self, mock_bt):
        """With the cooling target at the minimum the ceiling stops there.

        An unbounded ceiling would leave the configured range, so the value stops
        at bt_min_temp and the ordering fallback settles the rest.
        """
        mock_bt.cooler_entity_id = "climate.ac"
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.cool_target_temperature = 5.0
        mock_bt.bt_min_temp = 5.0
        mock_bt.bt_target_temperature_step = 0.5
        assert self._call(mock_bt, 22.0) == 5.0


# ===========================================================================
# 12. TestSeedCoolTarget
# ===========================================================================


class TestSeedCoolTarget:
    """_seed_cool_target adopts a cooler's own setpoint as the cool target."""

    COOLER_ID = "climate.test_cooler"

    def _call(self, bt, setpoint):
        return BetterThermostat._seed_cool_target(bt, setpoint, self.COOLER_ID)

    def test_in_range_setpoint_is_stored_and_reported(self, mock_bt, caplog):
        """Taking a device value as the target is worth an info entry."""
        mock_bt.heat_target_temperature = 20.0
        caplog.set_level(logging.INFO)
        self._call(
            mock_bt, InboundSetpoint(raw=24.0, value=24.0, clamped=False, is_echo=False)
        )
        assert mock_bt.cool_target_temperature == 24.0
        assert (
            "reports setpoint 24.0 while the cool target is unknown, taking it "
            "as the cool target" in caplog.text
        )
        assert "outside of range" not in caplog.text

    def test_clamped_setpoint_stores_the_clamped_value_and_warns(self, mock_bt, caplog):
        """The stored target is written back, so the substitution must be visible."""
        mock_bt.heat_target_temperature = 20.0
        caplog.set_level(logging.WARNING)
        self._call(
            mock_bt, InboundSetpoint(raw=31.0, value=30.0, clamped=True, is_echo=False)
        )
        assert mock_bt.cool_target_temperature == 30.0
        assert (
            "reported setpoint 31.0 outside of range while the cool target is "
            "unknown, taking 30.0 as the cool target" in caplog.text
        )

    def test_setpoint_colliding_with_the_heat_target_is_lifted(self, mock_bt):
        """The observed value yields, the heating target the user set stays."""
        mock_bt.hvac_mode = HVACMode.HEAT
        mock_bt.heat_target_temperature = 22.0
        mock_bt.bt_target_temperature_step = 0.5
        self._call(
            mock_bt, InboundSetpoint(raw=21.0, value=21.0, clamped=False, is_echo=False)
        )
        assert mock_bt.cool_target_temperature == 22.5
        assert mock_bt.heat_target_temperature == 22.0

    def test_setpoint_taken_while_off_is_lifted(self, mock_bt):
        """A seed taken while BT is off is the one the first cooling cycle uses."""
        mock_bt.hvac_mode = HVACMode.OFF
        mock_bt.heat_target_temperature = 22.0
        mock_bt.bt_target_temperature_step = 0.5
        self._call(
            mock_bt, InboundSetpoint(raw=20.0, value=20.0, clamped=False, is_echo=False)
        )
        assert mock_bt.cool_target_temperature == 22.5
        assert mock_bt.heat_target_temperature == 22.0

    def test_setpoint_equal_to_the_heat_target_is_lifted(self, mock_bt):
        """Equal targets are a collision too: the cooler would never switch on."""
        mock_bt.hvac_mode = HVACMode.HEAT
        mock_bt.heat_target_temperature = 22.0
        mock_bt.bt_target_temperature_step = 0.5
        self._call(
            mock_bt, InboundSetpoint(raw=22.0, value=22.0, clamped=False, is_echo=False)
        )
        assert mock_bt.cool_target_temperature == 22.5
        assert mock_bt.heat_target_temperature == 22.0


# ===========================================================================
# Per-channel ranges with a cooler
# ===========================================================================


class TestChannelRanges:
    """With a cooler each target is held to its own channel's range.

    The fixture's heads span 5 to 30 °C; the cooler here spans 16 to 35 °C,
    so the published range is 5 to 35 °C.
    """

    @pytest.fixture
    def cooled_bt(self, mock_bt):
        """Give ``mock_bt`` a cooler whose range reaches past the heads'."""
        mock_bt.cooler_entity_id = "climate.cooler"
        mock_bt.cool_min_temperature = 16.0
        mock_bt.cool_max_temperature = 35.0
        mock_bt.min_temp = 5.0
        mock_bt.max_temp = 35.0
        return mock_bt

    def test_a_re_injected_cooling_target_is_bounded_by_the_cooler(self, cooled_bt):
        """A cooling target the cooler holds is not cut to the heads' maximum."""
        assert BetterThermostat._bound_cool_target_to_range(cooled_bt, 33.0) == 33.0
        assert BetterThermostat._bound_cool_target_to_range(cooled_bt, 37.0) == 35.0

    def test_the_cooling_bump_is_capped_by_the_cooler_maximum(self, cooled_bt):
        """A heating target on the heads' maximum leaves the cooler room above it."""
        cooled_bt.hvac_mode = HVACMode.HEAT_COOL
        cooled_bt.heat_target_temperature = 30.0
        cooled_bt.cool_target_temperature = 29.0

        BetterThermostat._enforce_cool_above_heat(cooled_bt)

        assert cooled_bt.cool_target_temperature == 30.5

    def test_the_inbound_cooling_floor_is_capped_by_the_cooler_maximum(self, cooled_bt):
        """A cooler report is raised above the heating target up to the cooler's max."""
        cooled_bt.heat_target_temperature = 30.0

        assert BetterThermostat._clamp_inbound_cool_target(cooled_bt, 29.0) == 30.5

    def test_a_preset_is_held_to_the_heating_range(self, cooled_bt):
        """A heating preset above the heads' maximum applies as that maximum."""
        assert BetterThermostat._applied_target(cooled_bt, 33.0) == 30.0

    @pytest.mark.asyncio
    async def test_a_selected_preset_is_held_to_the_heating_range(self, cooled_bt):
        """Selecting a preset stored above the heads' maximum heats to that maximum."""
        cooled_bt.preset_modes = [PRESET_NONE, PRESET_COMFORT]
        cooled_bt.preset_mgr.mode = PRESET_NONE
        cooled_bt.preset_mgr.update_temperature(PRESET_COMFORT, 33.0)

        await BetterThermostat.async_set_preset_mode(cooled_bt, PRESET_COMFORT)

        assert cooled_bt.heat_target_temperature == 30.0

    @pytest.mark.asyncio
    async def test_a_selected_cooling_preset_is_held_to_the_cooling_range(
        self, cooled_bt
    ):
        """A cooling preset above the heads' maximum is kept where the cooler holds it."""
        cooled_bt.preset_modes = [PRESET_NONE, PRESET_COMFORT]
        cooled_bt.preset_mgr.mode = PRESET_NONE
        cooled_bt.preset_mgr.update_temperature(PRESET_COMFORT, 22.0)
        cooled_bt._preset_cool_temperatures = {PRESET_COMFORT: 33.0}
        cooled_bt._preset_cool_temperature = None

        await BetterThermostat.async_set_preset_mode(cooled_bt, PRESET_COMFORT)

        assert cooled_bt.cool_target_temperature == 33.0

    @pytest.mark.asyncio
    async def test_a_cooling_target_set_directly_is_held_to_the_cooling_range(
        self, cooled_bt
    ):
        """A target_temp_high above the heads' maximum is kept within the cooler's."""
        cooled_bt.hvac_mode = HVACMode.HEAT_COOL
        cooled_bt.bt_hvac_mode = HVACMode.HEAT_COOL
        cooled_bt.preset_mgr.mode = PRESET_NONE
        cooled_bt.heat_target_temperature = 21.0
        cooled_bt.cool_target_temperature = 26.0

        await BetterThermostat.async_set_temperature(
            cooled_bt, **{ATTR_TARGET_TEMP_HIGH: 33.0}
        )

        assert cooled_bt.cool_target_temperature == 33.0
