"""Tests for events/temperature.py – external temperature event handlers.

Covers EMA calculation, temperature application, guard clauses, debounce
acceptance logic, accumulation tracking, plateau acceptance, and the order
in which overlapping readings and the keepalive tick reach the TRVs.
"""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import State
from homeassistant.util import dt as dt_util
import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.decide import KernelState
from custom_components.better_thermostat.events.temperature import (
    _apply_temperature_update,
    _update_external_temp_ema,
    temperature_filter_lock,
    trigger_temperature_change,
)
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.const import CONF_HOMEMATICIP

SENSOR_ID = "sensor.external_temp"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_bt():
    """Create a mock BetterThermostat instance with sensible defaults."""
    bt = MagicMock()
    bt.hass = MagicMock()
    bt.device_name = "Test Thermostat"
    bt.sensor_entity_id = SENSOR_ID

    # Current temperature state
    bt.cur_temp = 20.0
    bt.prev_stable_temp = 20.0
    bt.last_change_direction = 0
    bt.last_known_external_temp = 20.0
    bt.last_external_sensor_change = dt_util.now() - timedelta(seconds=60)

    # EMA state
    bt.external_temp_ema_tau_s = 300.0
    bt._external_temp_ema_ts = None
    bt.external_temp_ema = None
    bt.cur_temp_filtered = None

    # Accumulation state
    bt.accum_delta = 0.0
    bt.accum_dir = 0
    bt.accum_since = dt_util.now()

    # Pending / plateau state
    bt.pending_temp = None
    bt.pending_since = None
    bt.plateau_timer_cancel = None

    # Serialisation of concurrent readings
    bt._temperature_filter_lock = None

    # Anti-flicker state
    bt.flicker_candidate = None

    # Maintenance
    bt.in_maintenance = False
    bt._control_needed_after_maintenance = False

    # Startup
    bt.startup_running = False

    # Control queue
    bt.control_queue_task = MagicMock()

    # HA state writing
    bt.async_write_ha_state = MagicMock()

    # TRV config
    bt.all_trvs = [{"advanced": {CONF_HOMEMATICIP: False}}]
    bt.real_trvs = {}

    return bt


def _make_event(new_state):
    """Build a mock event with the given new_state."""
    event = MagicMock()
    event.data = {"new_state": new_state}
    return event


# ---------------------------------------------------------------------------
# 1. EMA calculation
# ---------------------------------------------------------------------------


class TestUpdateExternalTempEma:
    """Tests for _update_external_temp_ema()."""

    def test_first_call_returns_input(self, mock_bt):
        """Return the input value when no previous EMA exists."""
        mock_bt._external_temp_ema_ts = None
        mock_bt.external_temp_ema = None

        result = _update_external_temp_ema(mock_bt, 21.5)

        assert result == 21.5

    def test_subsequent_call_applies_ema(self, mock_bt):
        """Blend old and new values when a previous EMA exists."""
        from time import monotonic

        mock_bt._external_temp_ema_ts = monotonic() - 60.0
        mock_bt.external_temp_ema = 20.0

        result = _update_external_temp_ema(mock_bt, 22.0)

        assert 20.0 < result < 22.0

    def test_zero_tau_defaults_to_300(self, mock_bt):
        """Fall back to tau=300 when tau_s is zero."""
        mock_bt.external_temp_ema_tau_s = 0.0
        mock_bt._external_temp_ema_ts = None
        mock_bt.external_temp_ema = None

        result = _update_external_temp_ema(mock_bt, 21.0)

        assert result == 21.0

    def test_none_tau_defaults_to_300(self, mock_bt):
        """Fall back to tau=300 when tau_s is None."""
        mock_bt.external_temp_ema_tau_s = None
        mock_bt._external_temp_ema_ts = None
        mock_bt.external_temp_ema = None

        result = _update_external_temp_ema(mock_bt, 21.0)

        assert result == 21.0

    def test_updates_all_state_attributes(self, mock_bt):
        """Set _external_temp_ema_ts, external_temp_ema, and cur_temp_filtered."""
        mock_bt._external_temp_ema_ts = None
        mock_bt.external_temp_ema = None

        _update_external_temp_ema(mock_bt, 21.5)

        assert mock_bt._external_temp_ema_ts is not None
        assert mock_bt.external_temp_ema == 21.5
        assert mock_bt.cur_temp_filtered == 21.5


# ---------------------------------------------------------------------------
# 2. Temperature application
# ---------------------------------------------------------------------------


