"""Tests for events/cooler.py – Cooler event handler.

Covers guard clauses, setpoint adoption, clamping, heat-target sync,
and control-queue triggering.
"""

from unittest.mock import AsyncMock, MagicMock

from homeassistant.components.climate.const import HVACMode
from homeassistant.core import State
import pytest

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
    bt.device_name = "Test Thermostat"
    bt.bt_hvac_mode = HVACMode.HEAT_COOL
    bt.bt_target_temp = 20.0
    bt.bt_target_cooltemp = 25.0
    bt.bt_target_temp_step = 0.5
    bt.bt_min_temp = 5.0
    bt.bt_max_temp = 30.0
    bt.startup_running = False
    bt.control_queue_task = AsyncMock()
    bt.context = MagicMock()  # unique context so != event.context
    bt.async_write_ha_state = MagicMock()
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
        mock_bt.bt_hvac_mode = HVACMode.OFF  # OFF → no setpoint adoption → no main change
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
    """Tests for setpoint range clamping."""

    @pytest.mark.asyncio
    async def test_setpoint_clamped_to_min(self, mock_bt):
        """Setpoint below min should be clamped to bt_min_temp."""
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
    async def test_setpoint_at_exact_min_not_clamped(self, mock_bt):
        """Setpoint exactly at min should not trigger clamping."""
        old_state = _make_state(attributes={"temperature": 25.0})
        new_state = _make_state(attributes={"temperature": 5.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 5.0

    @pytest.mark.asyncio
    async def test_setpoint_at_exact_max_not_clamped(self, mock_bt):
        """Setpoint exactly at max should not trigger clamping."""
        old_state = _make_state(attributes={"temperature": 25.0})
        new_state = _make_state(attributes={"temperature": 30.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 30.0


# ---------------------------------------------------------------------------
# 4. Heat-target sync (cooltemp pushes heat target down)
# ---------------------------------------------------------------------------


class TestHeatTargetSync:
    """Tests for the heat-target sync when cooltemp <= heat target."""

    @pytest.mark.asyncio
    async def test_heat_target_pushed_down_when_equal(self, mock_bt):
        """When cooltemp == heat target, heat target is pushed down by step."""
        mock_bt.bt_target_temp = 25.0
        old_state = _make_state(attributes={"temperature": 27.0})
        new_state = _make_state(attributes={"temperature": 25.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 25.0
        assert mock_bt.bt_target_temp == 24.5  # pushed down by step (0.5)

    @pytest.mark.asyncio
    async def test_heat_target_pushed_down_when_above_cooltemp(self, mock_bt):
        """When heat target > new cooltemp, heat target is pushed down."""
        mock_bt.bt_target_temp = 24.0
        old_state = _make_state(attributes={"temperature": 27.0})
        new_state = _make_state(attributes={"temperature": 23.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 23.0
        assert mock_bt.bt_target_temp == 22.5  # 23.0 - 0.5

    @pytest.mark.asyncio
    async def test_heat_target_not_pushed_when_below_cooltemp(self, mock_bt):
        """When heat target < cooltemp, heat target stays unchanged."""
        mock_bt.bt_target_temp = 20.0
        old_state = _make_state(attributes={"temperature": 25.0})
        new_state = _make_state(attributes={"temperature": 27.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 27.0
        assert mock_bt.bt_target_temp == 20.0  # unchanged

    @pytest.mark.asyncio
    async def test_heat_target_sync_not_checked_below_min(self, mock_bt):
        """Heat-target sync should still work correctly after clamping to min.

        If cooltemp is clamped to min (5.0), and heat target is >= 5.0,
        heat target should be pushed down to 4.5. But 4.5 < bt_min_temp!
        The code does NOT clamp the heat target — potential invariant violation.
        """
        mock_bt.bt_target_temp = 6.0
        mock_bt.bt_min_temp = 5.0
        old_state = _make_state(attributes={"temperature": 25.0})
        new_state = _make_state(attributes={"temperature": 2.0})  # clamped to 5.0
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        # cooltemp clamped to 5.0, heat target >= cooltemp → pushed to 4.5
        assert mock_bt.bt_target_cooltemp == 5.0
        # BUG: heat target pushed below min_temp without clamping
        assert mock_bt.bt_target_temp >= mock_bt.bt_min_temp

    @pytest.mark.asyncio
    async def test_heat_target_sync_with_zero_step(self, mock_bt):
        """When step is 0, heat-target sync produces cooltemp - 0 = cooltemp.

        This means heat target == cooltemp, which violates the invariant
        that heat < cool. The >= check would trigger again next time.
        """
        mock_bt.bt_target_temp = 25.0
        mock_bt.bt_target_temp_step = 0.0
        old_state = _make_state(attributes={"temperature": 27.0})
        new_state = _make_state(attributes={"temperature": 25.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        # With step=0: bt_target_temp = cooltemp - 0 = cooltemp
        # This should maintain heat < cool invariant
        assert mock_bt.bt_target_temp < mock_bt.bt_target_cooltemp


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
    async def test_main_key_mismatch_old_has_temp_new_has_target_temp_high(self, mock_bt):
        """Key selection uses old_state only — if old has 'temperature' but new
        only has 'target_temp_high', the new setpoint reads from wrong key.

        The _main_key is determined from old_state.attributes. If the cooler
        switches attribute schema between events, new_state is read with the
        wrong key → None → no adoption.
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

        # _main_key is "temperature" (from old_state), but new_state has
        # no "temperature" → convert_to_float("None") → None → no adoption
        # The 28.0 setpoint is silently lost
        assert mock_bt.bt_target_cooltemp == 28.0
