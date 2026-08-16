"""Tests for events/cooler.py – Cooler event handler.

Covers guard clauses, setpoint adoption, echo suppression, unit handling,
range and cross-channel clamping, and control-queue triggering.
"""

import logging
from unittest.mock import AsyncMock, MagicMock

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfTemperature
from homeassistant.core import State
import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.events.cooler import trigger_cooler_change

ENTITY_ID = "climate.test_cooler"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_bt():
    """Create a mock BetterThermostat instance with sensible defaults."""
    bt = MagicMock()
    bt.hass = MagicMock()
    bt.hass.config.units.temperature_unit = UnitOfTemperature.CELSIUS
    bt.device_name = "Test Thermostat"
    bt.bt_hvac_mode = HVACMode.HEAT_COOL
    bt.hvac_mode = HVACMode.HEAT_COOL
    bt.bt_target_temp = 20.0
    bt.bt_target_cooltemp = 25.0
    bt.bt_target_temp_step = 0.5
    bt.bt_min_temp = 5.0
    bt.bt_max_temp = 30.0
    bt.cooler_entity_id = ENTITY_ID
    bt.last_sent_cooler_temp = None
    bt.startup_running = False
    bt.control_queue_task = AsyncMock()
    bt.context = MagicMock()  # unique context so != event.context
    bt.async_write_ha_state = MagicMock()
    bt._enforce_heat_below_cool = lambda: BetterThermostat._enforce_heat_below_cool(bt)
    bt._clamp_inbound_cool_target = lambda v: (
        BetterThermostat._clamp_inbound_cool_target(bt, v)
    )
    bt._enforce_cool_above_heat = lambda **kwargs: (
        BetterThermostat._enforce_cool_above_heat(bt, **kwargs)
    )
    # Wrapped in a spy rather than wired directly, so a test can tell which of
    # the two branches decided an event both of them would store the same
    # value for.
    bt._seed_cool_target = MagicMock(
        side_effect=lambda setpoint, entity_id: BetterThermostat._seed_cool_target(
            bt, setpoint, entity_id
        )
    )
    return bt


def _make_state(state_str="cool", attributes=None):
    """Build a minimal HA State object."""
    attrs = {"current_temperature": 26.0, "temperature": 25.0}
    if attributes is not None:
        attrs.update(attributes)
    return State(ENTITY_ID, state_str, attributes=attrs)


def _make_event(bt, new_state=None, old_state=None, entity_id=ENTITY_ID):
    """Build a mock event whose context differs from bt.context."""
    if old_state is None:
        old_state = _make_state()
    if new_state is None:
        new_state = _make_state()

    event = MagicMock()
    event.data = {
        "old_state": old_state,
        "new_state": new_state,
        "entity_id": entity_id,
    }
    event.context = MagicMock()  # differs from bt.context
    return event


# ---------------------------------------------------------------------------
# 1. Guard clauses
# ---------------------------------------------------------------------------


class TestTriggerCoolerChangeGuards:
    """Guard-clause tests for trigger_cooler_change()."""

    @pytest.mark.asyncio
    async def test_returns_early_during_startup(self, mock_bt):
        """Return early when startup is still running."""
        mock_bt.startup_running = True
        event = _make_event(mock_bt)
        await trigger_cooler_change(mock_bt, event)
        mock_bt.async_write_ha_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_early_no_queue(self, mock_bt):
        """Return early when control_queue_task is None."""
        mock_bt.control_queue_task = None
        event = _make_event(mock_bt)
        await trigger_cooler_change(mock_bt, event)
        mock_bt.async_write_ha_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_early_new_state_none(self, mock_bt):
        """Return early when new_state is None."""
        event = _make_event(mock_bt)
        event.data["new_state"] = None
        await trigger_cooler_change(mock_bt, event)
        mock_bt.control_queue_task.put.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_early_old_state_none(self, mock_bt):
        """Return early when old_state is None."""
        event = _make_event(mock_bt)
        event.data["old_state"] = None
        await trigger_cooler_change(mock_bt, event)
        mock_bt.control_queue_task.put.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_early_not_state_instance(self, mock_bt):
        """Return early when new_state is not a State instance."""
        event = _make_event(mock_bt)
        event.data["new_state"] = "not a state"
        await trigger_cooler_change(mock_bt, event)
        mock_bt.control_queue_task.put.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_early_attributes_none(self, mock_bt):
        """Return early when new_state.attributes is None."""
        new_state = MagicMock(spec=State)
        new_state.attributes = None
        old_state = _make_state()
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)
        await trigger_cooler_change(mock_bt, event)
        mock_bt.control_queue_task.put.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_own_context(self, mock_bt):
        """Skip processing when event context matches BT's own context."""
        event = _make_event(mock_bt)
        event.context = mock_bt.context
        await trigger_cooler_change(mock_bt, event)
        mock_bt.control_queue_task.put.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Setpoint adoption