class TestApplyTemperatureUpdate:
    """Tests for _apply_temperature_update()."""

    @pytest.mark.asyncio
    async def test_updates_cur_temp(self, mock_bt):
        """Set cur_temp to the rounded new value."""
        await _apply_temperature_update(mock_bt, 21.567)

        assert mock_bt.cur_temp == 21.57

    @pytest.mark.asyncio
    async def test_updates_prev_stable_temp_on_change(self, mock_bt):
        """Store old cur_temp in prev_stable_temp when values differ."""
        mock_bt.cur_temp = 20.0

        await _apply_temperature_update(mock_bt, 21.0)

        assert mock_bt.prev_stable_temp == 20.0

    @pytest.mark.asyncio
    async def test_prev_stable_temp_unchanged_when_same(self, mock_bt):
        """Keep prev_stable_temp unchanged when new equals old."""
        mock_bt.cur_temp = 20.0
        mock_bt.prev_stable_temp = 19.0

        await _apply_temperature_update(mock_bt, 20.0)

        assert mock_bt.prev_stable_temp == 19.0

    @pytest.mark.asyncio
    async def test_direction_up(self, mock_bt):
        """Set last_change_direction to 1 on temperature increase."""
        mock_bt.cur_temp = 20.0

        await _apply_temperature_update(mock_bt, 21.0)

        assert mock_bt.last_change_direction == 1

    @pytest.mark.asyncio
    async def test_direction_down(self, mock_bt):
        """Set last_change_direction to -1 on temperature decrease."""
        mock_bt.cur_temp = 20.0

        await _apply_temperature_update(mock_bt, 19.0)

        assert mock_bt.last_change_direction == -1

    @pytest.mark.asyncio
    async def test_resets_accumulation(self, mock_bt):
        """Reset accum_delta and accum_dir to 0 after accepting."""
        mock_bt.accum_delta = 0.5
        mock_bt.accum_dir = 1

        await _apply_temperature_update(mock_bt, 21.0)

        assert mock_bt.accum_delta == 0.0
        assert mock_bt.accum_dir == 0

    @pytest.mark.asyncio
    async def test_resets_pending(self, mock_bt):
        """Reset pending_temp and pending_since to None after accepting."""
        mock_bt.pending_temp = 21.0
        mock_bt.pending_since = dt_util.now()

        await _apply_temperature_update(mock_bt, 21.0)

        assert mock_bt.pending_temp is None
        assert mock_bt.pending_since is None

    @pytest.mark.asyncio
    async def test_cancels_plateau_timer(self, mock_bt):
        """Cancel an active plateau timer and set it to None."""
        cancel_fn = MagicMock()
        mock_bt.plateau_timer_cancel = cancel_fn

        await _apply_temperature_update(mock_bt, 21.0)

        cancel_fn.assert_called_once()
        assert mock_bt.plateau_timer_cancel is None

    @pytest.mark.asyncio
    async def test_writes_ha_state(self, mock_bt):
        """Call async_write_ha_state() to publish the new temperature."""
        await _apply_temperature_update(mock_bt, 21.0)

        mock_bt.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_enqueues_control_action(self, mock_bt):
        """Enqueue a control action via request_control_cycle()."""
        await _apply_temperature_update(mock_bt, 21.0)

        mock_bt.control_queue_task.put_nowait.assert_called_once_with(mock_bt)

    @pytest.mark.asyncio
    async def test_skips_control_during_maintenance(self, mock_bt):
        """Skip put() during maintenance but set the deferred flag."""
        mock_bt.in_maintenance = True

        await _apply_temperature_update(mock_bt, 21.0)

        mock_bt.control_queue_task.put_nowait.assert_not_called()
        assert mock_bt._control_needed_after_maintenance is True

    @pytest.mark.asyncio
    async def test_quirks_external_temp_called(self, mock_bt):
        """Call model_quirks.maybe_set_external_temperature() for each TRV."""
        quirks = AsyncMock()
        mock_bt.real_trvs = {
            "climate.trv1": Trv.from_legacy_dict(
                "climate.trv1", {"model_quirks": quirks}
            )
        }

        await _apply_temperature_update(mock_bt, 21.0)

        quirks.maybe_set_external_temperature.assert_awaited_once_with(
            mock_bt, "climate.trv1", 21.0
        )


# ---------------------------------------------------------------------------
# 3. Guard clauses for trigger_temperature_change
# ---------------------------------------------------------------------------


