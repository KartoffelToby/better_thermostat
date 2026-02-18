"""Tests for events/temperature.py – external temperature event handlers.

Covers EMA calculation, temperature application, guard clauses, debounce
acceptance logic, accumulation tracking, and plateau acceptance.
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import State
import pytest

from custom_components.better_thermostat.events.temperature import (
    _apply_temperature_update,
    _update_external_temp_ema,
    trigger_temperature_change,
)
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
    bt.last_external_sensor_change = datetime.now() - timedelta(seconds=60)

    # EMA state
    bt.external_temp_ema_tau_s = 300.0
    bt._external_temp_ema_ts = None
    bt.external_temp_ema = None
    bt.cur_temp_filtered = None

    # Accumulation state
    bt.accum_delta = 0.0
    bt.accum_dir = 0
    bt.accum_since = datetime.now()

    # Pending / plateau state
    bt.pending_temp = None
    bt.pending_since = None
    bt.plateau_timer_cancel = None

    # Anti-flicker state
    bt.flicker_candidate = None
    bt.flicker_unignore_cancel = None

    # Maintenance
    bt.in_maintenance = False
    bt._control_needed_after_maintenance = False

    # Startup
    bt.startup_running = False

    # Control queue
    bt.control_queue_task = AsyncMock()

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
        mock_bt.pending_since = datetime.now()

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
        """Enqueue a control action via control_queue_task.put()."""
        await _apply_temperature_update(mock_bt, 21.0)

        mock_bt.control_queue_task.put.assert_awaited_once_with(mock_bt)

    @pytest.mark.asyncio
    async def test_skips_control_during_maintenance(self, mock_bt):
        """Skip put() during maintenance but set the deferred flag."""
        mock_bt.in_maintenance = True

        await _apply_temperature_update(mock_bt, 21.0)

        mock_bt.control_queue_task.put.assert_not_awaited()
        assert mock_bt._control_needed_after_maintenance is True

    @pytest.mark.asyncio
    async def test_quirks_external_temp_called(self, mock_bt):
        """Call model_quirks.maybe_set_external_temperature() for each TRV."""
        quirks = AsyncMock()
        mock_bt.real_trvs = {"climate.trv1": {"model_quirks": quirks}}

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

        mock_bt.control_queue_task.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returns_early_new_state_none(self, mock_bt):
        """Return early when new_state is None."""
        event = _make_event(None)

        await trigger_temperature_change(mock_bt, event)

        mock_bt.control_queue_task.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returns_early_state_unavailable(self, mock_bt):
        """Return early when state is 'unavailable'."""
        event = _make_event(State(SENSOR_ID, "unavailable"))

        await trigger_temperature_change(mock_bt, event)

        mock_bt.control_queue_task.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returns_early_state_unknown(self, mock_bt):
        """Return early when state is 'unknown'."""
        event = _make_event(State(SENSOR_ID, "unknown"))

        await trigger_temperature_change(mock_bt, event)

        mock_bt.control_queue_task.put.assert_not_awaited()

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
        mock_bt.control_queue_task.put.assert_not_awaited()

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
        mock_bt.control_queue_task.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_first_run_accepts_via_first_reading_path(self, mock_bt):
        """Accept the first reading via 'first_reading' when cur_temp is None."""
        mock_bt.last_external_sensor_change = None
        mock_bt.cur_temp = None
        event = _make_event(State(SENSOR_ID, "21.0"))

        await trigger_temperature_change(mock_bt, event)

        mock_bt.control_queue_task.put.assert_awaited_once()
        assert mock_bt.cur_temp == 21.0


# ---------------------------------------------------------------------------
# 4. Temperature acceptance (debounce)
# ---------------------------------------------------------------------------


class TestTemperatureAcceptance:
    """Tests for debounce and acceptance logic.

    Note: With _sig_threshold=0.0 on this branch, all temperature changes
    (including diff=0.0) are considered "significant". The accept condition
    fix enforces _interval_ok on the "significant" path, but the
    "accumulated" path (also threshold-dependent) catches everything that
    misses the interval window. Full debounce enforcement requires the
    threshold fix from PR #1930 in addition to this accept condition fix.
    """

    @pytest.mark.asyncio
    async def test_first_temp_accepted_when_cur_is_none(self, mock_bt):
        """Accept the first temperature reading when cur_temp is None."""
        mock_bt.cur_temp = None
        event = _make_event(State(SENSOR_ID, "21.0"))

        await trigger_temperature_change(mock_bt, event)

        mock_bt.control_queue_task.put.assert_awaited_once()
        assert mock_bt.cur_temp == 21.0

    @pytest.mark.asyncio
    async def test_change_accepted_after_interval(self, mock_bt):
        """Accept a temperature change when the debounce interval has elapsed."""
        mock_bt.cur_temp = 20.0
        mock_bt.last_external_sensor_change = datetime.now() - timedelta(seconds=60)
        event = _make_event(State(SENSOR_ID, "21.0"))

        await trigger_temperature_change(mock_bt, event)

        assert mock_bt.cur_temp == 21.0
        mock_bt.control_queue_task.put.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_change_within_debounce_rejected(self, mock_bt):
        """Reject a change within the 5s debounce window.

        The fix removes the or-branch that previously bypassed _interval_ok.
        Both "significant" and "accumulated" paths require _interval_ok,
        so within-debounce changes are now properly rejected.
        """
        mock_bt.cur_temp = 20.0
        mock_bt.last_external_sensor_change = datetime.now() - timedelta(seconds=1)
        event = _make_event(State(SENSOR_ID, "20.5"))

        await trigger_temperature_change(mock_bt, event)

        assert mock_bt.cur_temp == 20.0
        mock_bt.control_queue_task.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_homematicip_within_600s_rejected(self, mock_bt):
        """Reject a HomematicIP change within the 600s debounce window."""
        mock_bt.all_trvs = [{"advanced": {CONF_HOMEMATICIP: True}}]
        mock_bt.cur_temp = 20.0
        mock_bt.last_external_sensor_change = datetime.now() - timedelta(seconds=30)
        event = _make_event(State(SENSOR_ID, "20.5"))

        await trigger_temperature_change(mock_bt, event)

        assert mock_bt.cur_temp == 20.0
        mock_bt.control_queue_task.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_identical_temp_accepted_with_zero_threshold(self, mock_bt):
        """Accept identical temperature since diff=0.0 >= threshold=0.0."""
        mock_bt.cur_temp = 20.0
        mock_bt.last_external_sensor_change = datetime.now() - timedelta(seconds=60)
        event = _make_event(State(SENSOR_ID, "20.0"))

        await trigger_temperature_change(mock_bt, event)

        mock_bt.control_queue_task.put.assert_awaited_once()

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
        mock_bt.last_external_sensor_change = datetime.now() - timedelta(seconds=700)
        event = _make_event(State(SENSOR_ID, "21.0"))

        await trigger_temperature_change(mock_bt, event)

        assert mock_bt.cur_temp == 21.0


# ---------------------------------------------------------------------------
# 5. Accumulation tracking
# ---------------------------------------------------------------------------


class TestAccumulationTracking:
    """Tests for accumulation state updates inside trigger_temperature_change.

    With _sig_threshold=0.0, all changes are "significant" and accepted
    after the interval elapses, so accum_delta is always reset. These tests
    verify that accumulation state is correctly maintained before acceptance.
    """

    @pytest.mark.asyncio
    async def test_accum_delta_updated_before_accept(self, mock_bt):
        """Update accum_delta with the change delta before accepting."""
        mock_bt.cur_temp = 20.0
        mock_bt.accum_delta = 0.0
        mock_bt.accum_dir = 0

        event = _make_event(State(SENSOR_ID, "20.5"))
        await trigger_temperature_change(mock_bt, event)

        # With threshold=0.0, accepted as "significant" → accum_delta reset to 0
        mock_bt.control_queue_task.put.assert_awaited_once()
        assert mock_bt.accum_delta == 0.0

    @pytest.mark.asyncio
    async def test_accumulated_change_rejected_within_debounce(self, mock_bt):
        """Reject accumulated changes within the debounce window.

        Even though accum_delta exceeds threshold, _accum_ok also
        requires _interval_ok, so debounce is enforced.
        """
        mock_bt.cur_temp = 20.0
        mock_bt.accum_delta = 0.08
        mock_bt.accum_dir = 1
        mock_bt.last_external_sensor_change = datetime.now() - timedelta(seconds=1)

        event = _make_event(State(SENSOR_ID, "20.05"))
        await trigger_temperature_change(mock_bt, event)

        # interval_ok=False → neither "significant" nor "accumulated" → rejected
        mock_bt.control_queue_task.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_accumulated_change_accepted_after_interval(self, mock_bt):
        """Accept accumulated changes when debounce interval has elapsed."""
        mock_bt.cur_temp = 20.0
        mock_bt.accum_delta = 0.08
        mock_bt.accum_dir = 1
        mock_bt.last_external_sensor_change = datetime.now() - timedelta(seconds=60)

        event = _make_event(State(SENSOR_ID, "20.05"))
        await trigger_temperature_change(mock_bt, event)

        mock_bt.control_queue_task.put.assert_awaited_once()
        assert mock_bt.accum_delta == 0.0

    @pytest.mark.asyncio
    async def test_pending_cleared_when_value_returns_to_current(self, mock_bt):
        """Clear pending_temp when the new value equals cur_temp."""
        mock_bt.cur_temp = 20.0
        mock_bt.pending_temp = 20.05
        mock_bt.pending_since = datetime.now()

        event = _make_event(State(SENSOR_ID, "20.0"))
        await trigger_temperature_change(mock_bt, event)

        assert mock_bt.pending_temp is None


# ---------------------------------------------------------------------------
# 6. Plateau logic
# ---------------------------------------------------------------------------


class TestPlateauLogic:
    """Tests for plateau acceptance paths.

    With _sig_threshold=0.0, the "not _is_significant" guard for the
    plateau path is never True. Plateau and accumulation paths only become
    meaningful when combined with the threshold fix (PR #1930).
    """

    @pytest.mark.asyncio
    async def test_small_change_accepted_as_significant_with_zero_threshold(
        self, mock_bt
    ):
        """Accept a 0.05 change as 'significant' since threshold=0.0."""
        mock_bt.cur_temp = 20.0
        mock_bt.pending_temp = 20.05
        mock_bt.pending_since = datetime.now() - timedelta(seconds=300)

        event = _make_event(State(SENSOR_ID, "20.05"))

        with patch(
            "custom_components.better_thermostat.events.temperature.async_call_later"
        ) as mock_timer:
            await trigger_temperature_change(mock_bt, event)

        mock_timer.assert_not_called()
        mock_bt.control_queue_task.put.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_any_change_accepted_skips_plateau_timer(self, mock_bt):
        """Skip plateau timer scheduling since all changes are significant."""
        mock_bt.cur_temp = 20.0

        event = _make_event(State(SENSOR_ID, "20.01"))

        with patch(
            "custom_components.better_thermostat.events.temperature.async_call_later"
        ) as mock_timer:
            await trigger_temperature_change(mock_bt, event)

        # With threshold=0.0, 0.01 diff is "significant" → accepted directly
        mock_timer.assert_not_called()
        mock_bt.control_queue_task.put.assert_awaited_once()
