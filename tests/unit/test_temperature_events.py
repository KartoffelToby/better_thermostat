"""Tests for events/temperature.py – external temperature event handlers.

Covers EMA calculation, temperature application, guard clauses, debounce
acceptance logic, accumulation tracking, plateau acceptance, and the order
in which overlapping readings and the keepalive tick reach the TRVs.
"""

import asyncio
from dataclasses import replace
from datetime import timedelta
import logging
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util
import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.decide import (
    KernelState,
    running_kernel_state,
)
from custom_components.better_thermostat.core.fsm.control_mode import (
    ControlMode,
    ControlModeState,
)
from custom_components.better_thermostat.events.temperature import (
    _commit_pending_after,
    _commit_temperature_update,
    _update_room_temperature_ema,
    temperature_filter_lock,
    trigger_temperature_change,
)
from custom_components.better_thermostat.utils.const import CONF_HOMEMATICIP, DOMAIN
from tests.factories import ThermostatStandIn, trv_from_legacy_dict

SENSOR_ID = "sensor.external_temp"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_bt():
    """Create a mock BetterThermostat instance with sensible defaults."""
    bt = ThermostatStandIn()
    bt.kernel_state = running_kernel_state()
    bt.hass = MagicMock()
    bt.device_name = "Test Thermostat"
    bt.sensor_entity_id = SENSOR_ID

    # Current temperature state
    bt.room_temperature = 20.0
    bt.prev_stable_temp = 20.0
    bt.last_change_direction = 0
    bt.last_known_external_temp = 20.0
    bt.last_external_sensor_change = dt_util.now() - timedelta(seconds=60)

    # EMA state
    bt.room_temperature_ema_tau_seconds = 300.0
    bt._room_temperature_ema_monotonic = None
    bt.room_temperature_ema = None
    bt.room_temperature_filtered = None

    # Accumulation state
    bt.accum_delta = 0.0
    bt.accum_dir = 0

    # Pending / plateau state
    bt.pending_temp = None
    bt.pending_since = None
    bt.plateau_timer_cancel = None

    # Serialisation of concurrent readings
    bt._temperature_filter_lock = None

    # Maintenance
    bt.in_maintenance = False
    bt._control_needed_after_maintenance = False

    # Startup
    bt.startup_running = False
    bt.is_removed = False

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


async def _commit_in_turn(bt, new_temp):
    """Commit a value the way the plateau timer does, holding the filter lock."""
    async with temperature_filter_lock(bt):
        await _commit_temperature_update(bt, new_temp)


# ---------------------------------------------------------------------------
# 1. EMA calculation
# ---------------------------------------------------------------------------


class TestUpdateExternalTempEma:
    """Tests for _update_room_temperature_ema()."""

    def test_first_call_returns_input(self, mock_bt):
        """Return the input value when no previous EMA exists."""
        mock_bt._room_temperature_ema_monotonic = None
        mock_bt.room_temperature_ema = None

        result = _update_room_temperature_ema(mock_bt, 21.5)

        assert result == 21.5

    def test_subsequent_call_applies_ema(self, mock_bt):
        """Blend old and new values when a previous EMA exists."""
        from time import monotonic

        mock_bt._room_temperature_ema_monotonic = monotonic() - 60.0
        mock_bt.room_temperature_ema = 20.0

        result = _update_room_temperature_ema(mock_bt, 22.0)

        assert 20.0 < result < 22.0

    def test_zero_tau_defaults_to_300(self, mock_bt):
        """Fall back to tau=300 when tau_s is zero."""
        mock_bt.room_temperature_ema_tau_seconds = 0.0
        mock_bt._room_temperature_ema_monotonic = None
        mock_bt.room_temperature_ema = None

        result = _update_room_temperature_ema(mock_bt, 21.0)

        assert result == 21.0

    def test_none_tau_defaults_to_300(self, mock_bt):
        """Fall back to tau=300 when tau_s is None."""
        mock_bt.room_temperature_ema_tau_seconds = None
        mock_bt._room_temperature_ema_monotonic = None
        mock_bt.room_temperature_ema = None

        result = _update_room_temperature_ema(mock_bt, 21.0)

        assert result == 21.0

    def test_updates_all_state_attributes(self, mock_bt):
        """Set _room_temperature_ema_monotonic, room_temperature_ema, and room_temperature_filtered."""
        mock_bt._room_temperature_ema_monotonic = None
        mock_bt.room_temperature_ema = None

        _update_room_temperature_ema(mock_bt, 21.5)

        assert mock_bt._room_temperature_ema_monotonic is not None
        assert mock_bt.room_temperature_ema == 21.5
        assert mock_bt.room_temperature_filtered == 21.5


# ---------------------------------------------------------------------------
# 2. Temperature application
# ---------------------------------------------------------------------------


def _external_temperature_quirks(refusal=None):
    """Model quirks whose ``maybe_set_external_temperature`` is a mock.

    The dispatch looks the function up on the quirks before it calls, so
    the double carries it as a real attribute rather than one a mock would
    only make up once it is asked for.
    """
    return SimpleNamespace(
        maybe_set_external_temperature=AsyncMock(side_effect=refusal)
    )