class TestTriggerTemperatureChangeGuards:
    """Guard-clause tests for trigger_temperature_change()."""

    @pytest.mark.asyncio
    async def test_returns_early_during_startup(self, mock_bt):
        """Return early when startup_running is True."""
        mock_bt.startup_running = True
        event = _make_event(State(SENSOR_ID, "21.0"))

        await trigger_temperature_change(mock_bt, event)

        mock_bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_early_new_state_none(self, mock_bt):
        """Return early when new_state is None."""
        event = _make_event(None)

        await trigger_temperature_change(mock_bt, event)

        mock_bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_early_state_unavailable(self, mock_bt):
        """Return early when state is 'unavailable'."""
        event = _make_event(State(SENSOR_ID, "unavailable"))

        await trigger_temperature_change(mock_bt, event)

        mock_bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_early_state_unknown(self, mock_bt):
        """Return early when state is 'unknown'."""
        event = _make_event(State(SENSOR_ID, "unknown"))

        await trigger_temperature_change(mock_bt, event)

        mock_bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_early_non_numeric(self, mock_bt):
        """Return early and create a repair issue for non-numeric state."""
        event = _make_event(State(SENSOR_ID, "abc"))

        with patch(
            "custom_components.better_thermostat.events.temperature.ir"
        ) as mock_ir:
            mock_ir.IssueSeverity.ERROR = "error"
            await trigger_temperature_change(mock_bt, event)

        mock_ir.async_create_issue.assert_called_once()
        mock_bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_early_temp_below_minus_50(self, mock_bt):
        """Return early and create a repair issue for temperature below -50."""
        event = _make_event(State(SENSOR_ID, "-60.0"))

        with patch(
            "custom_components.better_thermostat.events.temperature.ir"
        ) as mock_ir:
            mock_ir.IssueSeverity.ERROR = "error"
            await trigger_temperature_change(mock_bt, event)

        mock_ir.async_create_issue.assert_called_once()
        mock_bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_first_run_accepts_via_first_reading_path(self, mock_bt):
        """Accept the first reading via 'first_reading' when cur_temp is None."""
        mock_bt.last_external_sensor_change = None
        mock_bt.cur_temp = None
        event = _make_event(State(SENSOR_ID, "21.0"))

        await trigger_temperature_change(mock_bt, event)

        mock_bt.control_queue_task.put_nowait.assert_called_once()
        assert mock_bt.cur_temp == 21.0


# ---------------------------------------------------------------------------
# 4. Temperature acceptance (debounce)
# ---------------------------------------------------------------------------


class TestTemperatureAcceptance:
    """Tests for debounce and acceptance logic.

    With _sig_threshold=0.11 and the accept condition requiring _interval_ok
    on both the "significant" and "accumulated" paths, debounce is properly
    enforced for all changes.
    """

    @pytest.mark.asyncio
    async def test_first_temp_accepted_when_cur_is_none(self, mock_bt):
        """Accept the first temperature reading when cur_temp is None."""
        mock_bt.cur_temp = None
        event = _make_event(State(SENSOR_ID, "21.0"))

        await trigger_temperature_change(mock_bt, event)

        mock_bt.control_queue_task.put_nowait.assert_called_once()
        assert mock_bt.cur_temp == 21.0

    @pytest.mark.asyncio
    async def test_first_update_accepted_when_timestamp_uninitialized(self, mock_bt):
        """First real update passes even with no prior timestamp.

        With a known cur_temp but ``last_external_sensor_change is None`` the
        guard must seed a timestamp older than the debounce window (not "now"),
        so the first significant update clears the interval check.
        """
        mock_bt.cur_temp = 20.0
        mock_bt.last_external_sensor_change = None
        event = _make_event(State(SENSOR_ID, "21.0"))

        await trigger_temperature_change(mock_bt, event)

        mock_bt.control_queue_task.put_nowait.assert_called_once()
        assert mock_bt.cur_temp == 21.0

    @pytest.mark.asyncio
    async def test_significant_change_accepted_after_interval(self, mock_bt):
        """Accept a significant change (>= 0.11) when the interval has elapsed."""
        mock_bt.cur_temp = 20.0
        mock_bt.last_external_sensor_change = dt_util.now() - timedelta(seconds=60)
        event = _make_event(State(SENSOR_ID, "21.0"))

        await trigger_temperature_change(mock_bt, event)

        assert mock_bt.cur_temp == 21.0
        mock_bt.control_queue_task.put_nowait.assert_called_once()

    @pytest.mark.asyncio
    async def test_significant_change_within_debounce_rejected(self, mock_bt):
        """Reject a significant change within the 5s debounce window.

        The accept condition requires _interval_ok on both the "significant"
        and "accumulated" paths, so within-debounce changes are rejected.
        """
        mock_bt.cur_temp = 20.0
        mock_bt.last_external_sensor_change = dt_util.now() - timedelta(seconds=1)
        event = _make_event(State(SENSOR_ID, "20.5"))

        await trigger_temperature_change(mock_bt, event)

        assert mock_bt.cur_temp == 20.0
        mock_bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_homematicip_within_600s_rejected(self, mock_bt):
        """Reject a HomematicIP change within the 600s debounce window."""
        mock_bt.all_trvs = [{"advanced": {CONF_HOMEMATICIP: True}}]
        mock_bt.cur_temp = 20.0
        mock_bt.last_external_sensor_change = dt_util.now() - timedelta(seconds=30)
        event = _make_event(State(SENSOR_ID, "20.5"))

        await trigger_temperature_change(mock_bt, event)

        assert mock_bt.cur_temp == 20.0
        mock_bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_sub_threshold_change_not_accepted_immediately(self, mock_bt):
        """Reject a change below the 0.11 significance threshold."""
        mock_bt.cur_temp = 20.0
        mock_bt.last_external_sensor_change = dt_util.now() - timedelta(seconds=60)
        event = _make_event(State(SENSOR_ID, "20.05"))

        await trigger_temperature_change(mock_bt, event)

        mock_bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_identical_temp_not_accepted(self, mock_bt):
        """Reject an identical temperature (diff=0.0 < threshold 0.11)."""
        mock_bt.cur_temp = 20.0
        mock_bt.last_external_sensor_change = dt_util.now() - timedelta(seconds=60)
        event = _make_event(State(SENSOR_ID, "20.0"))

        await trigger_temperature_change(mock_bt, event)

        mock_bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_accepted_temp_written_to_cur_temp(self, mock_bt):
        """Write the accepted temperature to cur_temp."""
        mock_bt.cur_temp = 20.0
        event = _make_event(State(SENSOR_ID, "21.5"))

        await trigger_temperature_change(mock_bt, event)

        assert mock_bt.cur_temp == 21.5

    @pytest.mark.asyncio
    async def test_homematicip_sets_600s_time_diff(self, mock_bt):
        """Use a 600s debounce interval for HomematicIP TRVs."""
        mock_bt.all_trvs = [{"advanced": {CONF_HOMEMATICIP: True}}]
        mock_bt.cur_temp = 20.0
        mock_bt.last_external_sensor_change = dt_util.now() - timedelta(seconds=700)
        event = _make_event(State(SENSOR_ID, "21.0"))

        await trigger_temperature_change(mock_bt, event)

        assert mock_bt.cur_temp == 21.0


