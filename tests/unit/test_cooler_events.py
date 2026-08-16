"""Tests for events/cooler.py – Cooler event handler.

Covers guard clauses, setpoint adoption, echo suppression, unit handling,
range and cross-channel clamping, and control-queue triggering.
"""

import logging
from unittest.mock import MagicMock

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
    # The cooler of these cases is a device of its own, so the set of
    # controlled thermostats does not contain it.
    bt.real_trvs = {"climate.radiator": MagicMock()}
    bt._cooler_last_sent = None
    bt.startup_running = False
    bt.control_queue_task = MagicMock()
    bt.context = MagicMock()  # unique context so != event.context
    # A bare MagicMock would hand out a truthy contact_open.
    bt.contact_open = False
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
        mock_bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_early_old_state_none(self, mock_bt):
        """Return early when old_state is None."""
        event = _make_event(mock_bt)
        event.data["old_state"] = None
        await trigger_cooler_change(mock_bt, event)
        mock_bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_early_not_state_instance(self, mock_bt):
        """Return early when new_state is not a State instance."""
        event = _make_event(mock_bt)
        event.data["new_state"] = "not a state"
        await trigger_cooler_change(mock_bt, event)
        mock_bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_early_attributes_none(self, mock_bt):
        """Return early when new_state.attributes is None."""
        new_state = MagicMock(spec=State)
        new_state.attributes = None
        old_state = _make_state()
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)
        await trigger_cooler_change(mock_bt, event)
        mock_bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_own_context(self, mock_bt):
        """Skip processing when event context matches BT's own context."""
        event = _make_event(mock_bt)
        event.context = mock_bt.context
        await trigger_cooler_change(mock_bt, event)
        mock_bt.control_queue_task.put_nowait.assert_not_called()


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
        mock_bt.control_queue_task.put_nowait.assert_called_once()

    @pytest.mark.asyncio
    async def test_setpoint_not_adopted_when_off(self, mock_bt):
        """No setpoint adoption when bt_hvac_mode is OFF."""
        mock_bt.bt_hvac_mode = HVACMode.OFF
        old_state = _make_state(attributes={"temperature": 25.0})
        new_state = _make_state(attributes={"temperature": 27.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 25.0  # unchanged
        mock_bt.control_queue_task.put_nowait.assert_not_called()

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
        mock_bt.control_queue_task.put_nowait.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Setpoint clamping
# ---------------------------------------------------------------------------


class TestCoolerSetpointClamping:
    """Tests for setpoint range clamping.

    The configured range and the heating target bound the reported value in
    sequence. A case that isolates the range clamp therefore leaves the heating
    target unknown, which is the only state in which the range is the single
    bound while a cooler is configured.
    """

    @pytest.mark.asyncio
    async def test_setpoint_clamped_to_min(self, mock_bt):
        """Setpoint below min is clamped to bt_min_temp and then above heat.

        The two clamps compose: the range clamp lifts the reported 2.0 to
        bt_min_temp, and the heating target lifts it one step further because a
        cooler report may not move the heating target.
        """
        old_state = _make_state(attributes={"temperature": 25.0})
        new_state = _make_state(attributes={"temperature": 2.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 20.5  # bt_target_temp 20.0 + step
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
        assert mock_bt.bt_target_temp is None

    @pytest.mark.asyncio
    async def test_setpoint_clamped_to_max(self, mock_bt):
        """Setpoint above max should be clamped to bt_max_temp."""
        old_state = _make_state(attributes={"temperature": 25.0})
        new_state = _make_state(attributes={"temperature": 35.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 30.0  # clamped to max

    @pytest.mark.asyncio
    async def test_setpoint_at_exact_min_not_range_clamped(self, mock_bt, caplog):
        """A setpoint at the range minimum is inside the range but below heat.

        Nothing is out of range, so no warning is due; the heating target still
        raises the adopted value and that is reported at INFO.
        """
        old_state = _make_state(attributes={"temperature": 25.0})
        new_state = _make_state(attributes={"temperature": 5.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        caplog.set_level(logging.INFO)
        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 20.5
        assert mock_bt.bt_target_temp == 20.0
        assert "setpoint outside of range" not in caplog.text
        assert "does not clear the heating target" in caplog.text

    @pytest.mark.asyncio
    async def test_setpoint_at_exact_max_not_clamped(self, mock_bt):
        """Setpoint exactly at max should not trigger clamping."""
        old_state = _make_state(attributes={"temperature": 25.0})
        new_state = _make_state(attributes={"temperature": 30.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 30.0

    @pytest.mark.asyncio
    async def test_clamped_setpoint_that_is_adopted_is_reported(self, mock_bt, caplog):
        """A clamp that changes BT's target is worth a warning."""
        old_state = _make_state(attributes={"temperature": 25.0})
        new_state = _make_state(attributes={"temperature": 35.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        caplog.set_level(logging.WARNING)
        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 30.0
        assert "setpoint outside of range" in caplog.text

    @pytest.mark.asyncio
    async def test_clamped_setpoint_that_is_discarded_is_silent(self, mock_bt, caplog):
        """A clamp on a value the handler drops changes nothing to report.

        The clamp pulls the reported value onto the cooling target BT already
        holds, so it is BT's own write coming back, not a user's out-of-range
        input.
        """
        mock_bt.bt_target_cooltemp = 30.0
        old_state = _make_state(attributes={"temperature": 25.0})
        new_state = _make_state(attributes={"temperature": 35.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        caplog.set_level(logging.WARNING)
        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 30.0
        assert "setpoint outside of range" not in caplog.text


# ---------------------------------------------------------------------------
# 4. Cross-channel clamp (a cooler report yields to the heating target)
# ---------------------------------------------------------------------------


class TestInboundCoolSetpointClamp:
    """A cooler report may move the cooling target only.

    The reported setpoint is raised above the heating target instead of pulling
    that target down, so a press on the air conditioner's remote cannot stop the
    radiators.
    """

    @pytest.mark.asyncio
    async def test_reported_setpoint_equal_to_heat_target_is_raised(self, mock_bt):
        """A report landing on the heating target is raised one step above it."""
        mock_bt.bt_target_temp = 25.0
        mock_bt.bt_target_cooltemp = 27.0
        old_state = _make_state(attributes={"temperature": 27.0})
        new_state = _make_state(attributes={"temperature": 25.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 25.5
        assert mock_bt.bt_target_temp == 25.0  # untouched
        assert mock_bt.bt_target_temp < mock_bt.bt_target_cooltemp

    @pytest.mark.asyncio
    async def test_reported_setpoint_below_heat_target_is_raised(self, mock_bt):
        """A report below the heating target does not drag that target down."""
        mock_bt.bt_target_temp = 24.0
        old_state = _make_state(attributes={"temperature": 27.0})
        new_state = _make_state(attributes={"temperature": 23.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 24.5
        assert mock_bt.bt_target_temp == 24.0  # untouched
        assert mock_bt.bt_target_temp < mock_bt.bt_target_cooltemp

    @pytest.mark.asyncio
    async def test_clamp_to_the_heat_target_is_annunciated(self, mock_bt, caplog):
        """The user has to be able to see why the remote's value was not kept.

        Every press on the remote produces one of these, so the level stays at
        INFO and the WARNING level keeps its meaning.
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

        No cooling value above bt_max_temp exists, so the clamp pins the adopted
        setpoint onto the heating target itself. The message names the target
        that was not cleared and the value kept, and both remain true of that
        state — it never claims the kept value ends up above the target.
        """
        mock_bt.bt_target_temp = 30.0
        mock_bt.bt_target_cooltemp = 30.0
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
        levels = {
            record.levelno
            for record in caplog.records
            if "does not clear the heating target" in record.getMessage()
        }
        assert levels == {logging.INFO}

    @pytest.mark.asyncio
    async def test_reported_setpoint_above_heat_target_is_kept(self, mock_bt, caplog):
        """A report that already clears the heating target is taken as reported."""
        mock_bt.bt_target_temp = 20.0
        old_state = _make_state(attributes={"temperature": 25.0})
        new_state = _make_state(attributes={"temperature": 27.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        caplog.set_level(logging.INFO)
        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 27.0
        assert mock_bt.bt_target_temp == 20.0  # unchanged
        assert "heating target" not in caplog.text

    @pytest.mark.asyncio
    async def test_range_clamped_setpoint_still_clears_the_heat_target(self, mock_bt):
        """Both clamps compose: into the range first, then above the heat target.

        A report below bt_min_temp is lifted to the minimum, which is still below
        the heating target, so the cross-channel clamp lifts it one step above it
        and the heating target keeps the value the user set.
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
    async def test_no_legal_value_above_heat_target_costs_one_step(self, mock_bt):
        """At the range maximum the heating target yields, but only by one step.

        With the heating target on bt_max_temp no cooling value above it exists,
        so the clamp stops at the maximum and the ordering fallback moves the
        heating target one step down — not the full distance to the reported
        value.
        """
        mock_bt.bt_target_temp = 30.0
        mock_bt.bt_target_cooltemp = 30.0
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
        """A heating target above the maximum is the one case that moves further.

        The range is recomputed from the children, so it can end up below a
        target already in place. The clamp stops the cooling setpoint at the
        maximum and the ordering fallback brings the heating target back inside
        the range, which takes more than one step.
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

        The configured range is the overlap of what the children advertise, so
        a heater whose maximum is below the cooler's minimum leaves the minimum
        above the maximum. Both bounds are applied to the report in sequence and
        the maximum decides, so the cool target lands below the minimum, and the
        heating target cannot be dropped below it. The pair keeps the values the
        range allows and the overlap is annunciated as such.
        """
        # heater advertises 5..25 and cooler 28..30, so the derived range is
        # bt_min_temp 28 over bt_max_temp 25.
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
    async def test_zero_step_falls_back_to_half_a_degree(self, mock_bt):
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
        mock_bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_unconvertible_temperature_string(self, mock_bt):
        """No crash when temperature is a non-numeric string."""
        old_state = _make_state(attributes={"temperature": "unavailable"})
        new_state = _make_state(attributes={"temperature": "unavailable"})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 25.0  # unchanged
        mock_bt.control_queue_task.put_nowait.assert_not_called()

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
        mock_bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_only_new_setpoint_none(self, mock_bt):
        """No adoption when only new_setpoint is None."""
        old_state = _make_state(attributes={"temperature": 25.0})
        new_state = State(ENTITY_ID, "cool", attributes={"current_temperature": 26.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 25.0
        mock_bt.control_queue_task.put_nowait.assert_not_called()

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


class TestContactOpenAdoption:
    """A setpoint arriving while a contact is open is not adopted."""

    @pytest.mark.asyncio
    async def test_open_contact_refuses_the_reported_setpoint(self, mock_bt, caplog):
        """Mid-airing the suppression owns the mode, so the target must hold."""
        mock_bt.contact_open = True
        old_state = _make_state(attributes={"temperature": 25.0})
        new_state = _make_state(attributes={"temperature": 27.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        caplog.set_level(logging.DEBUG)
        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 25.0
        mock_bt.control_queue_task.put_nowait.assert_not_called()
        # The handler reached the adoption decision and named the guard that
        # refused, so the change was weighed rather than missed on the way in.
        assert "setpoint change 25.0 -> 27.0 NOT adopted" in caplog.text
        assert "contact_open=True" in caplog.text

    @pytest.mark.asyncio
    async def test_closed_contact_adopts_the_same_setpoint(self, mock_bt):
        """The contact is the only thing holding that event back."""
        old_state = _make_state(attributes={"temperature": 25.0})
        new_state = _make_state(attributes={"temperature": 27.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 27.0
        mock_bt.control_queue_task.put_nowait.assert_called_once()


class TestEchoSuppression:
    """Only user input on the cooler is adopted, not BT's own writes."""

    @pytest.mark.asyncio
    async def test_unchanged_setpoint_is_not_re_adopted(self, mock_bt):
        """A report that did not move is not user intent.

        The cooler republishes its setpoint on every attribute refresh. With a
        BT-side target that has not reached the device yet, such a report would
        otherwise revert it.
        """
        mock_bt.bt_target_cooltemp = 22.0
        old_state = _make_state(
            attributes={"temperature": 25.0, "current_temperature": 26.0}
        )
        new_state = _make_state(
            attributes={"temperature": 25.0, "current_temperature": 26.1}
        )
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 22.0
        assert mock_bt.bt_target_temp == 20.0
        mock_bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_echo_of_last_sent_setpoint_is_not_adopted(self, mock_bt):
        """A write BT sent is not adopted when it returns with a foreign context.

        Cloud and MQTT backed coolers publish the new setpoint from a later poll
        or broker message whose context is not BT's, so the context check alone
        does not catch it.
        """
        mock_bt.bt_target_cooltemp = 25.0
        mock_bt._cooler_last_sent = {"temperature": (22.0, 0.0)}
        old_state = _make_state(attributes={"temperature": 25.0})
        new_state = _make_state(attributes={"temperature": 22.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 25.0
        assert mock_bt.bt_target_temp == 20.0
        mock_bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_device_rounding_of_own_write_is_not_adopted(self, mock_bt):
        """A device rounding BT's write to its own grid is not user input."""
        mock_bt.bt_target_cooltemp = 24.4
        mock_bt._cooler_last_sent = {"temperature": (24.4, 0.0)}
        old_state = _make_state(
            attributes={"temperature": 26.0, "target_temp_step": 1.0}
        )
        new_state = _make_state(
            attributes={"temperature": 24.0, "target_temp_step": 1.0}
        )
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 24.4
        mock_bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_user_change_of_one_full_step_is_adopted(self, mock_bt):
        """A change of at least one device step is user input."""
        mock_bt.bt_target_cooltemp = 24.0
        mock_bt._cooler_last_sent = {"temperature": (24.0, 0.0)}
        old_state = _make_state(
            attributes={"temperature": 24.0, "target_temp_step": 1.0}
        )
        new_state = _make_state(
            attributes={"temperature": 25.0, "target_temp_step": 1.0}
        )
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 25.0
        mock_bt.control_queue_task.put_nowait.assert_called_once()


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
        mock_bt.control_queue_task.put_nowait.assert_called_once()

    @pytest.mark.asyncio
    async def test_one_fahrenheit_press_is_adopted(self, mock_bt):
        """A single press on a 1 °F cooler moves the cool target.

        75 °F and 76 °F read back as 23.89 °C and 24.44 °C, 0.55 apart, while
        the echo window for the converted 0.5556 step is 0.5456 — a full step
        of user input clears it by a hair.
        """
        mock_bt.hass.config.units.temperature_unit = UnitOfTemperature.FAHRENHEIT
        mock_bt.bt_target_cooltemp = 23.89
        mock_bt._cooler_last_sent = {"temperature": (23.89, 0.0)}
        old_state = _make_state(
            attributes={"temperature": 75.0, "target_temp_step": 1.0}
        )
        new_state = _make_state(
            attributes={"temperature": 76.0, "target_temp_step": 1.0}
        )
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 24.44
        mock_bt.control_queue_task.put_nowait.assert_called_once()

    @pytest.mark.asyncio
    async def test_fahrenheit_step_is_converted_to_a_celsius_delta(self, mock_bt):
        """The device step is a °F delta on a °F system and must be scaled.

        One press of the up button on a 2 °F grid moves the setpoint by
        1.11 °C. Read as if the step were already Celsius, that move sits
        inside the echo window and the user's input would be swallowed.
        """
        mock_bt.hass.config.units.temperature_unit = UnitOfTemperature.FAHRENHEIT
        mock_bt.bt_target_cooltemp = 21.11  # 70 °F
        mock_bt._cooler_last_sent = {"temperature": (21.11, 0.0)}
        old_state = _make_state(
            attributes={"temperature": 70.0, "target_temp_step": 2.0}
        )
        new_state = _make_state(
            attributes={"temperature": 72.0, "target_temp_step": 2.0}
        )
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 22.22  # 72 °F
        mock_bt.control_queue_task.put_nowait.assert_called_once()


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
        mock_bt.control_queue_task.put_nowait.assert_called_once()


# ---------------------------------------------------------------------------
# 9. Seeding an unknown cool target
# ---------------------------------------------------------------------------


class TestUnknownCoolTargetSeed:
    """An unknown cool target is seeded from the first usable cooler report.

    While the cool target is None the control cycle has nothing to compare the
    room against and commands the cooler OFF, so the field has to be filled
    from the only value available: the setpoint the device itself reports.
    """

    @pytest.mark.asyncio
    async def test_cooler_returning_from_unavailable_seeds_the_target(self, mock_bt):
        """An unavailable previous state publishes no setpoint of its own.

        A restart while the cooler was offline leaves the target unknown, and
        the first event after the cooler joins carries the old state of an
        unavailable entity, which the adoption gate cannot work with.
        """
        mock_bt.bt_target_cooltemp = None
        old_state = State(ENTITY_ID, STATE_UNAVAILABLE)
        new_state = _make_state(attributes={"temperature": 24.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 24.0
        mock_bt.control_queue_task.put_nowait.assert_called_once()

    @pytest.mark.asyncio
    async def test_resting_cooler_seeds_from_a_current_temperature_push(self, mock_bt):
        """A cooler on its own setpoint never reports a move.

        The adoption gate needs the reported setpoint to have moved by half a
        step, so a device that only pushes its room reading would keep the
        target unknown forever.
        """
        mock_bt.bt_target_cooltemp = None
        old_state = _make_state(
            attributes={"temperature": 24.0, "current_temperature": 26.0}
        )
        new_state = _make_state(
            attributes={"temperature": 24.0, "current_temperature": 26.2}
        )
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 24.0
        mock_bt.control_queue_task.put_nowait.assert_called_once()

    @pytest.mark.asyncio
    async def test_seed_reports_the_adopted_value(self, mock_bt, caplog):
        """Taking a device value as the target is worth an info entry.

        The entry names the cooler the value came off, because that entity is
        what has to be looked at to find out where a target nobody chose came
        from.
        """
        mock_bt.bt_target_cooltemp = None
        new_state = _make_state(attributes={"temperature": 24.0})
        event = _make_event(mock_bt, new_state=new_state)

        caplog.set_level(logging.INFO)
        await trigger_cooler_change(mock_bt, event)

        assert (
            f"Cooler {ENTITY_ID} reports setpoint 24.0 while the cool target is "
            "unknown, taking it as the cool target" in caplog.text
        )

    @pytest.mark.asyncio
    async def test_seed_while_off_stores_without_a_control_cycle(self, mock_bt):
        """An OFF thermostat still needs the field, but no cycle to use it."""
        mock_bt.bt_hvac_mode = HVACMode.OFF
        mock_bt.hvac_mode = HVACMode.OFF
        mock_bt.bt_target_cooltemp = None
        new_state = _make_state(attributes={"temperature": 24.0})
        event = _make_event(mock_bt, new_state=new_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 24.0
        mock_bt.control_queue_task.put_nowait.assert_not_called()
        mock_bt.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_clamped_seed_is_reported(self, mock_bt, caplog):
        """A cooler back from an outage may report a manufacturer default.

        Such a default can sit below BT's range, and the clamped value is
        written back to the device, so the substitution must be visible, and
        the entry names the cooler it is written to.
        """
        mock_bt.bt_target_cooltemp = None
        mock_bt.bt_min_temp = 18.0
        mock_bt.bt_target_temp = 15.0
        new_state = _make_state(attributes={"temperature": 16.0})
        event = _make_event(mock_bt, new_state=new_state)

        caplog.set_level(logging.WARNING)
        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 18.0
        assert (
            f"Cooler {ENTITY_ID} reported setpoint 16.0 outside of range" in caplog.text
        )

    @pytest.mark.asyncio
    async def test_seed_colliding_with_the_heat_target_yields(self, mock_bt):
        """The observed value gives way, the heating target the user set stays.

        A seed carries no user intent, so a collision between the two targets
        is resolved by lifting the cooling side above the heating one. The
        adoption gate raises a colliding report to the same value and leaves
        the heating target alone as well, so the pair that ends up stored does
        not say which branch decided this event and the spy is what does.
        """
        mock_bt.bt_target_cooltemp = None
        mock_bt.bt_target_temp = 20.0
        new_state = _make_state(attributes={"temperature": 19.0})
        event = _make_event(mock_bt, new_state=new_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 20.5
        assert mock_bt.bt_target_temp == 20.0
        mock_bt._seed_cool_target.assert_called_once()

    @pytest.mark.asyncio
    async def test_seed_colliding_with_the_heat_target_yields_while_off(self, mock_bt):
        """An OFF thermostat still publishes both targets as a range.

        The range is advertised for as long as a cooler is configured, so an
        inverted pair would reach the thermostat card and, once the thermostat
        is switched back on, cap the cooler at the heating target that acts as
        its hard floor.
        """
        mock_bt.bt_hvac_mode = HVACMode.OFF
        mock_bt.hvac_mode = HVACMode.OFF
        mock_bt.bt_target_cooltemp = None
        mock_bt.bt_target_temp = 25.0
        new_state = _make_state(attributes={"temperature": 20.0})
        event = _make_event(mock_bt, new_state=new_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 25.5
        assert mock_bt.bt_target_temp == 25.0
        mock_bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_known_cool_target_is_not_re_seeded(self, mock_bt, caplog):
        """A target BT already holds is only moved by the adoption gate.

        With the previous state carrying no setpoint the gate declines, and a
        known target must stay put rather than fall back to the device value.
        """
        mock_bt.bt_target_cooltemp = 25.0
        old_state = State(ENTITY_ID, "cool", attributes={"current_temperature": 26.0})
        new_state = _make_state(attributes={"temperature": 27.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        caplog.set_level(logging.INFO)
        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 25.0
        assert "cool target is unknown" not in caplog.text
        mock_bt.control_queue_task.put_nowait.assert_not_called()

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
        mock_bt.control_queue_task.put_nowait.assert_called_once()
        mock_bt._seed_cool_target.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_target_seeds_a_report_the_gate_would_adopt(self, mock_bt):
        """A report the gate would take as well is seeded, not adopted.

        A cooler that comes back publishing a setpoint different from the one
        its previous state carried satisfies the adoption gate too: the
        previous state holds a setpoint, Better Thermostat is not off, and the
        reported value moved by more than the echo window. That gate resolves a
        collision on the cooling side as well, so it would store this very pair
        and only the spy tells the two branches apart. Which of them runs still
        matters, because the gate protects a cooling target Better Thermostat
        already holds and there is none while the target is unknown.
        """
        mock_bt.bt_target_cooltemp = None
        mock_bt.bt_target_temp = 20.0
        old_state = _make_state(attributes={"temperature": 24.0})
        new_state = _make_state(attributes={"temperature": 19.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 20.5
        assert mock_bt.bt_target_temp == 20.0
        mock_bt.control_queue_task.put_nowait.assert_called_once()
        mock_bt._seed_cool_target.assert_called_once()

    @pytest.mark.asyncio
    async def test_open_contact_does_not_suppress_the_seed(self, mock_bt):
        """An airing must not leave the cooler off for the rest of the session.

        The gate declines a report that arrives while a contact is open,
        because BT holds the cooler off and writes it no setpoint then, so
        nothing BT wrote explains what the device reports. An unknown target
        has no user intent to protect and holds the cooler off on every cycle,
        so the reading is taken and the airing keeps the cooler off by itself.
        """
        mock_bt.bt_target_cooltemp = None
        mock_bt.contact_open = True
        old_state = _make_state(attributes={"temperature": 24.0})
        new_state = _make_state(attributes={"temperature": 22.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 22.0
        assert mock_bt.bt_target_temp == 20.0
        mock_bt.control_queue_task.put_nowait.assert_called_once()

    @pytest.mark.parametrize("dead_state", [STATE_UNAVAILABLE, STATE_UNKNOWN])
    @pytest.mark.asyncio
    async def test_dead_cooler_does_not_seed_from_a_retained_setpoint(
        self, mock_bt, dead_state
    ):
        """A setpoint on a dead state is retained, not reported.

        A climate entity without a mode publishes ``unknown`` together with its
        full attributes, and one that writes the state machine directly keeps
        the attributes it last set, so a setpoint can reach this handler off a
        device that is gone. Startup declines the same two states, and a target
        taken here would be written straight back to the device.
        """
        mock_bt.bt_target_cooltemp = None
        old_state = State(ENTITY_ID, "cool", attributes={"current_temperature": 26.0})
        new_state = _make_state(state_str=dead_state, attributes={"temperature": 24.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp is None
        mock_bt._seed_cool_target.assert_not_called()
        mock_bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.parametrize("dead_state", [STATE_UNAVAILABLE, STATE_UNKNOWN])
    @pytest.mark.asyncio
    async def test_dead_cooler_seed_is_not_handed_to_the_adoption_gate(
        self, mock_bt, dead_state
    ):
        """Declining the seed must not let the gate read the same dead state.

        A previous state that carries a setpoint the retained one differs from
        satisfies every condition of the adoption gate, which would store that
        retained value as the cool target, raised clear of the heating target
        it leaves where the user set it. The cool target staying unknown is
        therefore what proves the gate never ran, together with the control
        cycle it does not request; the heating target holds either way.
        """
        mock_bt.bt_target_cooltemp = None
        mock_bt.bt_target_temp = 20.0
        old_state = _make_state(attributes={"temperature": 24.0})
        new_state = _make_state(state_str=dead_state, attributes={"temperature": 19.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp is None
        assert mock_bt.bt_target_temp == 20.0
        mock_bt.control_queue_task.put_nowait.assert_not_called()

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
        mock_bt.control_queue_task.put_nowait.assert_not_called()
        mock_bt.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_live_cooler_with_a_mode_still_seeds(self, mock_bt):
        """The guard turns on the reported state, not on the setpoint.

        A cooler that publishes a mode is live, so the same retained-looking
        report is the device's own setpoint and fills the unknown target.
        """
        mock_bt.bt_target_cooltemp = None
        old_state = State(ENTITY_ID, "cool", attributes={"current_temperature": 26.0})
        new_state = _make_state(state_str="cool", attributes={"temperature": 24.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 24.0
        mock_bt._seed_cool_target.assert_called_once()


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
        mock_bt.control_queue_task.put_nowait.assert_not_called()

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
    async def test_a_distinct_cooler_report_is_adopted_as_before(self, mock_bt):
        """A cooler of its own is untouched by the dual-role handling."""
        mock_bt.bt_target_cooltemp = 25.0
        mock_bt.bt_target_temp = 20.0
        old_state = _make_state(attributes={"temperature": 25.0})
        new_state = _make_state(attributes={"temperature": 23.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 23.0
        mock_bt.control_queue_task.put_nowait.assert_called_once()