class TestCommitTemperatureUpdate:
    """Tests for _commit_temperature_update()."""

    @pytest.mark.asyncio
    async def test_updates_room_temperature(self, mock_bt):
        """Set room_temperature to the rounded new value."""
        await _commit_temperature_update(mock_bt, 21.567)

        assert mock_bt.room_temperature == 21.57

    @pytest.mark.asyncio
    async def test_updates_prev_stable_temp_on_change(self, mock_bt):
        """Store old room_temperature in prev_stable_temp when values differ."""
        mock_bt.room_temperature = 20.0

        await _commit_temperature_update(mock_bt, 21.0)

        assert mock_bt.prev_stable_temp == 20.0

    @pytest.mark.asyncio
    async def test_prev_stable_temp_unchanged_when_same(self, mock_bt):
        """Keep prev_stable_temp unchanged when new equals old."""
        mock_bt.room_temperature = 20.0
        mock_bt.prev_stable_temp = 19.0

        await _commit_temperature_update(mock_bt, 20.0)

        assert mock_bt.prev_stable_temp == 19.0

    @pytest.mark.asyncio
    async def test_direction_up(self, mock_bt):
        """Set last_change_direction to 1 on temperature increase."""
        mock_bt.room_temperature = 20.0

        await _commit_temperature_update(mock_bt, 21.0)

        assert mock_bt.last_change_direction == 1

    @pytest.mark.asyncio
    async def test_direction_down(self, mock_bt):
        """Set last_change_direction to -1 on temperature decrease."""
        mock_bt.room_temperature = 20.0

        await _commit_temperature_update(mock_bt, 19.0)

        assert mock_bt.last_change_direction == -1

    @pytest.mark.asyncio
    async def test_resets_accumulation(self, mock_bt):
        """Reset accum_delta and accum_dir to 0 after accepting."""
        mock_bt.accum_delta = 0.5
        mock_bt.accum_dir = 1

        await _commit_temperature_update(mock_bt, 21.0)

        assert mock_bt.accum_delta == 0.0
        assert mock_bt.accum_dir == 0

    @pytest.mark.asyncio
    async def test_resets_pending(self, mock_bt):
        """Reset pending_temp and pending_since to None after accepting."""
        mock_bt.pending_temp = 21.0
        mock_bt.pending_since = dt_util.now()

        await _commit_temperature_update(mock_bt, 21.0)

        assert mock_bt.pending_temp is None
        assert mock_bt.pending_since is None

    @pytest.mark.asyncio
    async def test_cancels_plateau_timer(self, mock_bt):
        """Cancel an active plateau timer and set it to None."""
        cancel_fn = MagicMock()
        mock_bt.plateau_timer_cancel = cancel_fn

        await _commit_temperature_update(mock_bt, 21.0)

        cancel_fn.assert_called_once()
        assert mock_bt.plateau_timer_cancel is None

    @pytest.mark.asyncio
    async def test_writes_ha_state(self, mock_bt):
        """Call async_write_ha_state() to publish the new temperature."""
        await _commit_temperature_update(mock_bt, 21.0)

        mock_bt.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_enqueues_control_action(self, mock_bt):
        """Enqueue a control action via request_control_cycle()."""
        await _commit_temperature_update(mock_bt, 21.0)

        mock_bt.control_queue_task.put_nowait.assert_called_once_with(mock_bt)

    @pytest.mark.asyncio
    async def test_skips_control_during_maintenance(self, mock_bt):
        """Skip put() during maintenance but set the deferred flag."""
        mock_bt.in_maintenance = True

        await _commit_temperature_update(mock_bt, 21.0)

        mock_bt.control_queue_task.put_nowait.assert_not_called()
        assert mock_bt._control_needed_after_maintenance is True

    @pytest.mark.asyncio
    async def test_quirks_external_temp_called(self, mock_bt):
        """Call model_quirks.maybe_set_external_temperature() for each TRV."""
        quirks = _external_temperature_quirks()
        mock_bt.real_trvs = {
            "climate.trv1": trv_from_legacy_dict(
                "climate.trv1", {"model_quirks": quirks}
            )
        }

        await _commit_temperature_update(mock_bt, 21.0)

        quirks.maybe_set_external_temperature.assert_awaited_once_with(
            mock_bt, "climate.trv1", 21.0
        )

    @pytest.mark.asyncio
    async def test_a_trv_awaiting_its_initialization_gets_no_external_temp(
        self, mock_bt
    ):
        """A reading reaches only the TRVs that are initialized."""
        quirks = _external_temperature_quirks()
        waiting = trv_from_legacy_dict("climate.trv1", {"model_quirks": quirks})
        waiting.awaiting_initialization = True
        mock_bt.real_trvs = {
            "climate.trv1": waiting,
            "climate.trv2": trv_from_legacy_dict(
                "climate.trv2", {"model_quirks": quirks}
            ),
        }

        await _commit_temperature_update(mock_bt, 21.0)

        quirks.maybe_set_external_temperature.assert_awaited_once_with(
            mock_bt, "climate.trv2", 21.0
        )

    @pytest.mark.parametrize(
        "refusal",
        [
            HomeAssistantError("device did not answer"),
            ServiceValidationError("value is out of range"),
            OSError("connection reset"),
        ],
        ids=["unreachable", "out_of_range", "transport"],
    )
    @pytest.mark.asyncio
    async def test_a_refused_trv_write_still_starts_a_control_cycle(
        self, mock_bt, refusal
    ):
        """The reading is already accepted when the write goes out.

        A device that answers the write with an error keeps its old valve
        position, and without a cycle on the new reading it keeps it until
        the next room sensor change: the room is then regulated on a
        temperature Better Thermostat has already discarded.
        """
        quirks = _external_temperature_quirks(refusal)
        mock_bt.real_trvs = {
            "climate.trv1": trv_from_legacy_dict(
                "climate.trv1", {"model_quirks": quirks}
            )
        }

        await _commit_temperature_update(mock_bt, 21.0)

        mock_bt.control_queue_task.put_nowait.assert_called_once_with(mock_bt)

    @pytest.mark.asyncio
    async def test_a_refused_trv_write_does_not_skip_the_other_heads(self, mock_bt):
        """Every head in the room gets the reading, whatever the first one answers.

        In a multi-head room the write goes out head by head. One device that
        refuses must not cost the remaining heads their reading, or they keep
        regulating on a room temperature Better Thermostat has discarded.
        """
        refusing = _external_temperature_quirks(
            HomeAssistantError("device did not answer")
        )
        answering = _external_temperature_quirks()
        mock_bt.real_trvs = {
            "climate.trv1": trv_from_legacy_dict(
                "climate.trv1", {"model_quirks": refusing}
            ),
            "climate.trv2": trv_from_legacy_dict(
                "climate.trv2", {"model_quirks": answering}
            ),
        }

        await _commit_temperature_update(mock_bt, 21.0)

        answering.maybe_set_external_temperature.assert_awaited_once_with(
            mock_bt, "climate.trv2", 21.0
        )

    @pytest.mark.asyncio
    async def test_a_reading_withdrawn_mid_round_is_not_written_on(self, mock_bt):
        """Heads still waiting for their write get no reading once it is gone.

        The write goes out head by head. A room temperature withdrawn while
        an earlier head is being written leaves nothing to mirror into the
        later ones.
        """

        async def _withdraw(*_args):
            mock_bt.room_temperature = None
            return True

        first = SimpleNamespace(
            maybe_set_external_temperature=AsyncMock(side_effect=_withdraw)
        )
        later = _external_temperature_quirks()
        mock_bt.real_trvs = {
            "climate.trv1": trv_from_legacy_dict(
                "climate.trv1", {"model_quirks": first}
            ),
            "climate.trv2": trv_from_legacy_dict(
                "climate.trv2", {"model_quirks": later}
            ),
        }

        await _commit_temperature_update(mock_bt, 21.0)

        first.maybe_set_external_temperature.assert_awaited_once_with(
            mock_bt, "climate.trv1", 21.0
        )
        later.maybe_set_external_temperature.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_missing_trv_map_still_starts_a_control_cycle(
        self, mock_bt, caplog
    ):
        """Losing the head list costs the write, not the cycle.

        The reading has already been accepted at this point, so the room has to
        be regulated on it even when there is nobody left to send it to.
        """
        del mock_bt.real_trvs

        with caplog.at_level(logging.WARNING):
            await _commit_temperature_update(mock_bt, 21.0)

        assert "no TRV list to write external_temperature to" in caplog.text
        mock_bt.control_queue_task.put_nowait.assert_called_once_with(mock_bt)