# ---------------------------------------------------------------------------
# 5. Accumulation tracking
# ---------------------------------------------------------------------------


class TestAccumulationTracking:
    """Tests for accumulation state updates inside trigger_temperature_change."""

    @pytest.mark.asyncio
    async def test_sub_threshold_change_accumulates(self, mock_bt):
        """Track sub-threshold changes in accum_delta without accepting."""
        mock_bt.cur_temp = 20.0
        mock_bt.accum_delta = 0.0
        mock_bt.accum_dir = 0

        event = _make_event(State(SENSOR_ID, "20.05"))
        await trigger_temperature_change(mock_bt, event)

        assert mock_bt.accum_delta == 0.05
        assert mock_bt.accum_dir == 1
        mock_bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_accumulated_change_accepted_above_threshold(self, mock_bt):
        """Accept via accumulation when total delta reaches the threshold."""
        mock_bt.cur_temp = 20.0
        mock_bt.accum_delta = 0.08
        mock_bt.accum_dir = 1

        event = _make_event(State(SENSOR_ID, "20.05"))
        await trigger_temperature_change(mock_bt, event)

        # accum_delta = 0.08 + 0.05 = 0.13 >= 0.11 threshold
        mock_bt.control_queue_task.put_nowait.assert_called_once()
        assert mock_bt.accum_delta == 0.0  # reset after accept

    @pytest.mark.asyncio
    async def test_accumulated_change_rejected_within_debounce(self, mock_bt):
        """Reject accumulated changes within the debounce window.

        Even though accum_delta exceeds threshold, _accum_ok also
        requires _interval_ok, so debounce is enforced.
        """
        mock_bt.cur_temp = 20.0
        mock_bt.accum_delta = 0.08
        mock_bt.accum_dir = 1
        mock_bt.last_external_sensor_change = dt_util.now() - timedelta(seconds=1)

        event = _make_event(State(SENSOR_ID, "20.05"))
        await trigger_temperature_change(mock_bt, event)

        # interval_ok=False → neither "significant" nor "accumulated" → rejected
        mock_bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_accum_resets_on_direction_flip(self, mock_bt):
        """Reset accumulation to the new delta on direction change."""
        mock_bt.cur_temp = 20.0
        mock_bt.accum_delta = 0.1
        mock_bt.accum_dir = 1

        event = _make_event(State(SENSOR_ID, "19.95"))
        await trigger_temperature_change(mock_bt, event)

        # Direction flipped: accum_delta reset to -0.05
        assert mock_bt.accum_delta == -0.05
        assert mock_bt.accum_dir == -1

    @pytest.mark.asyncio
    async def test_pending_temp_set_for_sub_threshold_change(self, mock_bt):
        """Set pending_temp for sub-threshold changes (plateau tracking)."""
        mock_bt.cur_temp = 20.0
        event = _make_event(State(SENSOR_ID, "20.05"))

        await trigger_temperature_change(mock_bt, event)

        assert mock_bt.pending_temp == 20.05

    @pytest.mark.asyncio
    async def test_pending_cleared_when_value_returns_to_current(self, mock_bt):
        """Clear pending_temp when the new value equals cur_temp."""
        mock_bt.cur_temp = 20.0
        mock_bt.pending_temp = 20.05
        mock_bt.pending_since = dt_util.now()

        event = _make_event(State(SENSOR_ID, "20.0"))
        await trigger_temperature_change(mock_bt, event)

        assert mock_bt.pending_temp is None


