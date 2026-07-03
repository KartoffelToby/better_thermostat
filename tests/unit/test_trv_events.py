"""Tests for events/trv.py – TRV event handlers and conversion helpers.

Covers guard clauses, internal temperature changes, HVAC action/valve caching,
mode synchronisation, target-temperature adoption, control-queue triggering,
and the convert_inbound_states / convert_outbound_states helpers.
"""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.climate.const import HVACMode
from homeassistant.core import State
from homeassistant.util import dt as dt_util
import pytest

from custom_components.better_thermostat.events.trv import (
    convert_inbound_states,
    convert_outbound_states,
    trigger_trv_change,
)
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.const import (
    CONF_HOMEMATICIP,
    CalibrationMode,
    CalibrationType,
)

ENTITY_ID = "climate.test_trv"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_bt():
    """Create a mock BetterThermostat instance with sensible defaults."""
    bt = MagicMock()
    bt.hass = MagicMock()
    bt.device_name = "Test Thermostat"
    bt.bt_hvac_mode = HVACMode.HEAT
    bt.bt_target_temp = 19.0
    bt.bt_min_temp = 5.0
    bt.bt_max_temp = 30.0
    bt.bt_target_cooltemp = 25.0
    bt.bt_target_temp_step = 0.5
    bt.cur_temp = 18.0
    bt.window_open = False
    bt.tolerance = 0.3
    bt.startup_running = False
    bt.control_queue_task = AsyncMock()
    bt.bt_update_lock = False
    bt.cooler_entity_id = None
    bt.ignore_states = False
    bt.context = MagicMock()  # unique context so != event.context
    bt.last_internal_sensor_change = dt_util.now() - timedelta(seconds=60)
    bt.async_write_ha_state = MagicMock()

    bt.all_trvs = [{"advanced": {CONF_HOMEMATICIP: False}}]

    bt.real_trvs = {
        ENTITY_ID: Trv.from_legacy_dict(
            ENTITY_ID,
            {
                "hvac_mode": HVACMode.HEAT,
                "hvac_modes": [HVACMode.OFF, HVACMode.HEAT],
                "min_temp": 5.0,
                "max_temp": 30.0,
                "current_temperature": 18.0,
                "temperature": 19.0,
                "last_temperature": 19.0,
                "last_hvac_mode": "heat",
                "target_temp_received": True,
                "system_mode_received": True,
                "calibration_received": True,
                "calibration": 1,
                "last_calibration": 0.0,
                "ignore_trv_states": False,
                "model": "SomeModel",
                "model_quirks": None,
                "hvac_action": "heating",
                "valve_position": 50,
                "advanced": {
                    "calibration": CalibrationType.LOCAL_BASED,
                    "calibration_mode": CalibrationMode.DEFAULT,
                    "no_off_system_mode": False,
                    "heat_auto_swapped": False,
                    "child_lock": False,
                },
            },
        )
    }
    return bt


def _make_state(state_str="heat", attributes=None):
    """Build a minimal HA State object."""
    attrs = {"current_temperature": 18.0, "temperature": 19.0}
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