# ---------------------------------------------------------------------------
# 3. Guard clauses for trigger_temperature_change
# ---------------------------------------------------------------------------


class TestReturningRoomSensor:
    """Only a reading that ends an outage of the room's sensor restarts the filter."""

    @staticmethod
    async def _filtered_after(mock_bt, rung, previous_state):
        """Apply a 22 °C reading to a filter that has held 18 °C for a minute."""
        mock_bt.kernel_state = replace(
            KernelState(), control_mode=ControlModeState(mode=rung)
        )
        mock_bt.room_temperature = 18.0
        mock_bt.room_temperature_ema = 18.0
        mock_bt._room_temperature_ema_monotonic = monotonic() - 60.0
        event = MagicMock()
        event.data = {
            "old_state": previous_state,
            "new_state": State(SENSOR_ID, "22.0"),
        }

        await trigger_temperature_change(mock_bt, event)

        assert mock_bt.room_temperature == 22.0
        return mock_bt.room_temperature_filtered

    @pytest.mark.asyncio
    async def test_the_reading_that_ends_the_outage_seeds_the_filter(self, mock_bt):
        """The room ran on the TRVs, so the old filter value says nothing."""
        filtered = await self._filtered_after(
            mock_bt, ControlMode.SENSOR_FALLBACK, State(SENSOR_ID, "unavailable")
        )

        assert filtered == 22.0

    @pytest.mark.asyncio
    async def test_a_gap_the_room_stayed_on_its_sensor_through_is_filtered(
        self, mock_bt
    ):
        """A blip shorter than the ladder's debounce keeps the filter's memory."""
        filtered = await self._filtered_after(
            mock_bt, ControlMode.OPTIMAL, State(SENSOR_ID, "unavailable")
        )

        assert 18.0 < filtered < 22.0

    @pytest.mark.asyncio
    async def test_later_readings_before_the_ladder_climbs_back_are_filtered(
        self, mock_bt
    ):
        """Only the first reading after the outage restarts the filter."""
        filtered = await self._filtered_after(
            mock_bt, ControlMode.SENSOR_FALLBACK, State(SENSOR_ID, "21.5")
        )

        assert 18.0 < filtered < 22.0


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
        """Accept the first temperature reading when room_temperature is None."""
        mock_bt.room_temperature = None
        event = _make_event(State(SENSOR_ID, "21.0"))

        await trigger_temperature_change(mock_bt, event)

        mock_bt.control_queue_task.put_nowait.assert_called_once()
        assert mock_bt.room_temperature == 21.0

    @pytest.mark.asyncio
    async def test_significant_change_accepted_after_interval(self, mock_bt):
        """Accept a significant change (>= 0.11) when the interval has elapsed."""
        mock_bt.room_temperature = 20.0
        mock_bt.last_external_sensor_change = dt_util.now() - timedelta(seconds=60)
        event = _make_event(State(SENSOR_ID, "21.0"))

        await trigger_temperature_change(mock_bt, event)

        assert mock_bt.room_temperature == 21.0
        mock_bt.control_queue_task.put_nowait.assert_called_once()

    @pytest.mark.asyncio
    async def test_significant_change_within_debounce_rejected(self, mock_bt):
        """Reject a significant change within the 5s debounce window.

        The accept condition requires _interval_ok on both the "significant"
        and "accumulated" paths, so within-debounce changes are rejected.
        """
        mock_bt.room_temperature = 20.0
        mock_bt.last_external_sensor_change = dt_util.now() - timedelta(seconds=1)
        event = _make_event(State(SENSOR_ID, "20.5"))

        await trigger_temperature_change(mock_bt, event)

        assert mock_bt.room_temperature == 20.0
        mock_bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_all_homematicip_room_reads_the_sensor_at_its_own_pace(
        self, mock_bt
    ):
        """A room of HomematicIP heads takes a reading once the sensor's interval passed.

        The heads' radio limit is paced where their writes are sent, so the
        room temperature itself stays current.
        """
        mock_bt.all_trvs = [{"advanced": {CONF_HOMEMATICIP: True}}]
        mock_bt.room_temperature = 20.0
        mock_bt.last_external_sensor_change = dt_util.now() - timedelta(seconds=30)
        event = _make_event(State(SENSOR_ID, "20.5"))

        await trigger_temperature_change(mock_bt, event)

        assert mock_bt.room_temperature == 20.5
        mock_bt.control_queue_task.put_nowait.assert_called_once()

    @pytest.mark.asyncio
    async def test_sub_threshold_change_not_accepted_immediately(self, mock_bt):
        """Reject a change below the 0.11 significance threshold."""
        mock_bt.room_temperature = 20.0
        mock_bt.last_external_sensor_change = dt_util.now() - timedelta(seconds=60)
        event = _make_event(State(SENSOR_ID, "20.05"))

        await trigger_temperature_change(mock_bt, event)

        mock_bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_identical_temp_not_accepted(self, mock_bt):
        """Reject an identical temperature (diff=0.0 < threshold 0.11)."""
        mock_bt.room_temperature = 20.0
        mock_bt.last_external_sensor_change = dt_util.now() - timedelta(seconds=60)
        event = _make_event(State(SENSOR_ID, "20.0"))

        await trigger_temperature_change(mock_bt, event)

        mock_bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_accepted_temp_written_to_room_temperature(self, mock_bt):
        """Write the accepted temperature to room_temperature."""
        mock_bt.room_temperature = 20.0
        event = _make_event(State(SENSOR_ID, "21.5"))

        await trigger_temperature_change(mock_bt, event)

        assert mock_bt.room_temperature == 21.5

    @pytest.mark.asyncio
    async def test_an_all_homematicip_room_keeps_the_sensor_debounce(self, mock_bt):
        """A reading inside the sensor's own interval waits, HomematicIP or not."""
        mock_bt.all_trvs = [{"advanced": {CONF_HOMEMATICIP: True}}]
        mock_bt.room_temperature = 20.0
        mock_bt.last_external_sensor_change = dt_util.now() - timedelta(seconds=3)
        event = _make_event(State(SENSOR_ID, "21.0"))

        await trigger_temperature_change(mock_bt, event)

        assert mock_bt.room_temperature == 20.0
        mock_bt.control_queue_task.put_nowait.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Accumulation tracking
