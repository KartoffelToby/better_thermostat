"""Tests for events/cooler.py – Cooler event handler.

Covers guard clauses, setpoint adoption, echo suppression, unit handling,
clamping, heat-target sync, and control-queue triggering.
"""

from unittest.mock import AsyncMock, MagicMock

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import UnitOfTemperature
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
    bt.last_sent_cooler_temp = None
    bt.startup_running = False
    bt.control_queue_task = AsyncMock()
    bt.context = MagicMock()  # unique context so != event.context
    bt.async_write_ha_state = MagicMock()
    bt._enforce_heat_below_cool = lambda: BetterThermostat._enforce_heat_below_cool(bt)
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
        mock_bt.bt_target_cooltemp = 27.0
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
    async def test_heat_target_sync_respects_min_temp(self, mock_bt):
        """Heat-target sync keeps the heat target inside the configured range.

        With the cool target clamped to the minimum there is no room for a full
        step below it, so the heat target stops at bt_min_temp.
        """
        mock_bt.bt_target_temp = 6.0
        mock_bt.bt_min_temp = 5.0
        old_state = _make_state(attributes={"temperature": 25.0})
        new_state = _make_state(attributes={"temperature": 2.0})  # clamped to 5.0
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 5.0
        assert mock_bt.bt_target_temp >= mock_bt.bt_min_temp

    @pytest.mark.asyncio
    async def test_heat_target_sync_with_zero_step(self, mock_bt):
        """A zero step falls back to 0.5 so heat stays below cool."""
        mock_bt.bt_target_temp = 25.0
        mock_bt.bt_target_cooltemp = 27.0
        mock_bt.bt_target_temp_step = 0.0
        old_state = _make_state(attributes={"temperature": 27.0})
        new_state = _make_state(attributes={"temperature": 25.0})
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_cooler_change(mock_bt, event)

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