class TestUnavailableInvalidation:
    """An unavailable TRV must not keep feeding a stale internal temperature."""

    @pytest.mark.asyncio
    async def test_unavailable_trv_invalidates_internal_temperature(self, mock_bt):
        """The stored reading is cleared so calibration stops using it."""
        unavailable = State(ENTITY_ID, "unavailable")
        mock_bt.hass.states.get.return_value = unavailable

        event = _make_event(mock_bt, new_state=unavailable)
        await trigger_trv_change(mock_bt, event)

        assert mock_bt.real_trvs[ENTITY_ID].current_temperature is None

    @pytest.mark.asyncio
    async def test_first_reading_after_recovery_bypasses_debounce(self, mock_bt):
        """The first valid reading after an outage repopulates the cache at once."""
        unavailable = State(ENTITY_ID, "unavailable")
        mock_bt.hass.states.get.return_value = unavailable
        await trigger_trv_change(mock_bt, _make_event(mock_bt, new_state=unavailable))
        assert mock_bt.real_trvs[ENTITY_ID].current_temperature is None

        # The TRV recovers well inside the 5 s debounce window.
        mock_bt.last_internal_sensor_change = dt_util.now()
        trv_state = _make_state(attributes={"current_temperature": 18.0})
        mock_bt.hass.states.get.return_value = trv_state
        event = _make_event(mock_bt, new_state=trv_state, old_state=trv_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.real_trvs[ENTITY_ID].current_temperature == 18.0


class TestTriggerTrvChangeGuards:
    """Guard-clause tests for trigger_trv_change()."""

    @pytest.mark.asyncio
    async def test_returns_early_during_startup(self, mock_bt):
        """Return early when startup is still running."""
        mock_bt.startup_running = True
        event = _make_event(mock_bt)
        await trigger_trv_change(mock_bt, event)
        mock_bt.async_write_ha_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_early_no_queue(self, mock_bt):
        """Return early when control_queue_task is None."""
        mock_bt.control_queue_task = None
        event = _make_event(mock_bt)
        await trigger_trv_change(mock_bt, event)
        mock_bt.async_write_ha_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_early_none_temps(self, mock_bt):
        """Return early when bt_target_temp is None."""
        mock_bt.bt_target_temp = None
        event = _make_event(mock_bt)
        await trigger_trv_change(mock_bt, event)
        mock_bt.async_write_ha_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_early_none_cur_temp(self, mock_bt):
        """Return early when cur_temp is None."""
        mock_bt.cur_temp = None
        event = _make_event(mock_bt)
        await trigger_trv_change(mock_bt, event)
        mock_bt.async_write_ha_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_early_none_tolerance(self, mock_bt):
        """Return early when tolerance is None."""
        mock_bt.tolerance = None
        event = _make_event(mock_bt)
        await trigger_trv_change(mock_bt, event)
        mock_bt.async_write_ha_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_early_update_lock(self, mock_bt):
        """Return early when bt_update_lock is True."""
        mock_bt.bt_update_lock = True
        event = _make_event(mock_bt)
        await trigger_trv_change(mock_bt, event)
        mock_bt.async_write_ha_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_early_new_state_none(self, mock_bt):
        """Return early when new_state is None."""
        event = _make_event(mock_bt)
        event.data["new_state"] = None
        await trigger_trv_change(mock_bt, event)
        mock_bt.control_queue_task.put.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_early_old_state_none(self, mock_bt):
        """Return early when old_state is None."""
        event = _make_event(mock_bt)
        event.data["old_state"] = None
        await trigger_trv_change(mock_bt, event)
        mock_bt.control_queue_task.put.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_own_context(self, mock_bt):
        """Skip processing when event context matches BT's own context."""
        event = _make_event(mock_bt)
        event.context = mock_bt.context
        await trigger_trv_change(mock_bt, event)
        mock_bt.control_queue_task.put.assert_not_called()

    @pytest.mark.asyncio
    async def test_org_trv_state_none_returns_early(self, mock_bt):
        """Return early when hass.states.get() returns None (no crash)."""
        mock_bt.hass.states.get.return_value = None
        event = _make_event(mock_bt)

        await trigger_trv_change(mock_bt, event)
        mock_bt.control_queue_task.put.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Internal temperature change
# ---------------------------------------------------------------------------


class TestInternalTemperatureChange:
    """Tests for TRV internal-temperature-sensor updates."""

    @pytest.mark.asyncio
    async def test_temp_change_updates_cache(self, mock_bt):
        """A new TRV temperature reading should update the cache."""
        new_temp = 20.0
        trv_state = _make_state(attributes={"current_temperature": new_temp})
        mock_bt.hass.states.get.return_value = trv_state
        mock_bt.real_trvs[ENTITY_ID].current_temperature = 18.0
        mock_bt.real_trvs[ENTITY_ID].calibration_received = True

        event = _make_event(mock_bt, new_state=trv_state, old_state=trv_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.real_trvs[ENTITY_ID].current_temperature == new_temp

    @pytest.mark.asyncio
    async def test_fahrenheit_current_temp_without_unit_attr(self, mock_bt):
        """A Fahrenheit TRV with no unit attribute is read via the system unit.

        HA climate entities report in the system unit and expose no
        ``temperature_unit`` attribute. With a Fahrenheit system, a raw
        64 reading is 64 °F (≈17.8 °C) — a plausible indoor value. Without the
        system-unit fallback it is mistaken for 64 °C, rejected as implausible
        and dropped.
        """
        from homeassistant.const import UnitOfTemperature

        mock_bt.hass.config.units.temperature_unit = UnitOfTemperature.FAHRENHEIT
        trv_state = _make_state(attributes={"current_temperature": 64.0})
        mock_bt.hass.states.get.return_value = trv_state
        mock_bt.real_trvs[ENTITY_ID].current_temperature = 18.0
        mock_bt.real_trvs[ENTITY_ID].calibration_received = True

        event = _make_event(mock_bt, new_state=trv_state, old_state=trv_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        # 64 °F -> ~17.78 °C, accepted and cached (not dropped as implausible).
        assert mock_bt.real_trvs[ENTITY_ID].current_temperature == pytest.approx(
            17.78, abs=0.05
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("marker_temp", [126.5, 127.0])
    async def test_implausible_trv_temp_ignored(self, mock_bt, marker_temp):
        """AVM marker values (126.5 °C OFF, 127.0 °C ON) must not overwrite the cache."""
        trv_state = _make_state(attributes={"current_temperature": marker_temp})
        mock_bt.hass.states.get.return_value = trv_state
        mock_bt.real_trvs[ENTITY_ID].current_temperature = 20.0
        mock_bt.real_trvs[ENTITY_ID].calibration_received = True

        event = _make_event(mock_bt, new_state=trv_state, old_state=trv_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.real_trvs[ENTITY_ID].current_temperature == 20.0

    @pytest.mark.asyncio
    async def test_temp_change_respects_time_diff(self, mock_bt):
        """Changes within 5 s of the last internal sensor change are skipped."""
        mock_bt.last_internal_sensor_change = dt_util.now() - timedelta(seconds=2)
        trv_state = _make_state(attributes={"current_temperature": 20.0})
        mock_bt.hass.states.get.return_value = trv_state
        mock_bt.real_trvs[ENTITY_ID].current_temperature = 18.0
        mock_bt.real_trvs[ENTITY_ID].calibration_received = True
        mock_bt.real_trvs[ENTITY_ID].calibration = 1

        event = _make_event(mock_bt, new_state=trv_state, old_state=trv_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        # Temperature NOT updated because <5 s elapsed and calibration_received=True
        assert mock_bt.real_trvs[ENTITY_ID].current_temperature == 18.0

    @pytest.mark.asyncio
    async def test_temp_change_homematicip_600s(self, mock_bt):
        """HomematicIP uses a 600 s guard instead of 5 s."""
        mock_bt.all_trvs = [{"advanced": {CONF_HOMEMATICIP: True}}]
        mock_bt.last_internal_sensor_change = dt_util.now() - timedelta(seconds=30)
        trv_state = _make_state(attributes={"current_temperature": 20.0})
        mock_bt.hass.states.get.return_value = trv_state
        mock_bt.real_trvs[ENTITY_ID].current_temperature = 18.0
        mock_bt.real_trvs[ENTITY_ID].calibration_received = True
        mock_bt.real_trvs[ENTITY_ID].calibration = 1

        event = _make_event(mock_bt, new_state=trv_state, old_state=trv_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        # 30 s elapsed < 600 s → blocked
        assert mock_bt.real_trvs[ENTITY_ID].current_temperature == 18.0

    @pytest.mark.asyncio
    async def test_calibration_received_flag_set(self, mock_bt):
        """calibration_received should be set True on first temp change."""
        mock_bt.real_trvs[ENTITY_ID].calibration_received = False
        mock_bt.real_trvs[ENTITY_ID].calibration = 1
        trv_state = _make_state(attributes={"current_temperature": 20.0})
        mock_bt.hass.states.get.return_value = trv_state
        mock_bt.real_trvs[ENTITY_ID].current_temperature = 18.0

        event = _make_event(mock_bt, new_state=trv_state, old_state=trv_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.real_trvs[ENTITY_ID].calibration_received is True

    @pytest.mark.asyncio
    async def test_calibration_received_resets_main_change(self, mock_bt):
        """When calibration is first received, _main_change should become False."""
        mock_bt.real_trvs[ENTITY_ID].calibration_received = False
        mock_bt.real_trvs[ENTITY_ID].calibration = 1
        trv_state = _make_state(attributes={"current_temperature": 20.0})
        mock_bt.hass.states.get.return_value = trv_state
        mock_bt.real_trvs[ENTITY_ID].current_temperature = 18.0

        event = _make_event(mock_bt, new_state=trv_state, old_state=trv_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        mock_bt.control_queue_task.put.assert_not_called()

    @pytest.mark.asyncio
    async def test_calibration_zero_fetches_offset(self, mock_bt):
        """When calibration==0, get_current_offset() should be called."""
        mock_bt.real_trvs[ENTITY_ID].calibration_received = False
        mock_bt.real_trvs[ENTITY_ID].calibration = 0
        trv_state = _make_state(attributes={"current_temperature": 20.0})
        mock_bt.hass.states.get.return_value = trv_state
        mock_bt.real_trvs[ENTITY_ID].current_temperature = 18.0

        event = _make_event(mock_bt, new_state=trv_state, old_state=trv_state)

        with (
            patch(
                "custom_components.better_thermostat.events.trv.get_current_offset",
                new_callable=AsyncMock,
                return_value=2.5,
            ) as mock_offset,
            patch(
                "custom_components.better_thermostat.events.trv.convert_inbound_states",
                return_value=HVACMode.HEAT,
            ),
        ):
            await trigger_trv_change(mock_bt, event)

        mock_offset.assert_awaited_once_with(mock_bt, ENTITY_ID)
        assert mock_bt.real_trvs[ENTITY_ID].last_calibration == 2.5


# ---------------------------------------------------------------------------
# 3. HVAC action and valve position
# ---------------------------------------------------------------------------


class TestHvacActionAndValvePosition:
    """Tests for hvac_action / valve_position cache updates."""

    @pytest.mark.asyncio
    async def test_hvac_action_updated_from_attribute(self, mock_bt):
        """Cache hvac_action from the TRV state attribute."""
        trv_state = _make_state(
            attributes={
                "current_temperature": 18.0,
                "temperature": 19.0,
                "hvac_action": "idle",
            }
        )
        mock_bt.hass.states.get.return_value = trv_state
        mock_bt.real_trvs[ENTITY_ID].hvac_action = "heating"

        event = _make_event(mock_bt, new_state=trv_state, old_state=trv_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.real_trvs[ENTITY_ID].hvac_action == "idle"

    @pytest.mark.asyncio
    async def test_hvac_action_fallback_to_action(self, mock_bt):
        """Fallback: use 'action' attribute when 'hvac_action' is absent."""
        trv_state = _make_state(
            attributes={
                "current_temperature": 18.0,
                "temperature": 19.0,
                "action": "Heating",
            }
        )
        mock_bt.hass.states.get.return_value = trv_state
        mock_bt.real_trvs[ENTITY_ID].hvac_action = "idle"

        event = _make_event(mock_bt, new_state=trv_state, old_state=trv_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.real_trvs[ENTITY_ID].hvac_action == "heating"

    @pytest.mark.asyncio
    async def test_hvac_action_change_triggers_main_change(self, mock_bt):
        """A changed hvac_action value should trigger _main_change."""
        trv_state = _make_state(
            attributes={
                "current_temperature": 18.0,
                "temperature": 19.0,
                "hvac_action": "idle",
            }
        )
        mock_bt.hass.states.get.return_value = trv_state
        mock_bt.real_trvs[ENTITY_ID].hvac_action = "heating"

        event = _make_event(mock_bt, new_state=trv_state, old_state=trv_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        mock_bt.control_queue_task.put.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_valve_position_updated(self, mock_bt):
        """Cache valve_position converted to float from TRV state."""
        trv_state = _make_state(
            attributes={
                "current_temperature": 18.0,
                "temperature": 19.0,
                "valve_position": "75",
            }
        )
        mock_bt.hass.states.get.return_value = trv_state

        event = _make_event(mock_bt, new_state=trv_state, old_state=trv_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.real_trvs[ENTITY_ID].valve_position == 75.0


# ---------------------------------------------------------------------------
# 4. HVAC mode update
# ---------------------------------------------------------------------------


class TestHvacModeUpdate:
    """Tests for HVAC mode synchronisation."""

    @pytest.mark.asyncio
    async def test_mode_change_updates_cache(self, mock_bt):
        """New mode from TRV is written to real_trvs cache."""
        trv_state = _make_state(
            state_str="off",
            attributes={"current_temperature": 18.0, "temperature": 19.0},
        )
        mock_bt.hass.states.get.return_value = trv_state
        mock_bt.real_trvs[ENTITY_ID].hvac_mode = "heat"

        event = _make_event(
            mock_bt, new_state=trv_state, old_state=_make_state(state_str="heat")
        )

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.OFF,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.real_trvs[ENTITY_ID].hvac_mode == "off"

    @pytest.mark.asyncio
    async def test_mode_change_blocked_by_child_lock(self, mock_bt):
        """Child lock prevents mode cache update."""
        mock_bt.real_trvs[ENTITY_ID].advanced["child_lock"] = True
        trv_state = _make_state(
            state_str="off",
            attributes={"current_temperature": 18.0, "temperature": 19.0},
        )
        mock_bt.hass.states.get.return_value = trv_state
        mock_bt.real_trvs[ENTITY_ID].hvac_mode = "heat"

        event = _make_event(
            mock_bt, new_state=trv_state, old_state=_make_state(state_str="heat")
        )

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.OFF,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.real_trvs[ENTITY_ID].hvac_mode == "heat"

    @pytest.mark.asyncio
    async def test_mode_propagates_to_bt_hvac_mode(self, mock_bt):
        """Mode change propagates to bt_hvac_mode when conditions are met."""
        trv_state = _make_state(
            state_str="off",
            attributes={"current_temperature": 18.0, "temperature": 19.0},
        )
        mock_bt.hass.states.get.return_value = trv_state
        mock_bt.real_trvs[ENTITY_ID].hvac_mode = "heat"
        mock_bt.real_trvs[ENTITY_ID].system_mode_received = True
        mock_bt.real_trvs[ENTITY_ID].last_hvac_mode = "heat"
        mock_bt.real_trvs[ENTITY_ID].advanced["child_lock"] = False

        event = _make_event(
            mock_bt, new_state=trv_state, old_state=_make_state(state_str="heat")
        )

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.OFF,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.bt_hvac_mode == HVACMode.OFF

    @pytest.mark.asyncio
    async def test_mode_not_propagated_before_system_mode_received(self, mock_bt):
        """No propagation to bt_hvac_mode if system_mode_received is False."""
        trv_state = _make_state(
            state_str="off",
            attributes={"current_temperature": 18.0, "temperature": 19.0},
        )
        mock_bt.hass.states.get.return_value = trv_state
        mock_bt.real_trvs[ENTITY_ID].hvac_mode = "heat"
        mock_bt.real_trvs[ENTITY_ID].system_mode_received = False

        event = _make_event(
            mock_bt, new_state=trv_state, old_state=_make_state(state_str="heat")
        )

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.OFF,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.bt_hvac_mode == HVACMode.HEAT

    @pytest.mark.asyncio
    async def test_unmapped_mode_ignored(self, mock_bt):
        """Mode outside (OFF, HEAT, HEAT_COOL) doesn't update cache."""
        trv_state = _make_state(
            state_str="cool",
            attributes={"current_temperature": 18.0, "temperature": 19.0},
        )
        mock_bt.hass.states.get.return_value = trv_state
        mock_bt.real_trvs[ENTITY_ID].hvac_mode = "heat"

        event = _make_event(
            mock_bt, new_state=trv_state, old_state=_make_state(state_str="heat")
        )

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=None,  # unmapped
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.real_trvs[ENTITY_ID].hvac_mode == "heat"

    @pytest.mark.asyncio
    async def test_child_lock_none_blocks_mode_propagation(self, mock_bt):
        """Mode cache updates but bt_hvac_mode does not propagate when child_lock is None."""
        mock_bt.real_trvs[ENTITY_ID].advanced.pop("child_lock", None)
        trv_state = _make_state(
            state_str="off",
            attributes={"current_temperature": 18.0, "temperature": 19.0},
        )
        mock_bt.hass.states.get.return_value = trv_state
        mock_bt.real_trvs[ENTITY_ID].hvac_mode = "heat"
        mock_bt.real_trvs[ENTITY_ID].system_mode_received = True
        mock_bt.real_trvs[ENTITY_ID].last_hvac_mode = "heat"

        event = _make_event(
            mock_bt, new_state=trv_state, old_state=_make_state(state_str="heat")
        )

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.OFF,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.real_trvs[ENTITY_ID].hvac_mode == "off"
        assert mock_bt.bt_hvac_mode == HVACMode.HEAT


# ---------------------------------------------------------------------------
# 5. Target temperature adoption
# ---------------------------------------------------------------------------


class TestTargetTempAdoption:
    """Tests for setpoint adoption from TRV events."""

    @pytest.mark.asyncio
    async def test_new_setpoint_adopted(self, mock_bt):
        """A new TRV setpoint should be adopted as bt_target_temp."""
        old_state = _make_state(
            attributes={"temperature": 19.0, "current_temperature": 18.0}
        )
        new_state = _make_state(
            attributes={"temperature": 22.0, "current_temperature": 18.0}
        )
        trv_state = _make_state(
            state_str="heat",
            attributes={"current_temperature": 18.0, "temperature": 22.0},
        )
        mock_bt.hass.states.get.return_value = trv_state
        mock_bt.real_trvs[ENTITY_ID].last_temperature = 19.0

        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.bt_target_temp == 22.0

    @pytest.mark.asyncio
    async def test_same_setpoint_not_adopted(self, mock_bt):
        """Setpoint == bt_target_temp should not trigger adoption."""
        old_state = _make_state(
            attributes={"temperature": 19.0, "current_temperature": 18.0}
        )
        new_state = _make_state(
            attributes={"temperature": 19.0, "current_temperature": 18.0}
        )
        trv_state = _make_state(
            state_str="heat",
            attributes={"current_temperature": 18.0, "temperature": 19.0},
        )
        mock_bt.hass.states.get.return_value = trv_state

        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.bt_target_temp == 19.0

    @pytest.mark.asyncio
    async def test_setpoint_clamped_to_min(self, mock_bt):
        """Setpoint below min should be clamped."""
        old_state = _make_state(
            attributes={"temperature": 19.0, "current_temperature": 18.0}
        )
        new_state = _make_state(
            attributes={"temperature": 3.0, "current_temperature": 18.0}
        )
        trv_state = _make_state(
            state_str="heat",
            attributes={"current_temperature": 18.0, "temperature": 3.0},
        )
        mock_bt.hass.states.get.return_value = trv_state
        mock_bt.real_trvs[ENTITY_ID].last_temperature = 19.0

        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.bt_target_temp == 5.0

    @pytest.mark.asyncio
    async def test_setpoint_clamped_to_max(self, mock_bt):
        """Setpoint above max should be clamped."""
        old_state = _make_state(
            attributes={"temperature": 19.0, "current_temperature": 18.0}
        )
        new_state = _make_state(
            attributes={"temperature": 35.0, "current_temperature": 18.0}
        )
        trv_state = _make_state(
            state_str="heat",
            attributes={"current_temperature": 18.0, "temperature": 35.0},
        )
        mock_bt.hass.states.get.return_value = trv_state
        mock_bt.real_trvs[ENTITY_ID].last_temperature = 19.0

        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.bt_target_temp == 30.0

    @pytest.mark.asyncio
    async def test_setpoint_blocked_when_off(self, mock_bt):
        """No setpoint adoption when bt_hvac_mode is OFF."""
        mock_bt.bt_hvac_mode = HVACMode.OFF
        old_state = _make_state(
            attributes={"temperature": 19.0, "current_temperature": 18.0}
        )
        new_state = _make_state(
            attributes={"temperature": 22.0, "current_temperature": 18.0}
        )
        trv_state = _make_state(
            state_str="heat",
            attributes={"current_temperature": 18.0, "temperature": 22.0},
        )
        mock_bt.hass.states.get.return_value = trv_state

        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.bt_target_temp == 19.0

    @pytest.mark.asyncio
    async def test_setpoint_blocked_window_open(self, mock_bt):
        """No setpoint adoption when window is open."""
        mock_bt.window_open = True
        old_state = _make_state(
            attributes={"temperature": 19.0, "current_temperature": 18.0}
        )
        new_state = _make_state(
            attributes={"temperature": 22.0, "current_temperature": 18.0}
        )
        trv_state = _make_state(
            state_str="heat",
            attributes={"current_temperature": 18.0, "temperature": 22.0},
        )
        mock_bt.hass.states.get.return_value = trv_state

        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.bt_target_temp == 19.0

    @pytest.mark.asyncio
    async def test_setpoint_uses_target_temp_low_fallback(self, mock_bt):
        """When 'temperature' is missing, 'target_temp_low' is used."""
        old_state = State(
            ENTITY_ID,
            "heat",
            attributes={"target_temp_low": 19.0, "current_temperature": 18.0},
        )
        new_state = State(
            ENTITY_ID,
            "heat",
            attributes={"target_temp_low": 22.0, "current_temperature": 18.0},
        )
        trv_state = State(
            ENTITY_ID,
            "heat",
            attributes={"current_temperature": 18.0, "target_temp_low": 22.0},
        )
        mock_bt.hass.states.get.return_value = trv_state
        mock_bt.real_trvs[ENTITY_ID].last_temperature = 19.0

        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.bt_target_temp == 22.0

    @pytest.mark.asyncio
    async def test_cooler_sync_logic_bug(self, mock_bt):
        """Cooler-sync always sets cooltemp to target - step regardless of initial value."""
        mock_bt.cooler_entity_id = "climate.cooler"
        mock_bt.bt_target_cooltemp = 25.0
        mock_bt.bt_target_temp_step = 0.5

        old_state = _make_state(
            attributes={"temperature": 19.0, "current_temperature": 18.0}
        )
        new_state = _make_state(
            attributes={"temperature": 22.0, "current_temperature": 18.0}
        )
        trv_state = _make_state(
            state_str="heat",
            attributes={"current_temperature": 18.0, "temperature": 22.0},
        )
        mock_bt.hass.states.get.return_value = trv_state
        mock_bt.real_trvs[ENTITY_ID].last_temperature = 19.0

        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 25.0

    @pytest.mark.asyncio
    async def test_cooler_sync_pushes_cooltemp_above_target(self, mock_bt):
        """Cooltemp is pushed to target + step when equal to target."""
        mock_bt.cooler_entity_id = "climate.cooler"
        mock_bt.bt_target_cooltemp = 22.0  # equal to new target
        mock_bt.bt_target_temp_step = 0.5

        old_state = _make_state(
            attributes={"temperature": 19.0, "current_temperature": 18.0}
        )
        new_state = _make_state(
            attributes={"temperature": 22.0, "current_temperature": 18.0}
        )
        trv_state = _make_state(
            state_str="heat",
            attributes={"current_temperature": 18.0, "temperature": 22.0},
        )
        mock_bt.hass.states.get.return_value = trv_state
        mock_bt.real_trvs[ENTITY_ID].last_temperature = 19.0

        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 22.0 + 0.5

    @pytest.mark.asyncio
    async def test_cooler_sync_always_overwrites(self, mock_bt):
        """Cooltemp is pushed to target + step even when already below target."""
        mock_bt.cooler_entity_id = "climate.cooler"
        mock_bt.bt_target_cooltemp = 15.0
        mock_bt.bt_target_temp_step = 0.5

        old_state = _make_state(
            attributes={"temperature": 19.0, "current_temperature": 18.0}
        )
        new_state = _make_state(
            attributes={"temperature": 22.0, "current_temperature": 18.0}
        )
        trv_state = _make_state(
            state_str="heat",
            attributes={"current_temperature": 18.0, "temperature": 22.0},
        )
        mock_bt.hass.states.get.return_value = trv_state
        mock_bt.real_trvs[ENTITY_ID].last_temperature = 19.0

        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.bt_target_cooltemp == 22.0 + 0.5

    @pytest.mark.asyncio
    async def test_no_off_system_mode_sets_off_at_min(self, mock_bt):
        """no_off_system_mode + setpoint==min_temp → OFF."""
        mock_bt.real_trvs[ENTITY_ID].advanced["no_off_system_mode"] = True
        mock_bt.real_trvs[ENTITY_ID].min_temp = 5.0
        old_state = _make_state(
            attributes={"temperature": 19.0, "current_temperature": 18.0}
        )
        new_state = _make_state(
            attributes={"temperature": 5.0, "current_temperature": 18.0}
        )
        trv_state = _make_state(
            state_str="heat",
            attributes={"current_temperature": 18.0, "temperature": 5.0},
        )
        mock_bt.hass.states.get.return_value = trv_state

        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.bt_hvac_mode == HVACMode.OFF

    @pytest.mark.asyncio
    async def test_no_off_system_mode_sets_heat_above_min(self, mock_bt):
        """no_off_system_mode: setpoint above min_temp while BT is OFF switches to HEAT."""
        mock_bt.real_trvs[ENTITY_ID].advanced["no_off_system_mode"] = True
        mock_bt.real_trvs[ENTITY_ID].min_temp = 5.0
        mock_bt.bt_hvac_mode = HVACMode.OFF  # start as OFF
        old_state = _make_state(
            attributes={"temperature": 5.0, "current_temperature": 18.0}
        )
        new_state = _make_state(
            attributes={"temperature": 20.0, "current_temperature": 18.0}
        )
        trv_state = _make_state(
            state_str="heat",
            attributes={"current_temperature": 18.0, "temperature": 20.0},
        )
        mock_bt.hass.states.get.return_value = trv_state
        mock_bt.real_trvs[ENTITY_ID].last_temperature = 5.0

        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.bt_hvac_mode == HVACMode.HEAT


class TestTargetTempBasedSync:
    """User-initiated TRV setpoint changes must propagate to BT.

    Even when calibration is TARGET_TEMP_BASED. Device-side echoes within step
    distance of BT's known values are still suppressed.
    """

    def _set_target_temp_based(self, mock_bt):
        mock_bt.real_trvs[ENTITY_ID].advanced["calibration"] = (
            CalibrationType.TARGET_TEMP_BASED
        )

    @pytest.mark.asyncio
    async def test_user_change_picked_up(self, mock_bt):
        """User raises TRV from 19.0 to 22.0 — bt_target_temp follows."""
        self._set_target_temp_based(mock_bt)
        mock_bt.bt_target_temp = 19.0
        mock_bt.real_trvs[ENTITY_ID].last_temperature = 19.0

        old_state = _make_state(
            attributes={"temperature": 19.0, "current_temperature": 18.0}
        )
        new_state = _make_state(
            attributes={"temperature": 22.0, "current_temperature": 18.0}
        )
        trv_state = _make_state(
            state_str="heat",
            attributes={"current_temperature": 18.0, "temperature": 22.0},
        )
        mock_bt.hass.states.get.return_value = trv_state

        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.bt_target_temp == 22.0

    @pytest.mark.asyncio
    async def test_echo_within_step_suppressed(self, mock_bt):
        """Device echoes 21.3 after BT wrote 21.0 (step=0.5) — treated as echo."""
        self._set_target_temp_based(mock_bt)
        mock_bt.bt_target_temp = 21.0
        mock_bt.bt_target_temp_step = 0.5
        mock_bt.real_trvs[ENTITY_ID].last_temperature = 21.0
        mock_bt.real_trvs[ENTITY_ID].target_temp_step = 0.5

        old_state = _make_state(
            attributes={"temperature": 21.0, "current_temperature": 18.0}
        )
        new_state = _make_state(
            attributes={"temperature": 21.3, "current_temperature": 18.0}
        )
        trv_state = _make_state(
            state_str="heat",
            attributes={"current_temperature": 18.0, "temperature": 21.3},
        )
        mock_bt.hass.states.get.return_value = trv_state

        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.bt_target_temp == 21.0

    @pytest.mark.asyncio
    async def test_change_at_one_step_is_user(self, mock_bt):
        """Change equal to one full step is a user change, not an echo."""
        self._set_target_temp_based(mock_bt)
        mock_bt.bt_target_temp = 21.0
        mock_bt.bt_target_temp_step = 0.5
        mock_bt.real_trvs[ENTITY_ID].last_temperature = 21.0
        mock_bt.real_trvs[ENTITY_ID].target_temp_step = 0.5

        old_state = _make_state(
            attributes={"temperature": 21.0, "current_temperature": 18.0}
        )
        new_state = _make_state(
            attributes={"temperature": 21.5, "current_temperature": 18.0}
        )
        trv_state = _make_state(
            state_str="heat",
            attributes={"current_temperature": 18.0, "temperature": 21.5},
        )
        mock_bt.hass.states.get.return_value = trv_state

        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.bt_target_temp == 21.5

    @pytest.mark.asyncio
    async def test_user_change_after_echo_not_suppressed(self, mock_bt):
        """A user change following a device echo is still adopted.

        Setup mimics the post-echo state: BT wrote 21.0, device echoed
        21.3 (within step), so the TRV's currently-published state is 21.3.
        The user then dials to 21.5. ``_old_heating_setpoint`` is 21.3 (the
        echo), not a BT-written value — it must not feed into echo detection.
        """
        self._set_target_temp_based(mock_bt)
        mock_bt.bt_target_temp = 21.0
        mock_bt.bt_target_temp_step = 0.5
        mock_bt.real_trvs[ENTITY_ID].last_temperature = 21.0
        mock_bt.real_trvs[ENTITY_ID].target_temp_step = 0.5

        old_state = _make_state(
            attributes={"temperature": 21.3, "current_temperature": 18.0}
        )
        new_state = _make_state(
            attributes={"temperature": 21.5, "current_temperature": 18.0}
        )
        trv_state = _make_state(
            state_str="heat",
            attributes={"current_temperature": 18.0, "temperature": 21.5},
        )
        mock_bt.hass.states.get.return_value = trv_state

        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.bt_target_temp == 21.5


# ---------------------------------------------------------------------------
# 6. Control queue trigger
# ---------------------------------------------------------------------------


class TestControlQueueTrigger:
    """Tests for final control-queue triggering."""

    @pytest.mark.asyncio
    async def test_main_change_triggers_queue(self, mock_bt):
        """_main_change=True should call control_queue_task.put()."""
        trv_state = _make_state(
            attributes={
                "current_temperature": 18.0,
                "temperature": 19.0,
                "hvac_action": "idle",
            }
        )
        mock_bt.hass.states.get.return_value = trv_state
        mock_bt.real_trvs[ENTITY_ID].hvac_action = "heating"

        event = _make_event(mock_bt, new_state=trv_state, old_state=trv_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        mock_bt.control_queue_task.put.assert_awaited_once()
        mock_bt.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_change_still_writes_state(self, mock_bt):
        """Even without _main_change, async_write_ha_state() is called."""
        trv_state = _make_state(
            attributes={"current_temperature": 18.0, "temperature": 19.0}
        )
        mock_bt.hass.states.get.return_value = trv_state

        event = _make_event(mock_bt, new_state=trv_state, old_state=trv_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        mock_bt.async_write_ha_state.assert_called_once()
        mock_bt.control_queue_task.put.assert_not_awaited()


# ---------------------------------------------------------------------------
# 7. convert_inbound_states
# ---------------------------------------------------------------------------


class TestConvertInboundStates:
    """Tests for convert_inbound_states()."""

    def test_none_state_raises_typeerror(self, mock_bt):
        """Raise TypeError when state is None."""
        with pytest.raises(TypeError):
            convert_inbound_states(mock_bt, ENTITY_ID, None)  # type: ignore[arg-type]

    def test_none_attributes_raises_typeerror(self, mock_bt):
        """Raise TypeError when state.attributes is None."""
        state = MagicMock(spec=State)
        state.attributes = None
        state.state = "heat"
        with pytest.raises(TypeError):
            convert_inbound_states(mock_bt, ENTITY_ID, state)

    def test_none_state_value_raises_typeerror(self, mock_bt):
        """Raise TypeError when state.state is None."""
        state = MagicMock(spec=State)
        state.attributes = {"temperature": 20}
        state.state = None
        with pytest.raises(TypeError):
            convert_inbound_states(mock_bt, ENTITY_ID, state)

    def test_off_mode_returned(self, mock_bt):
        """Return HVACMode.OFF for an OFF state."""
        state = _make_state(state_str="off")
        with patch(
            "custom_components.better_thermostat.events.trv.mode_remap",
            return_value=HVACMode.OFF,
        ):
            result = convert_inbound_states(mock_bt, ENTITY_ID, state)
        assert result == HVACMode.OFF

    def test_heat_mode_returned(self, mock_bt):
        """Return HVACMode.HEAT for a HEAT state."""
        state = _make_state(state_str="heat")
        with patch(
            "custom_components.better_thermostat.events.trv.mode_remap",
            return_value=HVACMode.HEAT,
        ):
            result = convert_inbound_states(mock_bt, ENTITY_ID, state)
        assert result == HVACMode.HEAT

    def test_unsupported_mode_returns_none(self, mock_bt):
        """Return None for unsupported HVAC modes like COOL."""
        state = _make_state(state_str="cool")
        with patch(
            "custom_components.better_thermostat.events.trv.mode_remap",
            return_value=HVACMode.COOL,
        ):
            result = convert_inbound_states(mock_bt, ENTITY_ID, state)
        assert result is None


# ---------------------------------------------------------------------------
# 8. convert_outbound_states
# ---------------------------------------------------------------------------


class TestConvertOutboundStates:
    """Tests for convert_outbound_states()."""

    def test_local_based_calibration_payload(self, mock_bt):
        """LOCAL_BASED produces payload with local_temperature_calibration."""
        mock_bt.real_trvs[ENTITY_ID].advanced["calibration"] = (
            CalibrationType.LOCAL_BASED
        )
        mock_bt.real_trvs[ENTITY_ID].current_temperature = 18.0

        with (
            patch(
                "custom_components.better_thermostat.events.trv.calculate_calibration_local",
                return_value=2.5,
            ),
            patch(
                "custom_components.better_thermostat.events.trv.mode_remap",
                return_value=HVACMode.HEAT,
            ),
        ):
            result = convert_outbound_states(mock_bt, ENTITY_ID, HVACMode.HEAT)

        assert result is not None
        assert result["local_temperature_calibration"] == 2.5
        assert result["temperature"] == 19.0
        assert result["system_mode"] == HVACMode.HEAT

    def test_target_temp_based_payload(self, mock_bt):
        """TARGET_TEMP_BASED produces payload with calculated setpoint."""
        mock_bt.real_trvs[ENTITY_ID].advanced["calibration"] = (
            CalibrationType.TARGET_TEMP_BASED
        )
        mock_bt.real_trvs[ENTITY_ID].advanced["calibration_mode"] = (
            CalibrationMode.DEFAULT
        )
        mock_bt.real_trvs[ENTITY_ID].current_temperature = 18.0

        with (
            patch(
                "custom_components.better_thermostat.events.trv.calculate_calibration_setpoint",
                return_value=21.0,
            ),
            patch(
                "custom_components.better_thermostat.events.trv.mode_remap",
                return_value=HVACMode.HEAT,
            ),
        ):
            result = convert_outbound_states(mock_bt, ENTITY_ID, HVACMode.HEAT)

        assert result is not None
        assert "local_temperature_calibration" not in result
        assert result["temperature"] == 21.0

    def test_no_calibration_mode_uses_target(self, mock_bt):
        """NO_CALIBRATION mode uses bt_target_temp directly."""
        mock_bt.real_trvs[ENTITY_ID].advanced["calibration"] = (
            CalibrationType.TARGET_TEMP_BASED
        )
        mock_bt.real_trvs[ENTITY_ID].advanced["calibration_mode"] = (
            CalibrationMode.NO_CALIBRATION
        )
        mock_bt.real_trvs[ENTITY_ID].current_temperature = 18.0

        with patch(
            "custom_components.better_thermostat.events.trv.mode_remap",
            return_value=HVACMode.HEAT,
        ):
            result = convert_outbound_states(mock_bt, ENTITY_ID, HVACMode.HEAT)

        assert result is not None
        assert result["temperature"] == mock_bt.bt_target_temp

    def test_none_calibration_type_fallback(self, mock_bt):
        """None calibration type falls back to bt_target_temp without calibration."""
        mock_bt.real_trvs[ENTITY_ID].advanced["calibration"] = None
        mock_bt.real_trvs[ENTITY_ID].current_temperature = 18.0

        with patch(
            "custom_components.better_thermostat.events.trv.mode_remap",
            return_value=HVACMode.HEAT,
        ):
            result = convert_outbound_states(mock_bt, ENTITY_ID, HVACMode.HEAT)

        assert result is not None
        assert result["temperature"] == mock_bt.bt_target_temp
        assert "local_temperature_calibration" not in result

    def test_off_mode_no_system_modes_uses_min_temp(self, mock_bt):
        """When hvac_modes is None → no system mode → OFF uses min_temp."""
        mock_bt.real_trvs[ENTITY_ID].hvac_modes = None
        mock_bt.real_trvs[ENTITY_ID].current_temperature = 18.0

        with (
            patch(
                "custom_components.better_thermostat.events.trv.calculate_calibration_local",
                return_value=0.0,
            ),
            patch(
                "custom_components.better_thermostat.events.trv.mode_remap",
                return_value=HVACMode.OFF,
            ),
        ):
            result = convert_outbound_states(mock_bt, ENTITY_ID, HVACMode.OFF)

        assert result is not None
        assert result["temperature"] == 5.0
        assert result["system_mode"] is None

    def test_no_off_system_mode_flag(self, mock_bt):
        """no_off_system_mode + OFF → min_temp, system_mode=None."""
        mock_bt.real_trvs[ENTITY_ID].advanced["no_off_system_mode"] = True
        mock_bt.real_trvs[ENTITY_ID].current_temperature = 18.0

        with (
            patch(
                "custom_components.better_thermostat.events.trv.calculate_calibration_local",
                return_value=0.0,
            ),
            patch(
                "custom_components.better_thermostat.events.trv.mode_remap",
                return_value=HVACMode.OFF,
            ),
        ):
            result = convert_outbound_states(mock_bt, ENTITY_ID, HVACMode.OFF)

        assert result is not None
        assert result["temperature"] == 5.0
        assert result["system_mode"] is None

    def test_off_mode_not_in_hvac_modes(self, mock_bt):
        """OFF not in hvac_modes → min_temp, system_mode=None."""
        mock_bt.real_trvs[ENTITY_ID].hvac_modes = [HVACMode.HEAT]
        mock_bt.real_trvs[ENTITY_ID].current_temperature = 18.0

        with (
            patch(
                "custom_components.better_thermostat.events.trv.calculate_calibration_local",
                return_value=0.0,
            ),
            patch(
                "custom_components.better_thermostat.events.trv.mode_remap",
                return_value=HVACMode.OFF,
            ),
        ):
            result = convert_outbound_states(mock_bt, ENTITY_ID, HVACMode.OFF)

        assert result is not None
        assert result["temperature"] == 5.0
        assert result["system_mode"] is None

    def test_exception_returns_none(self, mock_bt):
        """Internal exception → None returned."""
        mock_bt.real_trvs[ENTITY_ID].advanced["calibration"] = (
            CalibrationType.LOCAL_BASED
        )

        with (
            patch(
                "custom_components.better_thermostat.events.trv.calculate_calibration_local",
                side_effect=ValueError("test error"),
            ),
            patch(
                "custom_components.better_thermostat.events.trv.mode_remap",
                return_value=HVACMode.HEAT,
            ),
        ):
            result = convert_outbound_states(mock_bt, ENTITY_ID, HVACMode.HEAT)

        assert result is None


# ---------------------------------------------------------------------------
# 6. Grouped-TRV mode adoption (quorum-gated OFF)
# ---------------------------------------------------------------------------

GRP_IDS = ["climate.grp_trv1", "climate.grp_trv2", "climate.grp_trv3"]


def _grp_state(entity_id, state_str, temperature=19.0, current=18.0):
    """Build an HA State for a grouped-TRV test member."""
    return State(
        entity_id,
        state_str,
        attributes={"current_temperature": current, "temperature": temperature},
    )


def _make_group_bt(entity_ids, *, no_off=False, bt_hvac_mode=HVACMode.HEAT):
    """Build a mock BetterThermostat controlling several TRVs.

    Mirrors the single-TRV ``mock_bt`` fixture but with an arbitrary number of
    members so the group-quorum logic can be exercised.
    """
    bt = MagicMock()
    bt.hass = MagicMock()
    bt.device_name = "Grouped Thermostat"
    bt.bt_hvac_mode = bt_hvac_mode
    bt.bt_target_temp = 19.0
    bt.bt_min_temp = 5.0
    bt.bt_max_temp = 30.0
    bt.bt_target_cooltemp = 25.0
    bt.bt_target_temp_step = 0.5
    bt.cur_temp = 18.0
    bt.window_open = False
    bt.tolerance = 0.3
    bt.startup_running = False
    bt.control_queue_task = AsyncMock()
    bt.bt_update_lock = False
    bt.cooler_entity_id = None
    bt.ignore_states = False
    bt.context = MagicMock()
    bt.last_internal_sensor_change = dt_util.now() - timedelta(seconds=60)
    bt.async_write_ha_state = MagicMock()
    bt.all_trvs = [{"advanced": {CONF_HOMEMATICIP: False}} for _ in entity_ids]

    bt.real_trvs = {
        eid: Trv.from_legacy_dict(
            eid,
            {
                "hvac_mode": HVACMode.HEAT,
                "hvac_modes": [HVACMode.OFF, HVACMode.HEAT],
                "min_temp": 5.0,
                "max_temp": 30.0,
                "current_temperature": 18.0,
                "temperature": 19.0,
                "last_temperature": 19.0,
                "last_hvac_mode": "heat",
                "target_temp_received": True,
                "system_mode_received": True,
                "calibration_received": True,
                "calibration": 1,
                "last_calibration": 0.0,
                "ignore_trv_states": False,
                "model": "SomeModel",
                "model_quirks": None,
                "hvac_action": "heating",
                "valve_position": 50,
                "advanced": {
                    "calibration": CalibrationType.LOCAL_BASED,
                    "calibration_mode": CalibrationMode.DEFAULT,
                    "no_off_system_mode": no_off,
                    "heat_auto_swapped": False,
                    "child_lock": False,
                },
            },
        )
        for eid in entity_ids
    }
    return bt


def _install_states(bt, states):
    """Route ``bt.hass.states.get`` to a per-entity mapping."""
    bt.hass.states.get.side_effect = states.get


class TestGroupedModeAdoption:
    """Quorum-gated OFF adoption for BT instances with several TRVs."""

    @pytest.mark.asyncio
    async def test_group_off_not_adopted_when_others_heat(self):
        """One valve reporting off must not switch a heating group off."""
        trigger, other1, other2 = GRP_IDS
        bt = _make_group_bt(GRP_IDS)
        _install_states(
            bt,
            {
                trigger: _grp_state(trigger, "off"),
                other1: _grp_state(other1, "heat"),
                other2: _grp_state(other2, "heat"),
            },
        )
        bt.real_trvs[trigger].hvac_mode = "heat"
        bt.real_trvs[trigger].last_hvac_mode = "heat"

        event = _make_event(
            bt,
            new_state=_grp_state(trigger, "off"),
            old_state=_grp_state(trigger, "heat"),
            entity_id=trigger,
        )
        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.OFF,
        ):
            await trigger_trv_change(bt, event)

        assert bt.bt_hvac_mode == HVACMode.HEAT

    @pytest.mark.asyncio
    async def test_group_off_adopted_when_all_off(self):
        """The group switches off only when every member reports off."""
        trigger = GRP_IDS[0]
        bt = _make_group_bt(GRP_IDS)
        _install_states(bt, {eid: _grp_state(eid, "off") for eid in GRP_IDS})
        bt.real_trvs[trigger].hvac_mode = "heat"
        bt.real_trvs[trigger].last_hvac_mode = "heat"

        event = _make_event(
            bt,
            new_state=_grp_state(trigger, "off"),
            old_state=_grp_state(trigger, "heat"),
            entity_id=trigger,
        )
        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.OFF,
        ):
            await trigger_trv_change(bt, event)

        assert bt.bt_hvac_mode == HVACMode.OFF

    @pytest.mark.asyncio
    async def test_group_on_adopted_from_single_valve(self):
        """A single valve turning on still switches the whole group on."""
        trigger, other1, other2 = GRP_IDS
        bt = _make_group_bt(GRP_IDS, bt_hvac_mode=HVACMode.OFF)
        _install_states(
            bt,
            {
                trigger: _grp_state(trigger, "heat"),
                other1: _grp_state(other1, "off"),
                other2: _grp_state(other2, "off"),
            },
        )
        bt.real_trvs[trigger].hvac_mode = "off"
        bt.real_trvs[trigger].last_hvac_mode = "off"

        event = _make_event(
            bt,
            new_state=_grp_state(trigger, "heat"),
            old_state=_grp_state(trigger, "off"),
            entity_id=trigger,
        )
        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(bt, event)

        assert bt.bt_hvac_mode == HVACMode.HEAT

    @pytest.mark.asyncio
    async def test_single_trv_off_still_adopted(self):
        """Single-TRV instances keep the historical single-valve behavior."""
        only = "climate.solo_trv"
        bt = _make_group_bt([only])
        _install_states(bt, {only: _grp_state(only, "off")})
        bt.real_trvs[only].hvac_mode = "heat"
        bt.real_trvs[only].last_hvac_mode = "heat"

        event = _make_event(
            bt,
            new_state=_grp_state(only, "off"),
            old_state=_grp_state(only, "heat"),
            entity_id=only,
        )
        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.OFF,
        ):
            await trigger_trv_change(bt, event)

        assert bt.bt_hvac_mode == HVACMode.OFF


class TestGroupedNoOffAdoption:
    """Quorum-gated OFF for no_off_system_mode groups (min_temp means off)."""

    _IDS = ["climate.hm_trv1", "climate.hm_trv2"]

    @pytest.mark.asyncio
    async def test_no_off_group_not_off_when_other_above_min(self):
        """One no_off valve at min_temp must not switch the group off."""
        trigger, other = self._IDS
        bt = _make_group_bt(self._IDS, no_off=True, bt_hvac_mode=HVACMode.HEAT)
        _install_states(
            bt,
            {
                trigger: _grp_state(trigger, "heat", temperature=5.0),
                other: _grp_state(other, "heat", temperature=20.0),
            },
        )
        bt.real_trvs[trigger].hvac_mode = "heat"  # keep HVAC-mode block a no-op

        event = _make_event(
            bt,
            new_state=_grp_state(trigger, "heat", temperature=5.0),
            old_state=_grp_state(trigger, "heat", temperature=19.0),
            entity_id=trigger,
        )
        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=None,
        ):
            await trigger_trv_change(bt, event)

        assert bt.bt_hvac_mode == HVACMode.HEAT

    @pytest.mark.asyncio
    async def test_no_off_group_off_when_all_at_min(self):
        """The group switches off when every no_off member is at min_temp."""
        trigger, other = self._IDS
        bt = _make_group_bt(self._IDS, no_off=True, bt_hvac_mode=HVACMode.HEAT)
        _install_states(
            bt,
            {
                trigger: _grp_state(trigger, "heat", temperature=5.0),
                other: _grp_state(other, "heat", temperature=5.0),
            },
        )
        bt.real_trvs[trigger].hvac_mode = "heat"

        event = _make_event(
            bt,
            new_state=_grp_state(trigger, "heat", temperature=5.0),
            old_state=_grp_state(trigger, "heat", temperature=19.0),
            entity_id=trigger,
        )
        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=None,
        ):
            await trigger_trv_change(bt, event)

        assert bt.bt_hvac_mode == HVACMode.OFF

    @pytest.mark.asyncio
    async def test_no_off_below_bt_min_detected_via_raw_setpoint(self, mock_bt):
        """A no_off report below bt_min_temp is still OFF (compared pre-clamp).

        The setpoint is clamped up to bt_min_temp for BT state, but OFF
        detection must compare the device's raw report against its own
        min_temp; a device whose min_temp is below bt_min_temp is not missed.
        """
        mock_bt.bt_min_temp = 5.0
        mock_bt.real_trvs[ENTITY_ID].min_temp = 4.0
        mock_bt.real_trvs[ENTITY_ID].advanced["no_off_system_mode"] = True
        mock_bt.real_trvs[ENTITY_ID].hvac_mode = "heat"

        new_state = _make_state(state_str="heat", attributes={"temperature": 4.0})
        mock_bt.hass.states.get.return_value = new_state

        event = _make_event(
            mock_bt,
            new_state=new_state,
            old_state=_make_state(state_str="heat", attributes={"temperature": 19.0}),
        )
        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=None,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.bt_hvac_mode == HVACMode.OFF