# ---------------------------------------------------------------------------
# 6. Plateau logic
# ---------------------------------------------------------------------------


class TestPlateauLogic:
    """Tests for plateau acceptance paths."""

    @pytest.mark.asyncio
    async def test_plateau_accepts_stable_sub_threshold_change(self, mock_bt):
        """Accept a sub-threshold change that has been stable for 120s."""
        mock_bt.cur_temp = 20.0
        mock_bt.pending_temp = 20.05
        mock_bt.pending_since = dt_util.now() - timedelta(seconds=300)

        event = _make_event(State(SENSOR_ID, "20.05"))

        with patch(
            "custom_components.better_thermostat.events.temperature.async_call_later"
        ) as mock_timer:
            await trigger_temperature_change(mock_bt, event)

        # Plateau age 300s >= 120s window → accepted directly, no timer needed
        mock_timer.assert_not_called()
        mock_bt.control_queue_task.put_nowait.assert_called_once()

    @pytest.mark.asyncio
    async def test_plateau_timer_scheduled_for_new_pending(self, mock_bt):
        """Schedule a plateau timer for a new sub-threshold pending value."""
        mock_bt.cur_temp = 20.0

        event = _make_event(State(SENSOR_ID, "20.01"))

        with patch(
            "custom_components.better_thermostat.events.temperature.async_call_later"
        ) as mock_timer:
            await trigger_temperature_change(mock_bt, event)

        # Sub-threshold, just set pending → timer scheduled for PLATEAU_ACCEPT_WINDOW
        mock_timer.assert_called_once()
        mock_bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_sub_threshold_accumulated_to_significant(self, mock_bt):
        """Accept via accumulation when small deltas sum above the threshold."""
        mock_bt.cur_temp = 20.0
        mock_bt.accum_delta = 0.10
        mock_bt.accum_dir = 1

        event = _make_event(State(SENSOR_ID, "20.05"))

        await trigger_temperature_change(mock_bt, event)

        # accum_delta = 0.10 + 0.05 = 0.15 >= 0.11 → accepted as "accumulated"
        mock_bt.control_queue_task.put_nowait.assert_called_once()


# ---------------------------------------------------------------------------
# 7. Edge cases and robustness
# ---------------------------------------------------------------------------