# ---------------------------------------------------------------------------


class TestAccumulationTracking:
    """Tests for accumulation state updates inside trigger_temperature_change."""

    @pytest.mark.asyncio
    async def test_sub_threshold_change_accumulates(self, mock_bt):
        """Track sub-threshold changes in accum_delta without accepting."""
        mock_bt.room_temperature = 20.0
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
        mock_bt.room_temperature = 20.0
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
        mock_bt.room_temperature = 20.0
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
        mock_bt.room_temperature = 20.0
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
        mock_bt.room_temperature = 20.0
        event = _make_event(State(SENSOR_ID, "20.05"))

        await trigger_temperature_change(mock_bt, event)

        assert mock_bt.pending_temp == 20.05

    @pytest.mark.asyncio
    async def test_pending_cleared_when_value_returns_to_current(self, mock_bt):
        """Clear pending_temp when the new value equals room_temperature."""
        mock_bt.room_temperature = 20.0
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
        mock_bt.room_temperature = 20.0
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
        mock_bt.room_temperature = 20.0

        event = _make_event(State(SENSOR_ID, "20.01"))

        with patch(
            "custom_components.better_thermostat.events.temperature.async_call_later"
        ) as mock_timer:
            await trigger_temperature_change(mock_bt, event)

        # Sub-threshold, just set pending → timer scheduled for PLATEAU_ACCEPT_WINDOW
        mock_timer.assert_called_once()
        mock_bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_plateau_timer_inside_the_sensor_interval_applies_nothing(
        self, mock_bt
    ):
        """A plateau timer firing right after an accepted reading waits.

        The timer re-checks the sensor's debounce interval when it fires, so
        a reading accepted in the meantime is not followed at once by the
        pending value.
        """
        mock_bt.room_temperature = 20.0
        mock_bt.last_external_sensor_change = dt_util.now() - timedelta(seconds=30)
        event = _make_event(State(SENSOR_ID, "20.05"))

        with patch(
            "custom_components.better_thermostat.events.temperature.async_call_later"
        ) as mock_timer:
            await trigger_temperature_change(mock_bt, event)
        plateau_callback = mock_timer.call_args.args[2]
        mock_bt.last_external_sensor_change = dt_util.now()

        await plateau_callback(dt_util.now())

        assert mock_bt.room_temperature == 20.0
        mock_bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_sub_threshold_accumulated_to_significant(self, mock_bt):
        """Accept via accumulation when small deltas sum above the threshold."""
        mock_bt.room_temperature = 20.0
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
    async def test_minus_50_exactly_accepted(self, mock_bt):
        """Temperature exactly -50.0 is on the inclusive lower bound."""
        mock_bt.room_temperature = None
        event = _make_event(State(SENSOR_ID, "-50.0"))

        await trigger_temperature_change(mock_bt, event)
        assert mock_bt.room_temperature == -50.0

    @pytest.mark.asyncio
    async def test_below_minus_50_rejected(self, mock_bt):
        """Temperature below the lower plausibility bound is rejected."""
        mock_bt.room_temperature = 20.0
        event = _make_event(State(SENSOR_ID, "-100.0"))

        with patch(
            "custom_components.better_thermostat.events.temperature.ir.async_create_issue"
        ) as mock_create_issue:
            await trigger_temperature_change(mock_bt, event)

        assert mock_bt.room_temperature == 20.0
        mock_create_issue.assert_called_once()

    @pytest.mark.asyncio
    async def test_plausible_reading_clears_the_issue(self, mock_bt):
        """A value back in range withdraws the repair issue.

        Without this the warning about an implausible reading survives the
        sensor's recovery and the user has to dismiss it by hand.
        """
        module = "custom_components.better_thermostat.events.temperature"

        with (
            patch(f"{module}.ir.async_create_issue"),
            patch(f"{module}.ir.async_delete_issue") as mock_delete_issue,
        ):
            await trigger_temperature_change(
                mock_bt, _make_event(State(SENSOR_ID, "127.0"))
            )
            mock_delete_issue.assert_not_called()

            await trigger_temperature_change(
                mock_bt, _make_event(State(SENSOR_ID, "21.0"))
            )

        mock_delete_issue.assert_called_once_with(
            mock_bt.hass, DOMAIN, "invalid_external_temperature_Test Thermostat"
        )

    @pytest.mark.asyncio
    async def test_avm_off_marker_rejected(self, mock_bt):
        """AVM Fritz!DECT 126.5 °C (OFF marker) must not update room_temperature."""
        mock_bt.room_temperature = 20.0
        event = _make_event(State(SENSOR_ID, "126.5"))

        with patch(
            "custom_components.better_thermostat.events.temperature.ir.async_create_issue"
        ) as mock_create_issue:
            await trigger_temperature_change(mock_bt, event)

        assert mock_bt.room_temperature == 20.0
        mock_create_issue.assert_called_once()

    @pytest.mark.asyncio
    async def test_avm_on_marker_rejected(self, mock_bt):
        """AVM Fritz!DECT 127.0 °C (ON marker) must not update room_temperature."""
        mock_bt.room_temperature = 20.0
        event = _make_event(State(SENSOR_ID, "127.0"))

        with patch(
            "custom_components.better_thermostat.events.temperature.ir.async_create_issue"
        ) as mock_create_issue:
            await trigger_temperature_change(mock_bt, event)

        assert mock_bt.room_temperature == 20.0
        mock_create_issue.assert_called_once()

    @pytest.mark.asyncio
    async def test_60_exactly_accepted(self, mock_bt):
        """Temperature exactly 60.0 is on the inclusive upper bound."""
        mock_bt.room_temperature = None
        event = _make_event(State(SENSOR_ID, "60.0"))

        await trigger_temperature_change(mock_bt, event)
        assert mock_bt.room_temperature == 60.0

    @pytest.mark.asyncio
    async def test_control_queue_none_no_crash_in_apply(self, mock_bt):
        """No crash when control_queue_task is None during _commit_temperature_update."""
        mock_bt.control_queue_task = None
        mock_bt.room_temperature = None
        event = _make_event(State(SENSOR_ID, "21.0"))

        await trigger_temperature_change(mock_bt, event)
        assert mock_bt.room_temperature == 21.0

    @pytest.mark.asyncio
    async def test_ema_failure_does_not_block_update(self, mock_bt):
        """EMA calculation failure should not prevent temperature update."""
        mock_bt.room_temperature = None
        # Force EMA to fail by making tau_s non-numeric
        mock_bt.room_temperature_ema_tau_seconds = "invalid"
        event = _make_event(State(SENSOR_ID, "21.0"))

        await trigger_temperature_change(mock_bt, event)
        # Temperature should still be updated despite EMA failure
        assert mock_bt.room_temperature == 21.0

    @pytest.mark.asyncio
    async def test_plateau_timer_cancelled_on_pending_value_change(self, mock_bt):
        """Changing pending value should cancel the old plateau timer."""
        mock_bt.room_temperature = 20.0
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
        mock_bt.room_temperature = 20.0
        mock_bt.last_external_sensor_change = "not_a_datetime"
        event = _make_event(State(SENSOR_ID, "21.0"))

        await trigger_temperature_change(mock_bt, event)
        # With fallback _age=999999, _interval_ok=True → accepted
        assert mock_bt.room_temperature == 21.0

    @pytest.mark.asyncio
    async def test_room_sensor_debounce_survives_a_homematicip_trv(self, mock_bt):
        """Accept a room sensor reading once the sensor's own interval elapsed.

        The debounce interval belongs to the external sensor, which is not a
        TRV, so a HomematicIP head in the same group does not lengthen it.
        """
        mock_bt.all_trvs = [
            {"advanced": {CONF_HOMEMATICIP: False}},
            {"advanced": {CONF_HOMEMATICIP: True}},
        ]
        mock_bt.room_temperature = 20.0
        mock_bt.last_external_sensor_change = dt_util.now() - timedelta(seconds=30)
        event = _make_event(State(SENSOR_ID, "21.0"))

        await trigger_temperature_change(mock_bt, event)

        assert mock_bt.room_temperature == 21.0


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
            entity_id: trv_from_legacy_dict(entity_id, {"model_quirks": quirks})
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
            _commit_in_turn(mock_bt, 21.0), _commit_in_turn(mock_bt, 22.0)
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

        cancelled = asyncio.create_task(_commit_in_turn(mock_bt, 21.0))
        await asyncio.sleep(0)
        queued = asyncio.create_task(_commit_in_turn(mock_bt, 22.0))
        await asyncio.sleep(0)

        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        quirks.gate.set()
        await queued

        assert quirks.writes == [("climate.trv1", 22.0)]
        assert mock_bt.last_known_external_temp == 22.0

    async def _arm_plateau_timer(self, mock_bt, quirks):
        """Leave 20.05 pending and return the plateau timer it arms.

        The sensor still reports the pending reading.
        """
        self._attach_trvs(mock_bt, quirks, ("climate.trv1",))
        mock_bt.hass.states.get.return_value = State(SENSOR_ID, "20.05")
        armed = []
        with patch(
            "custom_components.better_thermostat.events.temperature.async_call_later",
            side_effect=lambda _hass, _delay, callback: armed.append(callback),
        ):
            await self._take_turn_and_read(mock_bt, State(SENSOR_ID, "20.05"))
        assert (mock_bt.pending_temp, len(armed)) == (20.05, 1)
        return armed[0]

    @pytest.mark.asyncio
    async def test_a_plateau_value_nobody_superseded_is_applied(self, mock_bt):
        """Apply the pending value when the plateau timer fires on it."""
        quirks = _RecordingQuirks()
        plateau_timer = await self._arm_plateau_timer(mock_bt, quirks)

        await plateau_timer(dt_util.now())

        assert quirks.writes == [("climate.trv1", 20.05)]
        assert mock_bt.room_temperature == 20.05

    @pytest.mark.asyncio
    async def test_a_timer_that_gets_its_turn_after_removal_writes_nothing(
        self, mock_bt
    ):
        """A removed entity no longer drives its TRVs, not even from a timer.

        The timer fires while a reading holds the turn, and the entity is
        removed before the turn passes to the timer.
        """
        quirks = _RecordingQuirks()
        plateau_timer = await self._arm_plateau_timer(mock_bt, quirks)

        async with temperature_filter_lock(mock_bt):
            timer = asyncio.create_task(plateau_timer(dt_util.now()))
            await asyncio.sleep(0)
            mock_bt.is_removed = True
        await timer

        assert quirks.writes == []
        assert mock_bt.room_temperature == 20.0

    @pytest.mark.asyncio
    async def test_a_plateau_value_waits_out_the_debounce_interval(self, mock_bt):
        """Apply nothing while the sensor's debounce interval is still running.

        The timer re-checks the interval once it holds the filter, so a
        reading applied just before it fires is not followed at once by the
        pending value.
        """
        quirks = _RecordingQuirks()
        plateau_timer = await self._arm_plateau_timer(mock_bt, quirks)
        mock_bt.last_external_sensor_change = dt_util.now()

        await plateau_timer(dt_util.now())

        assert quirks.writes == []
        assert (mock_bt.room_temperature, mock_bt.pending_temp) == (20.0, 20.05)

    @pytest.mark.asyncio
    async def test_a_superseded_plateau_value_is_not_applied(self, mock_bt):
        """Leave the newer reading standing when the plateau timer comes second.

        Readings apply in arrival order: a pending value that a newer reading
        replaced while the plateau timer waited for its turn is out of date,
        so the timer writes nothing.
        """
        quirks = _RecordingQuirks()
        plateau_timer = await self._arm_plateau_timer(mock_bt, quirks)

        lock = temperature_filter_lock(mock_bt)
        await lock.acquire()
        newer = asyncio.create_task(
            self._take_turn_and_read(mock_bt, State(SENSOR_ID, "21.0"))
        )
        await asyncio.sleep(0)
        timer = asyncio.create_task(plateau_timer(dt_util.now()))
        await asyncio.sleep(0)
        lock.release()
        await asyncio.gather(newer, timer)

        assert quirks.writes == [("climate.trv1", 21.0)]
        assert mock_bt.room_temperature == 21.0

    @pytest.mark.asyncio
    async def test_a_plateau_timer_first_in_turn_leaves_a_newer_reading_to_its_event(
        self, mock_bt
    ):
        """Apply only the newer reading when the plateau timer takes the turn first.

        The sensor has already reported the newer reading, whose event waits
        for the filter behind the timer. Committing the pending value first
        would control on a value the sensor no longer reports.
        """
        quirks = _RecordingQuirks()
        plateau_timer = await self._arm_plateau_timer(mock_bt, quirks)

        newer_state = State(SENSOR_ID, "21.0")
        mock_bt.hass.states.get.return_value = newer_state
        lock = temperature_filter_lock(mock_bt)
        await lock.acquire()
        timer = asyncio.create_task(plateau_timer(dt_util.now()))
        await asyncio.sleep(0)
        newer = asyncio.create_task(self._take_turn_and_read(mock_bt, newer_state))
        await asyncio.sleep(0)
        lock.release()
        await asyncio.gather(timer, newer)

        assert quirks.writes == [("climate.trv1", 21.0)]
        assert mock_bt.room_temperature == 21.0

    @pytest.mark.asyncio
    async def test_a_plateau_value_of_a_sensor_without_a_reading_is_not_applied(
        self, mock_bt
    ):
        """A sensor that gives no usable reading any more withdrew the value."""
        quirks = _RecordingQuirks()
        plateau_timer = await self._arm_plateau_timer(mock_bt, quirks)
        mock_bt.hass.states.get.return_value = State(SENSOR_ID, "unavailable")

        await plateau_timer(dt_util.now())

        assert quirks.writes == []
        assert (mock_bt.room_temperature, mock_bt.pending_temp) == (20.0, 20.05)

    @pytest.mark.asyncio
    async def test_a_plateau_value_replaced_below_the_threshold_is_not_applied(
        self, mock_bt
    ):
        """Leave a newer sub-threshold value pending for its own window.

        A reading below the significance threshold that replaces the pending
        value while the old timer waits for its turn starts a plateau window
        of its own; the old timer does not apply it early.
        """
        quirks = _RecordingQuirks()
        plateau_timer = await self._arm_plateau_timer(mock_bt, quirks)

        rearmed = []
        lock = temperature_filter_lock(mock_bt)
        await lock.acquire()
        with patch(
            "custom_components.better_thermostat.events.temperature.async_call_later",
            side_effect=lambda _hass, _delay, callback: rearmed.append(callback),
        ):
            newer = asyncio.create_task(
                self._take_turn_and_read(mock_bt, State(SENSOR_ID, "19.97"))
            )
            await asyncio.sleep(0)
            timer = asyncio.create_task(plateau_timer(dt_util.now()))
            await asyncio.sleep(0)
            lock.release()
            await asyncio.gather(newer, timer)

        assert quirks.writes == []
        assert (mock_bt.room_temperature, mock_bt.pending_temp, len(rearmed)) == (
            20.0,
            19.97,
            1,
        )

    @pytest.mark.asyncio
    async def test_a_plateau_value_that_left_and_returned_waits_its_own_window(
        self, mock_bt
    ):
        """Leave a value that came back pending for the window it started anew.

        While the old timer waits for its turn, the pending value moves away
        and back to the value the timer was armed for. The value matches, but
        its plateau restarted with the second reading, which armed a timer of
        its own; the old timer does not apply it early.
        """
        quirks = _RecordingQuirks()
        plateau_timer = await self._arm_plateau_timer(mock_bt, quirks)

        rearmed = []
        lock = temperature_filter_lock(mock_bt)
        await lock.acquire()
        with patch(
            "custom_components.better_thermostat.events.temperature.async_call_later",
            side_effect=lambda _hass, _delay, callback: rearmed.append(callback),
        ):
            away = asyncio.create_task(
                self._take_turn_and_read(mock_bt, State(SENSOR_ID, "19.97"))
            )
            await asyncio.sleep(0)
            back = asyncio.create_task(
                self._take_turn_and_read(mock_bt, State(SENSOR_ID, "20.05"))
            )
            await asyncio.sleep(0)
            timer = asyncio.create_task(plateau_timer(dt_util.now()))
            await asyncio.sleep(0)
            lock.release()
            await asyncio.gather(away, back, timer)

        assert quirks.writes == []
        assert (mock_bt.room_temperature, mock_bt.pending_temp, len(rearmed)) == (
            20.0,
            20.05,
            2,
        )


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
        mock_bt.window_sensor_entity_id = None
        mock_bt.door_sensor_entity_id = None
        mock_bt.cooler_entity_id = None
        mock_bt.humidity_sensor_entity_id = None
        mock_bt.outdoor_sensor_entity_id = None
        mock_bt.weather_entity_id = None
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
        mock_bt._spawn_owned = lambda coro, *, name: handlers.append(
            asyncio.ensure_future(coro)
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
            "climate.trv1": trv_from_legacy_dict(
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
            entity_id: trv_from_legacy_dict(entity_id, {"model_quirks": quirks})
            for entity_id in ("climate.trv1", "climate.trv2")
        }

        tick = asyncio.create_task(
            BetterThermostat._external_temperature_keepalive(mock_bt)
        )
        await asyncio.sleep(0)
        reading = asyncio.create_task(_commit_in_turn(mock_bt, 22.0))
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


class TestPendingReadingAfterTheDebounce:
    """A reading turned away by the debounce interval alone is applied after it."""

    def _arm(self, mock_bt, value=22.3):
        """Leave ``value`` pending and arm its timer; return the timer callback.

        The sensor still reports the pending reading.
        """
        mock_bt.hass.states.get.return_value = State(SENSOR_ID, str(value))
        mock_bt.pending_temp = value
        mock_bt.pending_since = dt_util.now()
        armed = []
        with patch(
            "custom_components.better_thermostat.events.temperature.async_call_later",
            side_effect=lambda _hass, _delay, callback: armed.append(callback),
        ):
            _commit_pending_after(mock_bt, 5.0)
        (callback,) = armed
        return callback

    async def _fire(self, mock_bt, callback):
        """Fire the timer and run the work it hands to the entity."""
        mock_bt._spawn_owned = MagicMock()
        callback(dt_util.now())
        for spawn in mock_bt._spawn_owned.call_args_list:
            await spawn.args[0]

    @pytest.mark.asyncio
    async def test_the_pending_reading_is_applied_when_the_interval_is_over(
        self, mock_bt
    ):
        """The reading still pending when the timer fires is committed."""
        callback = self._arm(mock_bt)
        with patch(
            "custom_components.better_thermostat.events.temperature._commit_temperature_update",
            new=AsyncMock(),
        ) as commit:
            await self._fire(mock_bt, callback)

        commit.assert_awaited_once_with(mock_bt, 22.3)
        assert mock_bt.plateau_timer_cancel is None

    def test_the_firing_runs_as_work_the_entity_owns(self, mock_bt):
        """The commit runs in a task the entity's removal cancels.

        A timer callback Home Assistant runs on its own outlives the removal
        once it has started, and goes on writing to the remaining TRVs.
        """
        callback = self._arm(mock_bt)
        mock_bt._spawn_owned = MagicMock()

        returned = callback(dt_util.now())

        assert returned is None
        (spawn,) = mock_bt._spawn_owned.call_args_list
        spawn.args[0].close()

    @pytest.mark.asyncio
    async def test_a_reading_that_replaced_the_pending_one_is_left_alone(self, mock_bt):
        """A timer that fired after the pending reading moved on applies nothing.

        The reading that replaced it, or the commit that cleared it, took the
        filter while the timer waited for its turn.
        """
        callback = self._arm(mock_bt)
        mock_bt.pending_temp = 21.0
        with patch(
            "custom_components.better_thermostat.events.temperature._commit_temperature_update",
            new=AsyncMock(),
        ) as commit:
            await self._fire(mock_bt, callback)

        commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_removed_entity_applies_nothing(self, mock_bt):
        """Work that got the filter only after the removal writes nothing."""
        callback = self._arm(mock_bt)
        mock_bt.is_removed = True
        with patch(
            "custom_components.better_thermostat.events.temperature._commit_temperature_update",
            new=AsyncMock(),
        ) as commit:
            await self._fire(mock_bt, callback)

        commit.assert_not_awaited()

    def test_arming_cancels_the_timer_already_pending(self, mock_bt):
        """One timer per pending reading: arming replaces the one before."""
        earlier = MagicMock()
        mock_bt.plateau_timer_cancel = earlier
        self._arm(mock_bt)

        earlier.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_a_sensor_that_went_away_has_withdrawn_the_reading(self, mock_bt):
        """A sensor without a usable reading when the timer fires applies nothing.

        The room keeps its last reading while the ladder waits out the
        outage, as it does for any outage.
        """
        callback = self._arm(mock_bt)
        mock_bt.hass.states.get.return_value = State(SENSOR_ID, "unavailable")
        with patch(
            "custom_components.better_thermostat.events.temperature._commit_temperature_update",
            new=AsyncMock(),
        ) as commit:
            await self._fire(mock_bt, callback)

        commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_room_without_a_sensor_has_withdrawn_the_reading(self, mock_bt):
        """Without a room sensor no reading is still reported, so none is applied."""
        callback = self._arm(mock_bt)
        mock_bt.sensor_entity_id = None
        with patch(
            "custom_components.better_thermostat.events.temperature._commit_temperature_update",
            new=AsyncMock(),
        ) as commit:
            await self._fire(mock_bt, callback)

        commit.assert_not_awaited()

    def test_nothing_pending_arms_no_timer(self, mock_bt):
        """Without a pending reading there is nothing to apply later."""
        mock_bt.pending_temp = None
        with patch(
            "custom_components.better_thermostat.events.temperature.async_call_later"
        ) as call_later:
            _commit_pending_after(mock_bt, 5.0)

        call_later.assert_not_called()
        assert mock_bt.plateau_timer_cancel is None

    @pytest.mark.asyncio
    async def test_a_sensor_that_moved_on_leaves_its_new_reading_to_its_event(
        self, mock_bt
    ):
        """A timer that fires after the sensor moved on applies nothing.

        The newer reading's event waits for the filter behind the timer, and
        committing the pending reading first would control on a value the
        sensor no longer reports.
        """
        callback = self._arm(mock_bt)
        mock_bt.hass.states.get.return_value = State(SENSOR_ID, "22.8")
        with patch(
            "custom_components.better_thermostat.events.temperature._commit_temperature_update",
            new=AsyncMock(),
        ) as commit:
            await self._fire(mock_bt, callback)

        commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_sensor_reading_the_pending_value_more_finely_applies_it(
        self, mock_bt
    ):
        """The sensor's reading is compared at the precision readings are kept."""
        callback = self._arm(mock_bt)
        mock_bt.hass.states.get.return_value = State(SENSOR_ID, "22.3004")
        with patch(
            "custom_components.better_thermostat.events.temperature._commit_temperature_update",
            new=AsyncMock(),
        ) as commit:
            await self._fire(mock_bt, callback)

        commit.assert_awaited_once_with(mock_bt, 22.3)


class TestLadderSeesTheHandledReading:
    """The reading a handler takes is the one the ladder observes."""

    @pytest.mark.asyncio
    async def test_a_recovery_handled_after_a_new_outage_restarts_the_debounce(
        self, mock_bt
    ):
        """A new outage waits a full debounce after the reading that ended the last one.

        The handler for the returning reading can wait for the filter lock
        until the sensor has dropped out again. It still applies that
        reading, so the outage before it is over, and the next one has to
        last the whole debounce before the room moves onto the TRVs.
        """
        TestArrivalOrder._make_thermostat_checkable(mock_bt)
        mock_bt._degraded_warning_emitted = False
        trv_entity_id = "climate.trv1"
        mock_bt.real_trvs = {
            trv_entity_id: trv_from_legacy_dict(
                trv_entity_id, {"current_temperature": 21.0}
            )
        }
        live = {SENSOR_ID: State(SENSOR_ID, "unavailable")}

        def states_get(entity_id):
            if entity_id == trv_entity_id:
                return State(trv_entity_id, "heat", {"current_temperature": 21.0})
            return live.get(entity_id, State(entity_id, "21.0"))

        mock_bt.hass.states.get = states_get

        with (
            patch("custom_components.better_thermostat.utils.watcher.ir"),
            patch(
                "custom_components.better_thermostat.climate.trigger_temperature_change",
                AsyncMock(),
            ) as filter_reading,
        ):
            await BetterThermostat._handle_temperature_reading(
                mock_bt, _make_event(State(SENSOR_ID, "unavailable"))
            )
            mock_bt.clock.advance(121.0)
            # The sensor reported 21.0 and has dropped out again by the time
            # the handler for that reading runs.
            await BetterThermostat._handle_temperature_reading(
                mock_bt, _make_event(State(SENSOR_ID, "21.0"))
            )
            assert filter_reading.await_count == 2
            assert mock_bt.kernel_state.control_mode.mode == ControlMode.OPTIMAL

            await BetterThermostat._handle_temperature_reading(
                mock_bt, _make_event(State(SENSOR_ID, "unavailable"))
            )
            mock_bt.clock.advance(60.0)
            await BetterThermostat._availability_tick(mock_bt)
            assert mock_bt.kernel_state.control_mode.mode == ControlMode.OPTIMAL

            mock_bt.clock.advance(61.0)
            await BetterThermostat._availability_tick(mock_bt)

        assert mock_bt.kernel_state.control_mode.mode == ControlMode.SENSOR_FALLBACK