# ---------------------------------------------------------------------------


class TestCoolerSetpointAdoption:
    """Tests for cooler setpoint adoption logic."""

    @pytest.mark.asyncio
    async def test_new_setpoint_adopted(self, mock_bt):
        """A new cooler setpoint should be adopted as bt_target_cooltemp."""
        old_state = _make_state(attributes={"temperature": 25.0})
        new_state = _make_state(attributes={"temperature": 27.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 27.0
        mock_bt.control_queue_task.put.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_setpoint_not_adopted_when_off(self, mock_bt):
        """No setpoint adoption when bt_hvac_mode is OFF."""
        mock_bt.bt_hvac_mode = HVACMode.OFF
        old_state = _make_state(attributes={"temperature": 25.0})
        new_state = _make_state(attributes={"temperature": 27.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 25.0  # unchanged
        mock_bt.control_queue_task.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_uses_target_temp_high_fallback(self, mock_bt):
        """When 'temperature' is missing from old_state, use 'target_temp_high'."""
        old_state = _make_state(attributes={"target_temp_high": 25.0})
        # Remove "temperature" key from old_state
        old_attrs = dict(old_state.attributes)
        old_attrs.pop("temperature", None)
        old_state = State(ENTITY_ID, "cool", attributes=old_attrs)

        new_state = State(
            ENTITY_ID,
            "cool",
            attributes={"target_temp_high": 28.0, "current_temperature": 26.0},
        )
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 28.0

    @pytest.mark.asyncio
    async def test_writes_state_even_without_main_change(self, mock_bt):
        """async_write_ha_state() is always called, even without setpoint change."""
        mock_bt.bt_hvac_mode = (
            HVACMode.OFF
        )  # OFF → no setpoint adoption → no main change
        old_state = _make_state(attributes={"temperature": 25.0})
        new_state = _make_state(attributes={"temperature": 27.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        mock_bt.async_write_ha_state.assert_called_once()
        mock_bt.control_queue_task.put.assert_not_awaited()


# ---------------------------------------------------------------------------
# 3. Setpoint clamping
# ---------------------------------------------------------------------------


class TestCoolerSetpointClamping:
    """Tests for setpoint range clamping.

    The configured range and the heating target bound the reported value in
    sequence. The cases that isolate the range clamp therefore leave the heating
    target unknown, which is the only state in which the range is the single
    bound while a cooler is configured.
    """

    @pytest.mark.asyncio
    async def test_setpoint_clamped_to_min(self, mock_bt):
        """Setpoint below min should be clamped to bt_min_temp.

        In HEAT_COOL the heating target is the tighter of the two bounds, so
        the range clamp to 5.0 is followed by the channel clamp to one step
        above the heating target.
        """
        old_state = _make_state(attributes={"temperature": 25.0})
        new_state = _make_state(attributes={"temperature": 2.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 20.5
        assert mock_bt.bt_target_temp == 20.0

    @pytest.mark.asyncio
    async def test_setpoint_clamped_to_min_without_a_heating_target(self, mock_bt):
        """With no heating target known the range is the only bound."""
        mock_bt.bt_target_temp = None
        old_state = _make_state(attributes={"temperature": 25.0})
        new_state = _make_state(attributes={"temperature": 2.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 5.0  # clamped to min

    @pytest.mark.asyncio
    async def test_setpoint_clamped_to_max(self, mock_bt):
        """Setpoint above max should be clamped to bt_max_temp."""
        old_state = _make_state(attributes={"temperature": 25.0})
        new_state = _make_state(attributes={"temperature": 35.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 30.0  # clamped to max

    @pytest.mark.asyncio
    async def test_setpoint_at_exact_min_not_clamped(self, mock_bt, caplog):
        """Setpoint exactly at min should not trigger clamping.

        The lower bound is inclusive, so the value passes through untouched once
        the heating target is out of the way. Neither bound moved it, so neither
        bound may say it did.
        """
        mock_bt.bt_target_temp = None
        old_state = _make_state(attributes={"temperature": 25.0})
        new_state = _make_state(attributes={"temperature": 5.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        caplog.set_level(logging.INFO)
        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 5.0
        assert "setpoint outside of range" not in caplog.text
        assert "does not clear the heating target" not in caplog.text

    @pytest.mark.asyncio
    async def test_setpoint_at_exact_max_not_clamped(self, mock_bt):
        """Setpoint exactly at max should not trigger clamping."""
        old_state = _make_state(attributes={"temperature": 25.0})
        new_state = _make_state(attributes={"temperature": 30.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 30.0


# ---------------------------------------------------------------------------
# 4. Heat-target protection (a cooler report may not move the heat target)
# ---------------------------------------------------------------------------


class TestHeatTargetSync:
    """A cooler report is bounded by the heat target instead of moving it.

    The cooler owns the cooling channel alone, so a reported setpoint that would
    cross the heating target is raised to clear it. The heating target is the
    user's and stays put.
    """

    @pytest.mark.asyncio
    async def test_report_equal_to_heat_target_is_raised(self, mock_bt):
        """A setpoint equal to the heat target is raised one step above it."""
        mock_bt.bt_target_temp = 25.0
        mock_bt.bt_target_cooltemp = 27.0
        old_state = _make_state(attributes={"temperature": 27.0})
        new_state = _make_state(attributes={"temperature": 25.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 25.5
        assert mock_bt.bt_target_temp == 25.0
        assert mock_bt.bt_target_temp < mock_bt.bt_target_cooltemp

    @pytest.mark.asyncio
    async def test_report_below_heat_target_is_raised(self, mock_bt):
        """A setpoint below the heat target does not drag the heat target down."""
        mock_bt.bt_target_temp = 24.0
        old_state = _make_state(attributes={"temperature": 27.0})
        new_state = _make_state(attributes={"temperature": 23.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 24.5
        assert mock_bt.bt_target_temp == 24.0
        assert mock_bt.bt_target_temp < mock_bt.bt_target_cooltemp

    @pytest.mark.asyncio
    async def test_report_above_heat_target_is_adopted_verbatim(self, mock_bt, caplog):
        """A setpoint that already clears the heat target is adopted as reported.

        Nothing yielded to anything, so the log stays quiet about the heating
        target.
        """
        mock_bt.bt_target_temp = 20.0
        old_state = _make_state(attributes={"temperature": 25.0})
        new_state = _make_state(attributes={"temperature": 27.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        caplog.set_level(logging.INFO)
        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 27.0
        assert mock_bt.bt_target_temp == 20.0  # unchanged
        assert mock_bt.bt_target_temp < mock_bt.bt_target_cooltemp
        assert "heating target" not in caplog.text

    @pytest.mark.asyncio
    async def test_clamp_to_the_heat_target_is_annunciated(self, mock_bt, caplog):
        """The user has to be able to see why the remote's value was not kept.

        Every press on the remote produces one of these, so the level stays at
        INFO and WARNING keeps its meaning of something being out of range.
        """
        mock_bt.bt_target_temp = 20.0
        old_state = _make_state(attributes={"temperature": 24.0})
        new_state = _make_state(attributes={"temperature": 18.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        caplog.set_level(logging.INFO)
        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 20.5
        assert mock_bt.bt_target_temp == 20.0
        assert (
            "reported setpoint 18.00 does not clear the heating target 20.00"
            in caplog.text
        )
        assert "keeping 20.50" in caplog.text
        levels = {
            record.levelno
            for record in caplog.records
            if "heating target" in record.getMessage()
        }
        assert levels == {logging.INFO}

    @pytest.mark.asyncio
    async def test_annunciation_at_the_maximum_states_what_was_kept(
        self, mock_bt, caplog
    ):
        """At the maximum the kept value equals the heating target it yielded to.

        No cooling setpoint above bt_max_temp exists, so the adopted value lands
        on the heating target itself. The message names the target that was not
        cleared and the value kept, both of which are true of that state.
        """
        mock_bt.bt_target_temp = 30.0
        mock_bt.bt_max_temp = 30.0
        old_state = _make_state(attributes={"temperature": 25.0})
        new_state = _make_state(attributes={"temperature": 22.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        caplog.set_level(logging.INFO)
        await trigger_cooler_change(mock_bt, event)

        assert (
            "reported setpoint 22.00 does not clear the heating target 30.00, "
            "keeping 30.00" in caplog.text
        )

    @pytest.mark.asyncio
    async def test_report_below_min_temp_is_raised_above_the_heat_target(self, mock_bt):
        """Both bounds compose: the range first, then the heat target.

        A setpoint the range clamp already lifted to bt_min_temp is lifted
        further, because the heat target sits above the minimum.
        """
        mock_bt.bt_target_temp = 6.0
        mock_bt.bt_min_temp = 5.0
        old_state = _make_state(attributes={"temperature": 25.0})
        new_state = _make_state(attributes={"temperature": 2.0})  # clamped to 5.0
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 6.5
        assert mock_bt.bt_target_temp == 6.0
        assert mock_bt.bt_target_temp < mock_bt.bt_target_cooltemp

    @pytest.mark.asyncio
    async def test_no_legal_setpoint_above_heat_target_moves_it_one_step(self, mock_bt):
        """With the heat target at the maximum the heat target yields one step.

        No cooling setpoint above bt_max_temp exists, so the cool target stops
        at the maximum and the heat target gives up exactly one step. It is
        never pulled all the way down to the reported value.
        """
        mock_bt.bt_target_temp = 30.0
        mock_bt.bt_max_temp = 30.0
        old_state = _make_state(attributes={"temperature": 25.0})
        new_state = _make_state(attributes={"temperature": 22.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 30.0
        assert mock_bt.bt_target_temp == 29.5
        assert mock_bt.bt_target_temp < mock_bt.bt_target_cooltemp

    @pytest.mark.asyncio
    async def test_a_range_narrowed_below_the_heat_target_pulls_it_inside(
        self, mock_bt
    ):
        """A heat target above the maximum is the one case that moves further.

        The range is recomputed from the children, so it can end up below a
        target already in place. The clamp stops the cooling setpoint at the
        maximum and the tie-break brings the heating target back inside the
        range, which takes more than one step.
        """
        mock_bt.bt_target_temp = 30.0
        mock_bt.bt_max_temp = 28.0
        old_state = _make_state(attributes={"temperature": 25.0})
        new_state = _make_state(attributes={"temperature": 18.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 28.0
        assert mock_bt.bt_target_temp == 27.5
        assert mock_bt.bt_target_temp < mock_bt.bt_target_cooltemp

    @pytest.mark.asyncio
    async def test_non_overlapping_child_ranges_leave_the_pair_overlapping(
        self, mock_bt, caplog
    ):
        """Children that share no range put the cool target below the minimum.

        The configured range is the overlap of what the children advertise, so a
        heater whose maximum is below the cooler's minimum leaves bt_min_temp
        above bt_max_temp. Both bounds are applied to the report in sequence and
        the maximum decides, so the cool target lands below the minimum, and the
        heat target cannot be dropped below that minimum either. The pair keeps
        the values the range allows and the overlap is annunciated as such.
        """
        # The heater advertises 5..25 and the cooler 28..30, so the derived
        # range is bt_min_temp 28 over bt_max_temp 25.
        mock_bt.bt_min_temp = 28.0
        mock_bt.bt_max_temp = 25.0
        mock_bt.bt_target_temp = 25.0
        mock_bt.bt_target_cooltemp = 20.0
        old_state = _make_state(attributes={"temperature": 22.0})
        new_state = _make_state(attributes={"temperature": 24.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        caplog.set_level(logging.WARNING)
        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 25.0
        assert mock_bt.bt_target_cooltemp < mock_bt.bt_min_temp
        assert mock_bt.bt_target_temp == 28.0
        assert (
            "heating target 25.00 set to the configured minimum 28.00, which is "
            "not below the cooling target 25.00" in caplog.text
        )

    @pytest.mark.asyncio
    async def test_zero_step_falls_back_to_half_degree(self, mock_bt):
        """A zero step falls back to 0.5 so the two targets stay apart."""
        mock_bt.bt_target_temp = 25.0
        mock_bt.bt_target_cooltemp = 27.0
        mock_bt.bt_target_temp_step = 0.0
        old_state = _make_state(attributes={"temperature": 27.0})
        new_state = _make_state(attributes={"temperature": 25.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 25.5
        assert mock_bt.bt_target_temp == 25.0
        assert mock_bt.bt_target_temp < mock_bt.bt_target_cooltemp

    @pytest.mark.asyncio
    async def test_report_is_not_bounded_without_a_heating_target(self, mock_bt):
        """An unknown heating target is no bound and stays unknown.

        There is nothing to clear and nothing to yield, so the reported setpoint
        is adopted as it arrived.
        """
        mock_bt.bt_target_temp = None
        old_state = _make_state(attributes={"temperature": 27.0})
        new_state = _make_state(attributes={"temperature": 23.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 23.0
        assert mock_bt.bt_target_temp is None


# ---------------------------------------------------------------------------
# 5. Edge cases: None / unconvertible setpoints
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases for setpoint parsing."""

    @pytest.mark.asyncio
    async def test_none_temperature_attribute(self, mock_bt):
        """No crash when temperature attribute is missing entirely."""
        old_state = State(ENTITY_ID, "cool", attributes={"current_temperature": 26.0})
        new_state = State(ENTITY_ID, "cool", attributes={"current_temperature": 26.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        # No main change (both setpoints are None)
        assert mock_bt.bt_target_cooltemp == 25.0  # unchanged
        mock_bt.control_queue_task.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unconvertible_temperature_string(self, mock_bt):
        """No crash when temperature is a non-numeric string."""
        old_state = _make_state(attributes={"temperature": "unavailable"})
        new_state = _make_state(attributes={"temperature": "unavailable"})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 25.0  # unchanged
        mock_bt.control_queue_task.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_only_old_setpoint_none(self, mock_bt):
        """No adoption when only old_setpoint is None (new is valid).

        The code requires BOTH old and new to be non-None. This means
        the very first event (old has no temperature) is silently dropped.
        """
        old_state = State(ENTITY_ID, "cool", attributes={"current_temperature": 26.0})
        new_state = _make_state(attributes={"temperature": 27.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        # Setpoint NOT adopted because old is None
        assert mock_bt.bt_target_cooltemp == 25.0
        mock_bt.control_queue_task.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_only_new_setpoint_none(self, mock_bt):
        """No adoption when only new_setpoint is None."""
        old_state = _make_state(attributes={"temperature": 25.0})
        new_state = State(ENTITY_ID, "cool", attributes={"current_temperature": 26.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 25.0
        mock_bt.control_queue_task.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_setpoint_key_is_resolved_per_state(self, mock_bt):
        """Each state picks its own attribute key.

        A cooler that switches between single-setpoint and range attributes
        between two events is read correctly, because the key is resolved per
        state.
        """
        old_state = _make_state(attributes={"temperature": 25.0})
        # new_state has target_temp_high but NOT temperature
        new_state = State(
            ENTITY_ID,
            "cool",
            attributes={"target_temp_high": 28.0, "current_temperature": 26.0},
        )
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 28.0


# ---------------------------------------------------------------------------
# 6. Echo suppression
# ---------------------------------------------------------------------------


class TestEchoSuppression:
    """Only user input on the cooler is adopted, not BT's own writes."""

    @pytest.mark.asyncio
    async def test_unchanged_setpoint_is_not_re_adopted(self, mock_bt):
        """A cooler update without a setpoint change runs no control cycle."""
        old_state = _make_state(
            attributes={"temperature": 25.0, "current_temperature": 26.0}
        )
        new_state = _make_state(
            attributes={"temperature": 25.0, "current_temperature": 26.1}
        )
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 25.0
        assert mock_bt.bt_target_temp == 20.0
        mock_bt.control_queue_task.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_echo_of_last_sent_setpoint_is_not_adopted(self, mock_bt):
        """A write BT sent is not adopted when it returns with a foreign context.

        Cloud and MQTT backed coolers publish the new setpoint from a later poll
        or broker message whose context is not BT's, so the context check alone
        does not catch it.
        """
        mock_bt.bt_target_cooltemp = 25.0
        mock_bt.last_sent_cooler_temp = 22.0
        old_state = _make_state(attributes={"temperature": 25.0})
        new_state = _make_state(attributes={"temperature": 22.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 25.0
        assert mock_bt.bt_target_temp == 20.0
        mock_bt.control_queue_task.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_device_rounding_of_own_write_is_not_adopted(self, mock_bt):
        """A device rounding BT's write to its own grid is not user input."""
        mock_bt.bt_target_cooltemp = 24.4
        mock_bt.last_sent_cooler_temp = 24.4
        old_state = _make_state(
            attributes={"temperature": 26.0, "target_temp_step": 1.0}
        )
        new_state = _make_state(
            attributes={"temperature": 24.0, "target_temp_step": 1.0}
        )
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 24.4
        mock_bt.control_queue_task.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_report_of_an_unchanged_setpoint_does_not_revert_bt(self, mock_bt):
        """A cooler republishing its old setpoint does not undo a BT-side change.

        The send cache is written only after a successful service call, so
        between a BT-side target change and that write it still holds the
        previous value. Only a setpoint the cooler itself moved is user input.
        """
        mock_bt.bt_target_cooltemp = 27.0
        mock_bt.last_sent_cooler_temp = None
        old_state = _make_state(
            attributes={"temperature": 25.0, "current_temperature": 26.0}
        )
        new_state = _make_state(
            attributes={"temperature": 25.0, "current_temperature": 26.5}
        )
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 27.0
        mock_bt.control_queue_task.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_user_change_of_one_full_step_is_adopted(self, mock_bt):
        """A change of at least one device step is user input."""
        mock_bt.bt_target_cooltemp = 24.0
        mock_bt.last_sent_cooler_temp = 24.0
        old_state = _make_state(
            attributes={"temperature": 24.0, "target_temp_step": 1.0}
        )
        new_state = _make_state(
            attributes={"temperature": 25.0, "target_temp_step": 1.0}
        )
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 25.0
        mock_bt.control_queue_task.put.assert_awaited_once()


# ---------------------------------------------------------------------------
# 7. Unit handling
# ---------------------------------------------------------------------------


class TestCoolerUnitHandling:
    """Setpoints arrive in the system unit and are stored in °C."""

    @pytest.mark.asyncio
    async def test_fahrenheit_setpoint_is_converted_to_celsius(self, mock_bt):
        """On a °F system the reported setpoint is converted, not taken as °C."""
        mock_bt.hass.config.units.temperature_unit = UnitOfTemperature.FAHRENHEIT
        mock_bt.bt_target_temp = 18.0
        old_state = _make_state(attributes={"temperature": 75.0})
        new_state = _make_state(attributes={"temperature": 68.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 20.0
        mock_bt.control_queue_task.put.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_one_fahrenheit_press_is_adopted(self, mock_bt):
        """A single press on a 1 °F cooler moves the cool target.

        75 °F and 76 °F read back as 23.89 °C and 24.44 °C, 0.55 apart, while
        the converted step is 0.5556 — a full step of user input lands below
        the step itself.
        """
        mock_bt.hass.config.units.temperature_unit = UnitOfTemperature.FAHRENHEIT
        mock_bt.bt_target_cooltemp = 23.89
        mock_bt.last_sent_cooler_temp = 23.89
        old_state = _make_state(
            attributes={"temperature": 75.0, "target_temp_step": 1.0}
        )
        new_state = _make_state(
            attributes={"temperature": 76.0, "target_temp_step": 1.0}
        )
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 24.44
        mock_bt.control_queue_task.put.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fahrenheit_step_is_converted_to_a_celsius_delta(self, mock_bt):
        """The device step is a °F delta on a °F system and must be scaled."""
        mock_bt.hass.config.units.temperature_unit = UnitOfTemperature.FAHRENHEIT
        mock_bt.bt_target_cooltemp = 21.11  # 70 °F
        mock_bt.last_sent_cooler_temp = 21.11
        old_state = _make_state(
            attributes={"temperature": 70.0, "target_temp_step": 2.0}
        )
        new_state = _make_state(
            attributes={"temperature": 73.0, "target_temp_step": 2.0}
        )
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        # 3 °F is 1.67 K, above the converted step of 1.11 K and below the
        # 2.0 the raw attribute would give, so only a converted step adopts it.
        assert mock_bt.bt_target_cooltemp == 22.78
        mock_bt.control_queue_task.put.assert_awaited_once()


# ---------------------------------------------------------------------------
# 8. Range mode
# ---------------------------------------------------------------------------


class TestRangeModeCooler:
    """Coolers running in range mode publish an empty single setpoint."""

    @pytest.mark.asyncio
    async def test_empty_temperature_falls_back_to_target_temp_high(self, mock_bt):
        """A present-but-empty 'temperature' does not hide 'target_temp_high'."""
        old_state = State(
            ENTITY_ID,
            "heat_cool",
            attributes={
                "temperature": None,
                "target_temp_high": 24.0,
                "target_temp_low": 19.0,
            },
        )
        new_state = State(
            ENTITY_ID,
            "heat_cool",
            attributes={
                "temperature": None,
                "target_temp_high": 26.0,
                "target_temp_low": 19.0,
            },
        )
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 26.0
        mock_bt.control_queue_task.put.assert_awaited_once()


# ---------------------------------------------------------------------------
# 9. Seeding an unknown cool target
# ---------------------------------------------------------------------------


class TestUnknownCoolTargetSeed:
    """An unknown cool target is seeded from the cooler's own setpoint."""

    @pytest.mark.asyncio
    async def test_seed_from_unavailable_to_available_transition(self, mock_bt):
        """A cooler returning from an outage publishes no previous setpoint."""
        mock_bt.bt_target_cooltemp = None
        old_state = State(ENTITY_ID, "unavailable", attributes={})
        new_state = _make_state(attributes={"temperature": 24.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 24.0
        mock_bt.control_queue_task.put.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_seed_from_a_cooler_that_never_moves_its_setpoint(self, mock_bt):
        """A cooler resting on its own setpoint reports no move to adopt.

        The temperature push is the only event such a cooler produces, and it
        carries the same setpoint in both states.
        """
        mock_bt.bt_target_cooltemp = None
        old_state = _make_state(
            attributes={"temperature": 24.0, "current_temperature": 26.0}
        )
        new_state = _make_state(
            attributes={"temperature": 24.0, "current_temperature": 26.5}
        )
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 24.0
        mock_bt.control_queue_task.put.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_seed_while_bt_is_off_runs_no_control_cycle(self, mock_bt):
        """A BT that is OFF learns the cool target but stays idle."""
        mock_bt.bt_hvac_mode = HVACMode.OFF
        mock_bt.bt_target_cooltemp = None
        old_state = State(ENTITY_ID, "unavailable", attributes={})
        new_state = _make_state(attributes={"temperature": 24.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 24.0
        mock_bt.control_queue_task.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_clamped_seed_warns_and_stores_the_clamped_value(
        self, mock_bt, caplog
    ):
        """A reported setpoint outside BT's range is reported, not taken silently.

        Coolers come back from an outage on a manufacturer default well below a
        configured minimum, and the clamped value is written straight back to
        the device, so the user has to be able to see where it came from: the
        line names the cooler the event arrived from alongside both values.
        """
        mock_bt.bt_target_cooltemp = None
        mock_bt.bt_min_temp = 18.0
        mock_bt.bt_target_temp = 15.0
        old_state = State(ENTITY_ID, "unavailable", attributes={})
        new_state = _make_state(attributes={"temperature": 16.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        with caplog.at_level(logging.WARNING):
            await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 18.0
        assert (
            f"Cooler {ENTITY_ID} reported setpoint 16.0 outside of range while "
            "the cool target is unknown, taking 18.0 as the cool target" in caplog.text
        )

    @pytest.mark.asyncio
    async def test_seed_colliding_with_heat_target_raises_the_cool_side(self, mock_bt):
        """The observed value yields to the heating target the user set."""
        mock_bt.bt_target_cooltemp = None
        mock_bt.bt_target_temp = 20.0
        old_state = State(ENTITY_ID, "unavailable", attributes={})
        new_state = _make_state(attributes={"temperature": 19.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 20.5
        assert mock_bt.bt_target_temp == 20.0

    @pytest.mark.asyncio
    async def test_seed_colliding_with_heat_target_is_raised_while_bt_is_off(
        self, mock_bt
    ):
        """A collision is resolved while BT is off, not left for a later cycle.

        A cooler returning from an outage on a manufacturer default below the
        heating target would otherwise keep that value until BT is switched on,
        and the first cooling cycle would then run the air conditioner down to it
        while the TRVs heat towards the heating target.
        """
        mock_bt.bt_hvac_mode = HVACMode.OFF
        mock_bt.hvac_mode = HVACMode.OFF
        mock_bt.bt_target_cooltemp = None
        mock_bt.bt_target_temp = 21.0
        old_state = State(ENTITY_ID, "unavailable", attributes={})
        new_state = _make_state(attributes={"temperature": 16.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 21.5
        assert mock_bt.bt_target_temp == 21.0
        mock_bt.control_queue_task.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_seed_decides_an_event_the_adoption_gate_would_take_too(
        self, mock_bt
    ):
        """An unknown cool target is seeded even where the gate would fire.

        This event meets the gate's conditions as well: the previous state
        publishes a setpoint, Better Thermostat is not off and the reported
        setpoint moved by more than the echo window. Both branches raise the
        reported setpoint to clear the heating target and leave that target
        where the user put it, so the pair they store is identical and only
        the seed's own call tells them apart.
        """
        mock_bt.bt_target_cooltemp = None
        mock_bt.bt_target_temp = 21.0
        old_state = _make_state(attributes={"temperature": 25.0})
        new_state = _make_state(attributes={"temperature": 19.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        mock_bt._seed_cool_target.assert_called_once()
        assert mock_bt.bt_target_cooltemp == 21.5
        assert mock_bt.bt_target_temp == 21.0
        mock_bt.control_queue_task.put.assert_awaited_once()

    @pytest.mark.parametrize("dead_state", [STATE_UNAVAILABLE, STATE_UNKNOWN])
    @pytest.mark.asyncio
    async def test_dead_cooler_does_not_seed_from_its_retained_setpoint(
        self, mock_bt, dead_state
    ):
        """A setpoint carried on a dead state is retained, not reported.

        A climate entity without a mode publishes ``unknown`` together with its
        full attributes, and one that writes the state machine directly keeps
        the attributes it last set, so a setpoint can reach this handler off a
        device that is gone. The reading says nothing about the device now, and
        the target taken from it would be written back to a device that is not
        there to confirm it. The startup seed rejects both states, so the event
        path rejects them too.
        """
        mock_bt.bt_target_cooltemp = None
        old_state = _make_state(attributes={"temperature": 24.0})
        new_state = State(ENTITY_ID, dead_state, attributes={"temperature": 24.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp is None
        mock_bt._seed_cool_target.assert_not_called()
        mock_bt.control_queue_task.put.assert_not_awaited()
        mock_bt.async_write_ha_state.assert_called_once()

    @pytest.mark.parametrize("dead_state", [STATE_UNAVAILABLE, STATE_UNKNOWN])
    @pytest.mark.asyncio
    async def test_dead_cooler_seed_is_not_handed_to_the_adoption_gate(
        self, mock_bt, dead_state
    ):
        """Declining the seed must not let the gate read the same dead state.

        A previous state carrying a setpoint the retained one differs from
        satisfies every condition of the adoption gate, which would store that
        retained value as the cool target, raised to clear the heating target.
        An unknown cool target keeps the event with the seeding branch, so
        nothing is stored at all: the cool target still being None is what
        carries the proof here, since the heating target is left where it is on
        either path.
        """
        mock_bt.bt_target_cooltemp = None
        mock_bt.bt_target_temp = 20.0
        old_state = _make_state(attributes={"temperature": 24.0})
        new_state = State(ENTITY_ID, dead_state, attributes={"temperature": 19.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp is None
        assert mock_bt.bt_target_temp == 20.0
        mock_bt._seed_cool_target.assert_not_called()
        mock_bt.control_queue_task.put.assert_not_awaited()

    @pytest.mark.parametrize("dead_state", [STATE_UNAVAILABLE, STATE_UNKNOWN])
    @pytest.mark.asyncio
    async def test_dead_cooler_setpoint_does_not_move_a_known_cool_target(
        self, mock_bt, dead_state
    ):
        """A known cool target puts the adoption gate alone on a dead state.

        With the cool target known the seeding branch is out of reach, so the
        gate decides on its own and only the reported state can stop it: the
        previous state publishes a setpoint, Better Thermostat is not off, and
        the reported value differs from the previous one by more than the echo
        window. The attribute set is the one a climate entity publishes while
        it reports ``unknown``.
        """
        mock_bt.bt_target_cooltemp = 24.0
        mock_bt.bt_target_temp = 20.0
        old_state = _make_state(attributes={"temperature": 24.0})
        new_state = State(
            ENTITY_ID,
            dead_state,
            attributes={
                "hvac_modes": [HVACMode.OFF, HVACMode.COOL],
                "min_temp": 16.0,
                "max_temp": 30.0,
                "target_temp_step": 0.5,
                "fan_modes": ["auto", "low", "high"],
                "swing_modes": ["off", "vertical"],
                "current_temperature": 26.0,
                "temperature": 19.0,
                "fan_mode": "auto",
                "swing_mode": "off",
                "friendly_name": "Test Cooler",
            },
        )
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 24.0
        assert mock_bt.bt_target_temp == 20.0
        mock_bt.control_queue_task.put.assert_not_awaited()
        mock_bt.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_known_cool_target_is_not_re_seeded(self, mock_bt):
        """With a known cool target the adoption gate keeps deciding alone.

        A transition out of an outage carries no previous setpoint, so the gate
        does not adopt and the target BT already holds survives.
        """
        mock_bt.bt_target_cooltemp = 25.0
        old_state = State(ENTITY_ID, "unavailable", attributes={})
        new_state = _make_state(attributes={"temperature": 22.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 25.0
        mock_bt.control_queue_task.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_known_cool_target_still_adopts_a_reported_move(self, mock_bt):
        """A move reported for a known cool target is decided by the adoption gate.

        The seeding branch takes precedence over that gate, so it has to leave
        every event it does not own alone: a cool target Better Thermostat
        already holds still follows what the cooler reports. Both branches
        would store this reported value, so the stored pair alone does not say
        which one ran; what separates them is that a target already known is
        never seeded again.
        """
        mock_bt.bt_target_cooltemp = 25.0
        mock_bt.bt_target_temp = 20.0
        old_state = _make_state(attributes={"temperature": 25.0})
        new_state = _make_state(attributes={"temperature": 23.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 23.0
        assert mock_bt.bt_target_temp == 20.0
        mock_bt.control_queue_task.put.assert_awaited_once()
        mock_bt._seed_cool_target.assert_not_called()


class TestDualRoleEntityReports:
    """Reports from a cooler that is also one of the controlled thermostats.

    Such a device reports into the TRV handler, which owns every reading this
    one takes. Adopting here as well would read the heating channel's own
    write as a press on the cooler's controls.
    """

    @pytest.mark.asyncio
    async def test_a_shared_entity_report_is_declined_by_the_cooler_handler(
        self, mock_bt
    ):
        """The heating channel's write does not pull the cool target down.

        The device is holding the minimum setpoint the heating channel wrote to
        switch it off. Read as a cooling press, that value drags the cooling
        target down to one step above the heating one, which is the cooling
        target sliding towards the minimum by itself.
        """
        mock_bt.real_trvs = {ENTITY_ID: MagicMock()}
        mock_bt.bt_target_cooltemp = 23.0
        mock_bt.bt_target_temp = 20.0
        old_state = _make_state(attributes={"temperature": 23.0})
        new_state = _make_state(state_str="heat", attributes={"temperature": 5.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 23.0
        assert mock_bt.bt_target_temp == 20.0
        mock_bt.control_queue_task.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_shared_entity_report_does_not_seed_the_cool_target_either(
        self, mock_bt
    ):
        """An unknown cool target is not filled from the heating channel's write."""
        mock_bt.real_trvs = {ENTITY_ID: MagicMock()}
        mock_bt.bt_target_cooltemp = None
        mock_bt.bt_target_temp = 20.0
        new_state = _make_state(state_str="heat", attributes={"temperature": 5.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=new_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp is None
        mock_bt._seed_cool_target.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_distinct_cooler_report_is_adopted(self, mock_bt):
        """A cooler of its own is untouched by the dual-role handling."""
        mock_bt.real_trvs = {"climate.radiator": MagicMock()}
        mock_bt.bt_target_cooltemp = 25.0
        mock_bt.bt_target_temp = 20.0
        old_state = _make_state(attributes={"temperature": 25.0})
        new_state = _make_state(attributes={"temperature": 23.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 23.0
        mock_bt.control_queue_task.put.assert_awaited_once()