class TestEdgeCasesAndRobustness:
    """Edge cases that probe error handling and invariant boundaries."""

    @pytest.mark.asyncio
    async def test_all_trvs_none_does_not_crash(self, mock_bt):
        """all_trvs=None should not crash the HomematicIP detection loop.

        The loop `for trv in self.all_trvs` raises TypeError when
        all_trvs is None, which is NOT caught by `except KeyError`.
        """
        mock_bt.all_trvs = None
        mock_bt.cur_temp = 20.0
        mock_bt.last_external_sensor_change = dt_util.now() - timedelta(seconds=60)
        event = _make_event(State(SENSOR_ID, "21.0"))

        await trigger_temperature_change(mock_bt, event)
        assert mock_bt.cur_temp == 21.0

    @pytest.mark.asyncio
    async def test_all_trvs_advanced_none_does_not_crash(self, mock_bt):
        """TRV with advanced=None should not crash HomematicIP detection.

        `None[CONF_HOMEMATICIP]` raises TypeError, not KeyError.
        """
        mock_bt.all_trvs = [{"advanced": None}]
        mock_bt.cur_temp = 20.0
        mock_bt.last_external_sensor_change = dt_util.now() - timedelta(seconds=60)
        event = _make_event(State(SENSOR_ID, "21.0"))

        await trigger_temperature_change(mock_bt, event)
        assert mock_bt.cur_temp == 21.0

    @pytest.mark.asyncio
    async def test_minus_50_exactly_accepted(self, mock_bt):
        """Temperature exactly -50.0 is on the inclusive lower bound."""
        mock_bt.cur_temp = None
        event = _make_event(State(SENSOR_ID, "-50.0"))

        await trigger_temperature_change(mock_bt, event)
        assert mock_bt.cur_temp == -50.0

    @pytest.mark.asyncio
    async def test_below_minus_50_rejected(self, mock_bt):
        """Temperature below the lower plausibility bound is rejected."""
        mock_bt.cur_temp = 20.0
        event = _make_event(State(SENSOR_ID, "-100.0"))

        with patch(
            "custom_components.better_thermostat.events.temperature.ir.async_create_issue"
        ) as mock_create_issue:
            await trigger_temperature_change(mock_bt, event)

        assert mock_bt.cur_temp == 20.0
        mock_create_issue.assert_called_once()

    @pytest.mark.asyncio
    async def test_avm_off_marker_rejected(self, mock_bt):
        """AVM Fritz!DECT 126.5 °C (OFF marker) must not update cur_temp."""
        mock_bt.cur_temp = 20.0
        event = _make_event(State(SENSOR_ID, "126.5"))

        with patch(
            "custom_components.better_thermostat.events.temperature.ir.async_create_issue"
        ) as mock_create_issue:
            await trigger_temperature_change(mock_bt, event)

        assert mock_bt.cur_temp == 20.0
        mock_create_issue.assert_called_once()

    @pytest.mark.asyncio
    async def test_avm_on_marker_rejected(self, mock_bt):
        """AVM Fritz!DECT 127.0 °C (ON marker) must not update cur_temp."""
        mock_bt.cur_temp = 20.0
        event = _make_event(State(SENSOR_ID, "127.0"))

        with patch(
            "custom_components.better_thermostat.events.temperature.ir.async_create_issue"
        ) as mock_create_issue:
            await trigger_temperature_change(mock_bt, event)

        assert mock_bt.cur_temp == 20.0
        mock_create_issue.assert_called_once()

    @pytest.mark.asyncio
    async def test_60_exactly_accepted(self, mock_bt):
        """Temperature exactly 60.0 is on the inclusive upper bound."""
        mock_bt.cur_temp = None
        event = _make_event(State(SENSOR_ID, "60.0"))

        await trigger_temperature_change(mock_bt, event)
        assert mock_bt.cur_temp == 60.0

    @pytest.mark.asyncio
    async def test_control_queue_none_no_crash_in_apply(self, mock_bt):
        """No crash when control_queue_task is None during _apply_temperature_update."""
        mock_bt.control_queue_task = None
        mock_bt.cur_temp = None
        event = _make_event(State(SENSOR_ID, "21.0"))

        await trigger_temperature_change(mock_bt, event)
        assert mock_bt.cur_temp == 21.0

    @pytest.mark.asyncio
    async def test_ema_failure_does_not_block_update(self, mock_bt):
        """EMA calculation failure should not prevent temperature update."""
        mock_bt.cur_temp = None
        # Force EMA to fail by making tau_s non-numeric
        mock_bt.external_temp_ema_tau_s = "invalid"
        event = _make_event(State(SENSOR_ID, "21.0"))

        await trigger_temperature_change(mock_bt, event)
        # Temperature should still be updated despite EMA failure
        assert mock_bt.cur_temp == 21.0

    @pytest.mark.asyncio
    async def test_plateau_timer_cancelled_on_pending_value_change(self, mock_bt):
        """Changing pending value should cancel the old plateau timer."""
        mock_bt.cur_temp = 20.0
        mock_bt.pending_temp = 20.03
        mock_bt.pending_since = dt_util.now() - timedelta(seconds=10)
        cancel_fn = MagicMock()
        mock_bt.plateau_timer_cancel = cancel_fn
        mock_bt.last_external_sensor_change = dt_util.now() - timedelta(seconds=2)

        # New sub-threshold value different from pending
        event = _make_event(State(SENSOR_ID, "20.07"))
        with patch(
            "custom_components.better_thermostat.events.temperature.async_call_later",
            return_value=MagicMock(),
        ):
            await trigger_temperature_change(mock_bt, event)

        # Old timer should be cancelled, new pending set
        cancel_fn.assert_called_once()
        assert mock_bt.pending_temp == 20.07

    @pytest.mark.asyncio
    async def test_last_external_sensor_change_typeerror_handled(self, mock_bt):
        """TypeError in age calculation should fall back to large age."""
        mock_bt.cur_temp = 20.0
        mock_bt.last_external_sensor_change = "not_a_datetime"
        event = _make_event(State(SENSOR_ID, "21.0"))

        await trigger_temperature_change(mock_bt, event)
        # With fallback _age=999999, _interval_ok=True → accepted
        assert mock_bt.cur_temp == 21.0

    @pytest.mark.asyncio
    async def test_multiple_homematicip_trvs_any_sets_600s(self, mock_bt):
        """If ANY TRV is HomematicIP, 600s debounce applies."""
        mock_bt.all_trvs = [
            {"advanced": {CONF_HOMEMATICIP: False}},
            {"advanced": {CONF_HOMEMATICIP: True}},
        ]
        mock_bt.cur_temp = 20.0
        mock_bt.last_external_sensor_change = dt_util.now() - timedelta(seconds=30)
        event = _make_event(State(SENSOR_ID, "21.0"))

        await trigger_temperature_change(mock_bt, event)

        # 30s < 600s → rejected because one TRV is HomematicIP
        assert mock_bt.cur_temp == 20.0


# ---------------------------------------------------------------------------
# 8. Concurrent readings
# ---------------------------------------------------------------------------


class _RecordingQuirks:
    """Model quirks that record external-temperature writes and overlap."""

    def __init__(self):
        self.writes: list[tuple[str, float]] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.gate: asyncio.Event | None = None
        # How many writes wait for ``gate``; None holds up every one of
        # them. A budget of one leaves a caller standing between two TRVs
        # while another one runs its own round to the end.
        self.gated_writes: int | None = None
        self.started = 0

    async def maybe_set_external_temperature(self, entity, entity_id, temperature):
        """Record one write, yielding long enough for another task to run."""
        self.started += 1
        gated = self.gate is not None and (
            self.gated_writes is None or self.started <= self.gated_writes
        )
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if gated:
                await self.gate.wait()
            else:
                for _ in range(3):
                    await asyncio.sleep(0)
            self.writes.append((entity_id, temperature))
        finally:
            self.in_flight -= 1


class _AdvancingClock:
    """Hand out timestamps that move forward on every read.

    Each reading is debounced against the previous one, so a clock that
    stood still would reject the second of two readings long before the
    handlers could overlap.
    """

    def __init__(self, start, step_seconds=10):
        self._current = start
        self._step = timedelta(seconds=step_seconds)

    def now(self):
        """Return a timestamp ``step_seconds`` after the previous one."""
        self._current += self._step
        return self._current


class TestConcurrentReadings:
    """Two readings handled at the same time must not interleave."""

    @staticmethod
    def _attach_trvs(mock_bt, quirks, entity_ids):
        """Give the thermostat TRVs that all share one quirks recorder."""
        mock_bt.real_trvs = {
            entity_id: Trv.from_legacy_dict(entity_id, {"model_quirks": quirks})
            for entity_id in entity_ids
        }

    @staticmethod
    async def _take_turn_and_read(mock_bt, state):
        """Hand one reading to the filter the way the thermostat does."""
        async with temperature_filter_lock(mock_bt):
            await trigger_temperature_change(mock_bt, _make_event(state))

    @pytest.mark.asyncio
    async def test_overlapping_readings_reach_every_trv_in_order(self, mock_bt):
        """Hand both accepted readings to every TRV, oldest first."""
        quirks = _RecordingQuirks()
        self._attach_trvs(mock_bt, quirks, ("climate.trv1", "climate.trv2"))
        start = dt_util.now()
        mock_bt.last_external_sensor_change = start

        with patch(
            "custom_components.better_thermostat.events.temperature.dt_util",
            _AdvancingClock(start),
        ):
            await asyncio.gather(
                self._take_turn_and_read(mock_bt, State(SENSOR_ID, "21.0")),
                self._take_turn_and_read(mock_bt, State(SENSOR_ID, "22.0")),
            )

        assert quirks.max_in_flight == 1
        assert quirks.writes == [
            ("climate.trv1", 21.0),
            ("climate.trv2", 21.0),
            ("climate.trv1", 22.0),
            ("climate.trv2", 22.0),
        ]
        assert mock_bt.last_known_external_temp == 22.0
        assert mock_bt.accum_delta == 0.0
        assert mock_bt.pending_temp is None

    @pytest.mark.asyncio
    async def test_overlapping_applies_do_not_share_the_write_loop(self, mock_bt):
        """Serialise updates that skip the debounce, such as the plateau timer."""
        quirks = _RecordingQuirks()
        self._attach_trvs(mock_bt, quirks, ("climate.trv1",))

        await asyncio.gather(
            _apply_temperature_update(mock_bt, 21.0),
            _apply_temperature_update(mock_bt, 22.0),
        )

        assert quirks.max_in_flight == 1
        assert quirks.writes == [("climate.trv1", 21.0), ("climate.trv1", 22.0)]
        assert mock_bt.last_known_external_temp == 22.0

    @pytest.mark.asyncio
    async def test_cancelled_update_lets_the_next_one_through(self, mock_bt):
        """Release the serialisation when a pending update is cancelled."""
        quirks = _RecordingQuirks()
        quirks.gate = asyncio.Event()
        self._attach_trvs(mock_bt, quirks, ("climate.trv1",))

        cancelled = asyncio.create_task(_apply_temperature_update(mock_bt, 21.0))
        await asyncio.sleep(0)
        queued = asyncio.create_task(_apply_temperature_update(mock_bt, 22.0))
        await asyncio.sleep(0)

        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        quirks.gate.set()
        await queued

        assert quirks.writes == [("climate.trv1", 22.0)]
        assert mock_bt.last_known_external_temp == 22.0


async def _suspending_translations(*args, **kwargs):
    """Stand in for a translation lookup that has to read its files.

    Home Assistant serves a cached language from memory without ever
    giving up control, and reads it through the executor when the cache
    does not hold it yet. Announcing a change of degraded mode is the only
    part of the entity checks that looks a translation up, so the checks
    take longer for the reading that announces one than for the next
    reading, which is the difference the ordering has to survive.
    """
    await asyncio.sleep(0)
    return {}


class TestArrivalOrder:
    """Readings are applied in the order the room sensor sent them."""

    @staticmethod
    def _make_thermostat_checkable(mock_bt):
        """Give the thermostat what the entity checks read.

        The checks look at the sensors the thermostat was configured with
        and at the control-mode record; a bare mock answers every one of
        those with a new mock and the checks cannot run against it.
        """
        mock_bt.hass.states.get = lambda entity_id: State(entity_id, "21.0")
        mock_bt.window_id = None
        mock_bt.door_id = None
        mock_bt.cooler_entity_id = None
        mock_bt.humidity_sensor_entity_id = None
        mock_bt.outdoor_sensor = None
        mock_bt.weather_entity = None
        mock_bt.devices_errors = []
        mock_bt.devices_states = {}
        mock_bt.unavailable_sensors = []
        mock_bt._critical_grace_until = None
        mock_bt.kernel_state = KernelState()
        mock_bt.clock = FakeClock()
        # The first of the two readings announces that degraded mode has
        # ended, which is the pass whose checks have to wait.
        mock_bt._degraded_warning_emitted = True

    @staticmethod
    def _collect_handlers(mock_bt, handlers):
        """Build the handler coroutines the listener hands over.

        The thermostat is a mock, so the coroutine it would hand to Home
        Assistant has to be built from the real method.
        """
        mock_bt._handle_temperature_reading = lambda event: (
            BetterThermostat._handle_temperature_reading(mock_bt, event)
        )
        mock_bt.hass.async_create_background_task = lambda coro, name=None: (
            handlers.append(asyncio.ensure_future(coro))
        )

    @pytest.mark.asyncio
    async def test_the_older_of_two_readings_is_applied_first(self, mock_bt):
        """Two readings dispatched together keep the order they arrived in.

        Home Assistant handles each state change in its own task. The
        checks that run for a reading wait on the pass that announces a
        change of degraded mode and on no other, so the second reading can
        overtake the first one and leave the room regulated on the older
        value.
        """
        self._make_thermostat_checkable(mock_bt)
        quirks = _RecordingQuirks()
        mock_bt.real_trvs = {
            "climate.trv1": Trv.from_legacy_dict(
                "climate.trv1", {"model_quirks": quirks}
            )
        }
        start = dt_util.now()
        mock_bt.last_external_sensor_change = start
        handlers = []
        self._collect_handlers(mock_bt, handlers)

        with (
            patch(
                "custom_components.better_thermostat.utils.helpers.translation."
                "async_get_translations",
                _suspending_translations,
            ),
            patch(
                "custom_components.better_thermostat.events.temperature.dt_util",
                _AdvancingClock(start),
            ),
        ):
            await asyncio.gather(
                BetterThermostat._trigger_temperature_change(
                    mock_bt, _make_event(State(SENSOR_ID, "21.0"))
                ),
                BetterThermostat._trigger_temperature_change(
                    mock_bt, _make_event(State(SENSOR_ID, "22.0"))
                ),
            )
            await asyncio.gather(*handlers)

        assert quirks.writes == [("climate.trv1", 21.0), ("climate.trv1", 22.0)]
        assert mock_bt.last_known_external_temp == 22.0


class TestKeepaliveTick:
    """The periodic re-send shares its turn with the readings."""

    @pytest.mark.asyncio
    async def test_the_tick_does_not_write_over_a_reading_it_overlapped(self, mock_bt):
        """A tick left standing between two TRVs must not undo an update.

        The tick reads the room temperature once and writes it to every
        TRV in turn. An update landing while it is between two of them
        would leave the TRVs it has yet to reach on the value it started
        with, while Better Thermostat regulates on the newer one.
        """
        quirks = _RecordingQuirks()
        quirks.gate = asyncio.Event()
        quirks.gated_writes = 1
        mock_bt.real_trvs = {
            entity_id: Trv.from_legacy_dict(entity_id, {"model_quirks": quirks})
            for entity_id in ("climate.trv1", "climate.trv2")
        }

        tick = asyncio.create_task(
            BetterThermostat._external_temperature_keepalive(mock_bt)
        )
        await asyncio.sleep(0)
        reading = asyncio.create_task(_apply_temperature_update(mock_bt, 22.0))
        for _ in range(4):
            await asyncio.sleep(0)
        quirks.gate.set()
        await asyncio.gather(tick, reading)

        assert quirks.max_in_flight == 1
        assert quirks.writes == [
            ("climate.trv1", 20.0),
            ("climate.trv2", 20.0),
            ("climate.trv1", 22.0),
            ("climate.trv2", 22.0),
        ]
        assert mock_bt.last_known_external_temp == 22.0
