"""Tests for events/trv.py – TRV event handlers and conversion helpers.

Covers guard clauses, internal temperature changes, HVAC action/valve caching,
mode synchronisation, target-temperature adoption, control-queue triggering,
and the convert_inbound_states / convert_outbound_states helpers.
"""

from datetime import timedelta
import logging
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import UnitOfTemperature
from homeassistant.core import State
from homeassistant.util import dt as dt_util
import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.events.trv import (
    convert_inbound_states,
    convert_outbound_states,
    trigger_trv_change,
)
from custom_components.better_thermostat.trv import PendingSetpoint, Trv
from custom_components.better_thermostat.utils.const import (
    CONF_HOMEMATICIP,
    CalibrationMode,
    CalibrationType,
)
from custom_components.better_thermostat.utils.helpers import mode_remap

ENTITY_ID = "climate.test_trv"
PEER_ID = "climate.test_trv_peer"


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
    bt.hvac_mode = HVACMode.HEAT
    # A room without a cooler spells its heat demand HEAT; ``_bind_cooler_hvac_mode``
    # replaces this with HEAT_COOL for a room that has one.
    bt.map_on_hvac_mode = HVACMode.HEAT
    bt.bt_target_temp = 19.0
    bt.bt_min_temp = 5.0
    bt.bt_max_temp = 30.0
    bt.bt_target_cooltemp = 25.0
    bt.bt_target_temp_step = 0.5
    bt.cur_temp = 18.0
    bt.window_open = False
    bt.contact_open = False
    bt.tolerance = 0.3
    bt.startup_running = False
    bt.control_queue_task = AsyncMock()
    bt.bt_update_lock = False
    bt.cooler_entity_id = None
    bt.ignore_states = False
    bt.context = MagicMock()  # unique context so != event.context
    bt.async_write_ha_state = MagicMock()
    bt._enforce_cool_above_heat = lambda **kwargs: (
        BetterThermostat._enforce_cool_above_heat(bt, **kwargs)
    )
    bt._clamp_inbound_heat_target = lambda v: (
        BetterThermostat._clamp_inbound_heat_target(bt, v)
    )

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


def _add_homematicip_peer(bt):
    """Put a second, HomematicIP-flagged valve into the same room.

    Returns the state the peer reports, so a caller can route
    ``hass.states.get`` to the right state per entity.
    """
    bt.real_trvs[PEER_ID] = Trv.from_legacy_dict(
        PEER_ID,
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
                CONF_HOMEMATICIP: True,
            },
        },
    )
    bt.all_trvs = [
        {"advanced": {CONF_HOMEMATICIP: False}},
        {"advanced": {CONF_HOMEMATICIP: True}},
    ]
    peer_state = State(
        PEER_ID, "heat", attributes={"current_temperature": 20.0, "temperature": 19.0}
    )

    def _state_for(entity_id):
        if entity_id == PEER_ID:
            return peer_state
        return _make_state(attributes={"current_temperature": 20.0})

    bt.hass.states.get.side_effect = _state_for
    return peer_state


def _bind_cooler_hvac_mode(bt):
    """Let ``bt.hvac_mode`` follow the real property of a cooler setup.

    With a cooler configured the mode list carries HEAT_COOL in place of HEAT,
    so the property reports HEAT_COOL for a ``bt_hvac_mode`` of HEAT. That
    mapping decides whether the ordering check between the two targets applies,
    so a handler that changes ``bt_hvac_mode`` needs the derived value, not a
    fixed one.
    """
    bt._hvac_list = [HVACMode.OFF, HVACMode.HEAT_COOL]
    bt.map_on_hvac_mode = HVACMode.HEAT_COOL
    type(bt).hvac_mode = BetterThermostat.hvac_mode


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
        mock_bt.real_trvs[ENTITY_ID].last_internal_sensor_change = dt_util.now()
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
        """Changes within 5 s of the TRV's last internal sensor change are skipped."""
        mock_bt.real_trvs[ENTITY_ID].last_internal_sensor_change = dt_util.now() - (
            timedelta(seconds=2)
        )
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
        """A HomematicIP TRV guards its own readings with 600 s instead of 5 s."""
        mock_bt.real_trvs[ENTITY_ID].advanced[CONF_HOMEMATICIP] = True
        mock_bt.real_trvs[ENTITY_ID].last_internal_sensor_change = dt_util.now() - (
            timedelta(seconds=30)
        )
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
    async def test_homematicip_peer_does_not_hold_back_the_other_trv(self, mock_bt):
        """A HomematicIP valve does not stretch its room mates' debounce window.

        The 600 s window belongs to the HomematicIP radio, so the Zigbee valve
        of the same room keeps the 5 s window: its reading is taken 30 s after
        its own last one, while the HomematicIP valve reported a moment ago.
        """
        _add_homematicip_peer(mock_bt)
        mock_bt.real_trvs[PEER_ID].last_internal_sensor_change = dt_util.now()
        mock_bt.real_trvs[ENTITY_ID].last_internal_sensor_change = dt_util.now() - (
            timedelta(seconds=30)
        )
        mock_bt.real_trvs[ENTITY_ID].current_temperature = 18.0
        mock_bt.real_trvs[ENTITY_ID].calibration_received = True
        mock_bt.real_trvs[ENTITY_ID].calibration = 1

        trv_state = _make_state(attributes={"current_temperature": 20.0})
        event = _make_event(mock_bt, new_state=trv_state, old_state=trv_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.real_trvs[ENTITY_ID].current_temperature == 20.0

    @pytest.mark.asyncio
    async def test_homematicip_trv_keeps_its_own_600s_window(self, mock_bt):
        """The HomematicIP valve of a mixed room still waits out its 600 s.

        A room mate on another radio does not shorten the duty-cycle window,
        just as the window does not lengthen the room mate's.
        """
        peer_state = _add_homematicip_peer(mock_bt)
        mock_bt.real_trvs[PEER_ID].last_internal_sensor_change = dt_util.now() - (
            timedelta(seconds=30)
        )
        mock_bt.real_trvs[PEER_ID].current_temperature = 18.0
        mock_bt.real_trvs[ENTITY_ID].last_internal_sensor_change = dt_util.now() - (
            timedelta(seconds=30)
        )
        event = _make_event(
            mock_bt, new_state=peer_state, old_state=peer_state, entity_id=PEER_ID
        )

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.real_trvs[PEER_ID].current_temperature == 18.0

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


class TestHvacModesCache:
    """Tests for the cached list of HVAC modes the device offers."""

    @pytest.mark.asyncio
    async def test_reported_modes_replace_the_cached_list(self, mock_bt):
        """The offered modes are taken from the TRV state on every event."""
        mock_bt.real_trvs[ENTITY_ID].hvac_modes = [HVACMode.OFF, HVACMode.COOL]
        trv_state = _make_state(attributes={"hvac_modes": ["off", "heat"]})
        mock_bt.hass.states.get.return_value = trv_state

        event = _make_event(mock_bt, new_state=trv_state, old_state=trv_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.real_trvs[ENTITY_ID].hvac_modes == ["off", "heat"]

    @pytest.mark.asyncio
    async def test_absent_modes_attribute_keeps_the_cached_list(self, mock_bt):
        """An event without the attribute does not wipe the known capabilities."""
        trv_state = _make_state()
        mock_bt.hass.states.get.return_value = trv_state

        event = _make_event(mock_bt, new_state=trv_state, old_state=trv_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.real_trvs[ENTITY_ID].hvac_modes == [HVACMode.OFF, HVACMode.HEAT]

    @pytest.mark.asyncio
    async def test_empty_modes_attribute_keeps_the_cached_list(self, mock_bt):
        """An empty list is treated as "nothing reported", not as "no modes"."""
        trv_state = _make_state(attributes={"hvac_modes": []})
        mock_bt.hass.states.get.return_value = trv_state

        event = _make_event(mock_bt, new_state=trv_state, old_state=trv_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.real_trvs[ENTITY_ID].hvac_modes == [HVACMode.OFF, HVACMode.HEAT]

    @pytest.mark.asyncio
    async def test_changed_mode_list_clears_the_annunciated_modes(self, mock_bt):
        """A genuine capability change lets the unsupported-mode error fire again."""
        mock_bt.real_trvs[ENTITY_ID].hvac_modes = [HVACMode.OFF, HVACMode.COOL]
        mock_bt.real_trvs[ENTITY_ID].unsupported_modes_logged = {"heat"}
        trv_state = _make_state(attributes={"hvac_modes": ["off", "heat"]})
        mock_bt.hass.states.get.return_value = trv_state

        event = _make_event(mock_bt, new_state=trv_state, old_state=trv_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.real_trvs[ENTITY_ID].unsupported_modes_logged == set()

    @pytest.mark.asyncio
    async def test_unchanged_mode_list_keeps_the_annunciated_modes(self, mock_bt):
        """Repeating the same list keeps the error suppressed across cycles."""
        mock_bt.real_trvs[ENTITY_ID].unsupported_modes_logged = {"heat_cool"}
        trv_state = _make_state(attributes={"hvac_modes": ["off", "heat"]})
        mock_bt.hass.states.get.return_value = trv_state

        event = _make_event(mock_bt, new_state=trv_state, old_state=trv_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.real_trvs[ENTITY_ID].unsupported_modes_logged == {"heat_cool"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "republished",
        [
            ["cool", "off", "auto"],
            [HVACMode.AUTO, HVACMode.COOL, HVACMode.OFF],
            ["HVACMode.AUTO", "HVACMode.COOL", "HVACMode.OFF"],
            ["AUTO", "Cool", "OFF"],
        ],
        ids=["reordered", "enum_members", "prefixed", "mixed_case"],
    )
    async def test_same_capabilities_keep_the_annunciated_modes(
        self, mock_bt, republished
    ):
        """The same offered modes in another spelling are not a change."""
        mock_bt.real_trvs[ENTITY_ID].hvac_modes = ["auto", "cool", "off"]
        mock_bt.real_trvs[ENTITY_ID].unsupported_modes_logged = {"heat_cool"}
        trv_state = _make_state(attributes={"hvac_modes": republished})
        mock_bt.hass.states.get.return_value = trv_state

        event = _make_event(mock_bt, new_state=trv_state, old_state=trv_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.real_trvs[ENTITY_ID].unsupported_modes_logged == {"heat_cool"}

    @pytest.mark.asyncio
    async def test_reordered_republication_does_not_repeat_the_error(
        self, mock_bt, caplog
    ):
        """A flapping mode list keeps the annunciation at once per mode."""
        helpers_logger = "custom_components.better_thermostat.utils.helpers"
        mock_bt.real_trvs[ENTITY_ID].hvac_modes = ["auto", "cool", "off"]

        def _errors():
            return [
                record
                for record in caplog.records
                if "does not offer HVAC mode" in record.getMessage()
            ]

        trv_state = _make_state(attributes={"hvac_modes": ["cool", "off", "auto"]})
        mock_bt.hass.states.get.return_value = trv_state
        event = _make_event(mock_bt, new_state=trv_state, old_state=trv_state)

        with caplog.at_level(logging.ERROR, logger=helpers_logger):
            assert (
                mode_remap(mock_bt, ENTITY_ID, HVACMode.HEAT_COOL, inbound=False)
                is None
            )
            assert len(_errors()) == 1

            with patch(
                "custom_components.better_thermostat.events.trv.convert_inbound_states",
                return_value=HVACMode.HEAT,
            ):
                await trigger_trv_change(mock_bt, event)

            assert (
                mode_remap(mock_bt, ENTITY_ID, HVACMode.HEAT_COOL, inbound=False)
                is None
            )
            assert len(_errors()) == 1

    @pytest.mark.asyncio
    async def test_a_capability_change_decodes_the_state_that_carried_it(self, mock_bt):
        """A device switching to HEAT_COOL is decoded against its new list.

        The reported state belongs to the capabilities reported alongside it,
        so the mode the entity takes over is the one the new list translates
        to and not the one the previous list would have produced.
        """
        mock_bt.bt_hvac_mode = HVACMode.OFF
        trv_state = _make_state(
            state_str="heat_cool",
            attributes={"hvac_modes": [HVACMode.OFF, HVACMode.HEAT_COOL]},
        )
        mock_bt.hass.states.get.return_value = trv_state

        event = _make_event(mock_bt, new_state=trv_state, old_state=_make_state())

        await trigger_trv_change(mock_bt, event)

        assert mock_bt.real_trvs[ENTITY_ID].hvac_modes == [
            HVACMode.OFF,
            HVACMode.HEAT_COOL,
        ]
        assert mock_bt.bt_hvac_mode == HVACMode.HEAT

    @pytest.mark.asyncio
    async def test_modes_cached_in_the_device_spelling_still_translate(self, mock_bt):
        """A mode list reported as ``HVACMode.HEAT`` reaches the translation."""
        trv_state = _make_state(
            attributes={"hvac_modes": ["HVACMode.OFF", "HVACMode.HEAT"]}
        )
        mock_bt.hass.states.get.return_value = trv_state

        event = _make_event(mock_bt, new_state=trv_state, old_state=trv_state)

        await trigger_trv_change(mock_bt, event)

        assert mock_bt.real_trvs[ENTITY_ID].hvac_modes == [
            "HVACMode.OFF",
            "HVACMode.HEAT",
        ]
        assert (
            mode_remap(mock_bt, ENTITY_ID, HVACMode.HEAT_COOL, inbound=False)
            == HVACMode.HEAT
        )


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

    @pytest.mark.parametrize(
        ("previous", "reported", "commanded", "cycle"),
        [
            ("heat", "off", "heat", True),
            ("off", "heat", "off", True),
            ("heat", "cool", "heat", True),
            ("heat", "dry", "heat", True),
            ("heat", "off", "off", False),
            ("off", "off", "heat", False),
            ("cool", "cool", "heat", False),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_mode_switched_at_a_locked_device_requests_a_cycle(
        self, mock_bt, previous, reported, commanded, cycle
    ):
        """A locked device leaving the commanded mode is driven back at once.

        That holds for any mode the device reports, including one the room
        never adopts. The device's own report of the commanded mode landing,
        and a report that repeats the mode it held, request nothing.
        """
        trv = mock_bt.real_trvs[ENTITY_ID]
        trv.advanced["child_lock"] = True
        trv.hvac_mode = previous
        trv.last_hvac_mode = commanded
        mock_bt.bt_hvac_mode = HVACMode(commanded)
        trv_state = _make_state(state_str=reported)
        mock_bt.hass.states.get.return_value = trv_state
        event = _make_event(
            mock_bt, new_state=trv_state, old_state=_make_state(state_str=previous)
        )

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode(reported) if reported in ("heat", "off") else None,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.bt_hvac_mode == HVACMode(commanded)
        assert mock_bt.control_queue_task.put.await_count == int(cycle)

    @pytest.mark.parametrize(
        ("previous", "reported", "cycle"),
        [(19.0, 22.0, True), (19.0, 17.5, True), (19.0, 19.0, False)],
    )
    @pytest.mark.asyncio
    async def test_a_setpoint_turned_at_a_locked_device_requests_a_cycle(
        self, mock_bt, previous, reported, cycle
    ):
        """A turn at a locked device is driven back at once, not adopted.

        A report that repeats the setpoint the device held requests nothing,
        so a device that keeps a turned setpoint is not written on every
        report it sends.
        """
        trv = mock_bt.real_trvs[ENTITY_ID]
        trv.advanced["child_lock"] = True
        trv.last_temperature = 21.0
        mock_bt.bt_target_temp = 21.0
        new_state = _make_state(attributes={"temperature": reported})
        mock_bt.hass.states.get.return_value = new_state
        event = _make_event(
            mock_bt,
            new_state=new_state,
            old_state=_make_state(attributes={"temperature": previous}),
        )

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.bt_target_temp == 21.0
        assert mock_bt.control_queue_task.put.await_count == int(cycle)

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

    @staticmethod
    def _off_landing_after_withdrawal(mock_bt, *, now: float):
        """Let the room take back an off command that the device applies now.

        The room is heating again and its watchdog has settled; the off
        command went out earlier and was withdrawn with a deadline of 360 s
        from ``t = 0``.
        """
        trv = mock_bt.real_trvs[ENTITY_ID]
        trv.hvac_mode = "heat"
        trv.last_hvac_mode = "heat"
        trv.system_mode_received = True
        trv.withdrawn_hvac_mode = "off"
        trv.withdrawn_hvac_mode_until = 360.0
        trv_state = _make_state(
            state_str="off",
            attributes={"current_temperature": 18.0, "temperature": 19.0},
        )
        mock_bt.hass.states.get.return_value = trv_state
        event = _make_event(
            mock_bt, new_state=trv_state, old_state=_make_state(state_str="heat")
        )
        return patch(
            "custom_components.better_thermostat.events.trv.monotonic", return_value=now
        ), event

    @pytest.mark.asyncio
    async def test_a_withdrawn_mode_command_landing_late_does_not_switch_the_room(
        self, mock_bt
    ):
        """A slow device applying a mode command the room has taken back is no press."""
        clock, event = self._off_landing_after_withdrawal(mock_bt, now=100.0)

        with (
            clock,
            patch(
                "custom_components.better_thermostat.events.trv.convert_inbound_states",
                return_value=HVACMode.OFF,
            ),
        ):
            await trigger_trv_change(mock_bt, event)

        trv = mock_bt.real_trvs[ENTITY_ID]
        assert mock_bt.bt_hvac_mode == HVACMode.HEAT
        assert trv.hvac_mode == "off"
        assert trv.withdrawn_hvac_mode is None
        assert trv.withdrawn_hvac_mode_until is None

    @pytest.mark.asyncio
    async def test_a_press_after_the_withdrawn_command_had_its_time_is_adopted(
        self, mock_bt
    ):
        """Once the confirmation window has passed, an off at the device is the user's."""
        clock, event = self._off_landing_after_withdrawal(mock_bt, now=361.0)

        with (
            clock,
            patch(
                "custom_components.better_thermostat.events.trv.convert_inbound_states",
                return_value=HVACMode.OFF,
            ),
        ):
            await trigger_trv_change(mock_bt, event)

        trv = mock_bt.real_trvs[ENTITY_ID]
        assert mock_bt.bt_hvac_mode == HVACMode.OFF
        assert trv.withdrawn_hvac_mode is None
        assert trv.withdrawn_hvac_mode_until is None

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

    @pytest.mark.parametrize(
        "bt_hvac_mode", [HVACMode.HEAT, HVACMode.OFF], ids=["heating", "off"]
    )
    @pytest.mark.asyncio
    async def test_an_unswapped_device_reporting_auto_leaves_the_room_mode(
        self, mock_bt, bt_hvac_mode
    ):
        """A reported AUTO without the swap option changes neither room mode.

        The report is ambiguous: a heating room must not be switched off by
        it, and a room that is off must not be switched on.
        """
        mock_bt.bt_hvac_mode = bt_hvac_mode
        trv = mock_bt.real_trvs[ENTITY_ID]
        trv.hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.AUTO]
        trv.hvac_mode = "heat"
        trv.system_mode_received = True
        trv.last_hvac_mode = "heat"
        trv.advanced["child_lock"] = False
        trv_state = _make_state(
            state_str="auto",
            attributes={
                "current_temperature": 18.0,
                "temperature": 19.0,
                "hvac_modes": ["off", "heat", "auto"],
            },
        )
        mock_bt.hass.states.get.return_value = trv_state
        event = _make_event(
            mock_bt, new_state=trv_state, old_state=_make_state(state_str="heat")
        )

        await trigger_trv_change(mock_bt, event)

        assert mock_bt.bt_hvac_mode == bt_hvac_mode

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
    async def test_a_missing_child_lock_flag_counts_as_unlocked(self, mock_bt):
        """A TRV whose config carries no child lock flag is not locked.

        A config entry written before the option existed has no flag at all,
        and no migration adds one, so ``advanced`` is missing the key rather
        than holding ``False``. Everything else that reads the flag — the
        mode cache in this same handler, the setpoint adoption below it, the
        child lock switch — takes that as unlocked, so a dial turned on such
        a device has to reach Better Thermostat like on any other.
        """
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
        assert mock_bt.bt_hvac_mode == HVACMode.OFF

    @pytest.mark.asyncio
    async def test_a_missing_and_a_false_child_lock_flag_behave_alike(self, mock_bt):
        """The two ways of not being locked are one behaviour, not two.

        The guard is asked three times in this handler, and a flag that is
        absent rather than ``False`` used to answer one of them differently:
        the device's new mode was recorded but never adopted, while a
        setpoint turned on the same device was. What that looks like from
        the outside is a dial that works for temperature and not for mode.
        """
        outcomes = []
        for flag in ({}, {"child_lock": False}):
            trv = mock_bt.real_trvs[ENTITY_ID]
            trv.advanced.pop("child_lock", None)
            trv.advanced.update(flag)
            trv.hvac_mode = "heat"
            trv.system_mode_received = True
            trv.last_hvac_mode = "heat"
            mock_bt.bt_hvac_mode = HVACMode.HEAT
            trv_state = _make_state(
                state_str="off",
                attributes={"current_temperature": 18.0, "temperature": 19.0},
            )
            mock_bt.hass.states.get.return_value = trv_state

            event = _make_event(
                mock_bt, new_state=trv_state, old_state=_make_state(state_str="heat")
            )
            with patch(
                "custom_components.better_thermostat.events.trv.convert_inbound_states",
                return_value=HVACMode.OFF,
            ):
                await trigger_trv_change(mock_bt, event)
            outcomes.append((trv.hvac_mode, mock_bt.bt_hvac_mode))

        assert outcomes[0] == outcomes[1]


class TestKnobOperatedMode:
    """A user turning the device off and on again at its own knob."""

    @staticmethod
    async def _report(bt, mode):
        """Route one reported device mode through the event handler.

        The remap is not patched out here: the decoding of the device's own
        spelling is what these tests are about.
        """
        previous = bt.real_trvs[ENTITY_ID].hvac_mode
        trv_state = _make_state(state_str=mode)
        bt.hass.states.get.return_value = trv_state
        event = _make_event(
            bt, new_state=trv_state, old_state=_make_state(state_str=previous)
        )
        await trigger_trv_change(bt, event)

    @staticmethod
    def _stamp_written_mode(bt, mode):
        """Record the mode a control cycle put on the wire.

        ``last_hvac_mode`` is written by the control cycle, and the handler
        reads it to tell a device echoing Better Thermostat's own command
        from a device a user has just operated.
        """
        bt.real_trvs[ENTITY_ID].last_hvac_mode = mode

    @pytest.mark.asyncio
    async def test_a_heat_only_device_switched_off_and_on_ends_on_heat(self, mock_bt):
        """Both directions of the knob reach the cache and the entity.

        The device offers off and heat only, the overwhelmingly common shape.
        """
        trv = mock_bt.real_trvs[ENTITY_ID]
        assert trv.hvac_modes == [HVACMode.OFF, HVACMode.HEAT]
        trv.hvac_mode = "heat"
        self._stamp_written_mode(mock_bt, "heat")

        await self._report(mock_bt, "off")

        assert trv.hvac_mode == "off"
        assert mock_bt.bt_hvac_mode == HVACMode.OFF

        self._stamp_written_mode(mock_bt, "off")

        await self._report(mock_bt, "heat")

        assert trv.hvac_mode == "heat"
        assert mock_bt.bt_hvac_mode == HVACMode.HEAT

    @pytest.mark.asyncio
    async def test_a_room_with_a_cooler_holds_the_knob_press_as_on(self, mock_bt):
        """A knob press into heat holds the room on and publishes HEAT_COOL.

        The room holds its intent as HEAT whatever device or spelling set it;
        the entity publishes it in the spelling its own list carries.
        """
        _bind_cooler_hvac_mode(mock_bt)
        mock_bt.cooler_entity_id = "climate.cooler"
        trv = mock_bt.real_trvs[ENTITY_ID]
        trv.hvac_mode = "heat"
        self._stamp_written_mode(mock_bt, "heat")

        await self._report(mock_bt, "off")

        assert mock_bt.bt_hvac_mode == HVACMode.OFF
        assert mock_bt.hvac_mode == HVACMode.OFF

        self._stamp_written_mode(mock_bt, "off")

        await self._report(mock_bt, "heat")

        assert trv.hvac_mode == "heat"
        assert mock_bt.bt_hvac_mode == HVACMode.HEAT
        assert mock_bt.hvac_mode == HVACMode.HEAT_COOL


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

    @pytest.mark.parametrize(
        ("no_off", "bt_hvac_mode"),
        [
            pytest.param(False, HVACMode.HEAT, id="heating"),
            pytest.param(True, HVACMode.OFF, id="no_off_device_room_off"),
        ],
    )
    @pytest.mark.asyncio
    async def test_an_ignored_auto_report_adopts_no_setpoint(
        self, mock_bt, no_off, bt_hvac_mode
    ):
        """An AUTO report without the swap option is ignored as a whole.

        The setpoint such a device shows in AUTO is its own schedule's, so it
        changes neither the room's target nor, on a no_off device, its mode.
        """
        mock_bt.bt_hvac_mode = bt_hvac_mode
        trv = mock_bt.real_trvs[ENTITY_ID]
        trv.hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.AUTO]
        trv.last_temperature = 19.0
        trv.advanced["no_off_system_mode"] = no_off
        attributes = {
            "current_temperature": 18.0,
            "temperature": 16.0,
            "hvac_modes": ["off", "heat", "auto"],
        }
        new_state = _make_state(state_str="auto", attributes=attributes)
        mock_bt.hass.states.get.return_value = _make_state(
            state_str="auto", attributes=attributes
        )
        old_state = _make_state(
            state_str="heat",
            attributes={"temperature": 19.0, "current_temperature": 18.0},
        )
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_trv_change(mock_bt, event)

        assert mock_bt.bt_target_temp == 19.0
        assert mock_bt.bt_hvac_mode == bt_hvac_mode

    @pytest.mark.parametrize(
        ("event_mode", "registry_mode", "expected_target"),
        [
            pytest.param("auto", "heat", 19.0, id="auto_event_heat_registry"),
            pytest.param("heat", "auto", 16.0, id="heat_event_auto_registry"),
        ],
    )
    @pytest.mark.asyncio
    async def test_the_report_carrying_the_setpoint_decides_whether_it_is_ignored(
        self, mock_bt, event_mode, registry_mode, expected_target
    ):
        """The mode of the event whose setpoint is read decides, not the registry.

        A queued event can be handled after the device has already reported
        again, so the registry may hold a different mode than the event.
        """
        trv = mock_bt.real_trvs[ENTITY_ID]
        trv.hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.AUTO]
        trv.last_temperature = 19.0
        attributes = {
            "current_temperature": 18.0,
            "temperature": 16.0,
            "hvac_modes": ["off", "heat", "auto"],
        }
        mock_bt.hass.states.get.return_value = _make_state(
            state_str=registry_mode, attributes=attributes
        )
        old_state = _make_state(
            state_str="heat",
            attributes={"temperature": 19.0, "current_temperature": 18.0},
        )
        event = _make_event(
            mock_bt,
            new_state=_make_state(state_str=event_mode, attributes=attributes),
            old_state=old_state,
        )

        await trigger_trv_change(mock_bt, event)

        assert mock_bt.bt_target_temp == expected_target

    @pytest.mark.asyncio
    async def test_a_swapped_device_reporting_auto_adopts_its_setpoint(self, mock_bt):
        """On a heat auto swapped device AUTO is heating, so its setpoint counts.

        A knob turn on such a device reports AUTO with the new setpoint, and
        that setpoint becomes the room's target like any heating report's.
        """
        mock_bt.bt_hvac_mode = HVACMode.HEAT
        trv = mock_bt.real_trvs[ENTITY_ID]
        trv.hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.AUTO]
        trv.last_temperature = 19.0
        trv.advanced["heat_auto_swapped"] = True
        attributes = {
            "current_temperature": 18.0,
            "temperature": 16.0,
            "hvac_modes": ["off", "heat", "auto"],
        }
        new_state = _make_state(state_str="auto", attributes=attributes)
        mock_bt.hass.states.get.return_value = _make_state(
            state_str="auto", attributes=attributes
        )
        old_state = _make_state(
            state_str="auto",
            attributes={"temperature": 19.0, "current_temperature": 18.0},
        )
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        await trigger_trv_change(mock_bt, event)

        assert mock_bt.bt_target_temp == 16.0
        assert mock_bt.bt_hvac_mode == HVACMode.HEAT

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
    async def test_echo_above_range_logs_the_reported_setpoint(self, mock_bt, caplog):
        """The debug log names what the TRV reported, not BT's clamped view.

        A calibration setpoint above ``bt_max_temp`` comes back verbatim from
        the TRV. Logging only the clamped value would read as if BT had capped
        the setpoint it sent at its own maximum.
        """
        old_state = _make_state(
            attributes={"temperature": 19.0, "current_temperature": 18.0}
        )
        new_state = _make_state(
            attributes={"temperature": 33.5, "current_temperature": 18.0}
        )
        mock_bt.hass.states.get.return_value = new_state
        mock_bt.real_trvs[ENTITY_ID].last_temperature = 33.5
        caplog.set_level(logging.DEBUG)

        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.bt_target_temp == 19.0
        assert "setpoint change 19.0 -> 33.5 (clamped to 30.0) NOT adopted" in (
            caplog.text
        )
        assert "_new_heating_setpoint: 33.5 (clamped to 30.0)" in caplog.text

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
    async def test_parked_no_off_valve_keeps_the_heating_target(self, mock_bt):
        """A valve resting on its own minimum republishes BT's own write.

        BT parks a no_off valve on the device minimum while it is OFF and
        records that write in ``last_temperature``. The report sits below
        ``bt_min_temp``, so the clamp lifts it onto the configured minimum and
        only the reported value still identifies the write. The ordered pair
        the user configured has to survive the report unchanged.
        """
        mock_bt.bt_hvac_mode = HVACMode.OFF
        mock_bt.hvac_mode = HVACMode.OFF
        mock_bt.bt_min_temp = 20.0
        mock_bt.bt_target_temp = 21.0
        mock_bt.bt_target_cooltemp = 24.0
        mock_bt.cooler_entity_id = "climate.test_cooler"
        trv = mock_bt.real_trvs[ENTITY_ID]
        trv.advanced["no_off_system_mode"] = True
        trv.last_temperature = 5.0
        old_state = _make_state(
            attributes={"temperature": 5.0, "current_temperature": 18.0}
        )
        new_state = _make_state(
            attributes={"temperature": 5.0, "current_temperature": 18.4}
        )
        mock_bt.hass.states.get.return_value = new_state

        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.bt_target_temp == 21.0
        assert mock_bt.bt_target_cooltemp == 24.0
        assert mock_bt.bt_hvac_mode == HVACMode.OFF

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
        mock_bt.contact_open = True
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
    async def test_setpoint_falls_back_when_temperature_is_empty(self, mock_bt):
        """An empty 'temperature' does not hide 'target_temp_low'.

        A TRV running in range mode publishes the key it does not drive as
        None, so the fallback has to survive a present-but-empty attribute.
        """
        old_state = State(
            ENTITY_ID,
            "heat",
            attributes={
                "temperature": None,
                "target_temp_low": 19.0,
                "current_temperature": 18.0,
            },
        )
        new_state = State(
            ENTITY_ID,
            "heat",
            attributes={
                "temperature": None,
                "target_temp_low": 22.0,
                "current_temperature": 18.0,
            },
        )
        mock_bt.hass.states.get.return_value = new_state
        mock_bt.real_trvs[ENTITY_ID].last_temperature = 19.0

        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.bt_target_temp == 22.0

    @pytest.mark.asyncio
    async def test_cooler_sync_keeps_cooltemp_above_target(self, mock_bt):
        """A cooltemp already above the new target is left untouched."""
        mock_bt.cooler_entity_id = "climate.cooler"
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
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

        assert mock_bt.bt_target_temp == 22.0
        assert mock_bt.bt_target_cooltemp == 25.0

    @pytest.mark.asyncio
    async def test_knob_turn_equal_to_cooltemp_is_lowered(self, mock_bt):
        """A knob turn onto the cool target is capped one step below it.

        The TRV owns the heating channel alone, so the cool target the user set
        on the air conditioner stays where it is.
        """
        mock_bt.cooler_entity_id = "climate.cooler"
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_target_cooltemp = 22.0  # equal to the reported setpoint
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

        assert mock_bt.bt_target_temp == 21.5
        assert mock_bt.bt_target_cooltemp == 22.0
        assert mock_bt.bt_target_temp < mock_bt.bt_target_cooltemp

    @pytest.mark.asyncio
    async def test_knob_turn_above_cooltemp_is_lowered(self, mock_bt, caplog):
        """A knob turn past the cool target is capped, not the cool target raised.

        The knob went somewhere the group cannot follow, so the user gets an
        INFO naming the target that was not cleared and the value kept. Every
        detent of a turn produces one, which is why it is not a WARNING.
        """
        mock_bt.cooler_entity_id = "climate.cooler"
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_target_cooltemp = 22.5
        mock_bt.bt_target_temp_step = 0.5

        old_state = _make_state(
            attributes={"temperature": 19.0, "current_temperature": 18.0}
        )
        new_state = _make_state(
            attributes={"temperature": 24.0, "current_temperature": 18.0}
        )
        trv_state = _make_state(
            state_str="heat",
            attributes={"current_temperature": 18.0, "temperature": 24.0},
        )
        mock_bt.hass.states.get.return_value = trv_state
        mock_bt.real_trvs[ENTITY_ID].last_temperature = 19.0

        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        caplog.set_level(logging.INFO)
        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.bt_target_temp == 22.0
        assert mock_bt.bt_target_cooltemp == 22.5
        assert mock_bt.bt_target_temp < mock_bt.bt_target_cooltemp
        assert (
            "reported setpoint 24.00 does not clear the cooling target 22.50"
            in caplog.text
        )
        assert "keeping 22.00" in caplog.text
        levels = {
            record.levelno
            for record in caplog.records
            if "cooling target" in record.getMessage()
        }
        assert levels == {logging.INFO}

    @pytest.mark.asyncio
    async def test_no_legal_setpoint_below_cooltemp_moves_it_one_step(self, mock_bt):
        """With the cool target at the minimum the cool target yields one step.

        No heating setpoint below bt_min_temp exists, so the heat target stops
        at the minimum and the cool target gives up exactly one step.
        """
        mock_bt.cooler_entity_id = "climate.cooler"
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_target_cooltemp = 5.0
        mock_bt.bt_min_temp = 5.0
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

        assert mock_bt.bt_target_temp == 5.0
        assert mock_bt.bt_target_cooltemp == 5.5
        assert mock_bt.bt_target_temp < mock_bt.bt_target_cooltemp

    @pytest.mark.asyncio
    async def test_a_range_narrowed_above_the_cooltemp_moves_it_further(self, mock_bt):
        """A cool target below the minimum is the one case that moves further.

        The range is recomputed from the children, so it can end up above a
        target already in place. The clamp holds the heating setpoint at the
        minimum and the tie-break then lifts the cooling target clear of it,
        which takes more than one step.
        """
        mock_bt.cooler_entity_id = "climate.cooler"
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_target_cooltemp = 10.0
        mock_bt.bt_min_temp = 20.0
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

        assert mock_bt.bt_target_temp == 20.0
        assert mock_bt.bt_target_cooltemp == 20.5
        assert mock_bt.bt_target_temp < mock_bt.bt_target_cooltemp

    @pytest.mark.asyncio
    async def test_cooler_sync_with_unknown_cooltemp_adopts_setpoint(self, mock_bt):
        """An unknown cool target does not abort the heating-setpoint adoption."""
        mock_bt.cooler_entity_id = "climate.cooler"
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_target_cooltemp = None
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

        assert mock_bt.bt_target_temp == 22.0
        assert mock_bt.bt_target_cooltemp is None
        mock_bt.async_write_ha_state.assert_called()
        mock_bt.control_queue_task.put.assert_awaited_with(mock_bt)

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

    @pytest.mark.asyncio
    async def test_no_off_leaving_off_keeps_the_two_targets_apart(self, mock_bt):
        """A knob turn that also switches BT on stays below the cool target.

        The same event adopts the setpoint and resolves the mode from OFF to
        HEAT, so the mode is still OFF while the setpoint is bounded. Keying the
        bound off the configured cooler rather than the live mode is what keeps
        the two channels from ending up crossed with nothing left to repair them.
        """
        mock_bt.real_trvs[ENTITY_ID].advanced["no_off_system_mode"] = True
        mock_bt.real_trvs[ENTITY_ID].min_temp = 5.0
        mock_bt.cooler_entity_id = "climate.cooler"
        mock_bt.bt_hvac_mode = HVACMode.OFF
        mock_bt.hvac_mode = HVACMode.OFF
        mock_bt.bt_target_cooltemp = 22.0
        mock_bt.bt_target_temp_step = 0.5
        old_state = _make_state(
            attributes={"temperature": 5.0, "current_temperature": 18.0}
        )
        new_state = _make_state(
            attributes={"temperature": 24.0, "current_temperature": 18.0}
        )
        trv_state = _make_state(
            state_str="heat",
            attributes={"current_temperature": 18.0, "temperature": 24.0},
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
        assert mock_bt.bt_target_temp == 21.5
        assert mock_bt.bt_target_cooltemp == 22.0
        assert mock_bt.bt_target_temp < mock_bt.bt_target_cooltemp

    @pytest.mark.asyncio
    async def test_no_off_leaving_off_separates_targets_stuck_at_the_minimum(
        self, mock_bt, caplog
    ):
        """A knob turn against a cool target at the minimum still ends apart.

        With the cool target sitting on bt_min_temp there is no legal heating
        setpoint below it, so the clamp pins the heat target to that same value
        and the tie-break has to resolve the draw. The mode is still OFF while
        the setpoint is adopted, so only the mode resolution later in the same
        event puts the group into HEAT_COOL and makes the tie-break effective.

        This is the corner where the kept value equals the target it yielded to,
        so the annunciation is pinned here as well: it may only claim that the
        report did not clear the cooling target, never that the kept value ends
        up on either side of it.
        """
        mock_bt.real_trvs[ENTITY_ID].advanced["no_off_system_mode"] = True
        mock_bt.real_trvs[ENTITY_ID].min_temp = 5.0
        mock_bt.cooler_entity_id = "climate.cooler"
        mock_bt.bt_hvac_mode = HVACMode.OFF
        # A configured cooler drops HEAT from the mode list and reports the
        # group as HEAT_COOL instead, so the live mode has to be resolved by
        # the production property rather than pinned to a fixed value.
        mock_bt.map_on_hvac_mode = HVACMode.HEAT_COOL
        mock_bt._hvac_list = [HVACMode.OFF, HVACMode.HEAT_COOL]
        type(mock_bt).hvac_mode = PropertyMock(
            side_effect=lambda: BetterThermostat.hvac_mode.fget(mock_bt)
        )
        mock_bt.bt_min_temp = 5.0
        mock_bt.bt_target_cooltemp = 5.0
        mock_bt.bt_target_temp_step = 0.5
        old_state = _make_state(
            attributes={"temperature": 5.0, "current_temperature": 18.0}
        )
        new_state = _make_state(
            attributes={"temperature": 24.0, "current_temperature": 18.0}
        )
        trv_state = _make_state(
            state_str="heat",
            attributes={"current_temperature": 18.0, "temperature": 24.0},
        )
        mock_bt.hass.states.get.return_value = trv_state
        mock_bt.real_trvs[ENTITY_ID].last_temperature = 5.0

        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        caplog.set_level(logging.INFO)
        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.bt_hvac_mode == HVACMode.HEAT
        assert mock_bt.hvac_mode == HVACMode.HEAT_COOL
        assert mock_bt.bt_target_temp == 5.0
        assert mock_bt.bt_target_cooltemp == 5.5
        assert mock_bt.bt_target_temp < mock_bt.bt_target_cooltemp
        assert (
            "reported setpoint 24.00 does not clear the cooling target 5.00, "
            "keeping 5.00" in caplog.text
        )
        assert "to stay below cooling target" not in caplog.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "room_mode",
        [
            pytest.param(HVACMode.HEAT, id="heat"),
            pytest.param(HVACMode.HEAT_COOL, id="heat_cool"),
        ],
    )
    async def test_a_routine_no_off_report_requests_no_cycle(self, mock_bt, room_mode):
        """A device without an off mode repeating its setpoint leaves a heating room as it is.

        A heating room holds HEAT, with a cooler (published as HEAT_COOL) or
        without one. A routine report of the setpoint the device already holds
        names the mode the room is in, so it moves nothing and requests no
        control cycle.
        """
        mock_bt.real_trvs[ENTITY_ID].advanced["no_off_system_mode"] = True
        mock_bt.real_trvs[ENTITY_ID].min_temp = 5.0
        mock_bt.real_trvs[ENTITY_ID].last_temperature = 20.0
        mock_bt.map_on_hvac_mode = room_mode
        mock_bt.bt_hvac_mode = HVACMode.HEAT
        mock_bt.bt_target_temp = 20.0
        mock_bt.bt_target_cooltemp = 25.0
        routine = {"temperature": 20.0, "current_temperature": 18.0}
        mock_bt.hass.states.get.return_value = _make_state(
            state_str="heat", attributes=routine
        )
        event = _make_event(
            mock_bt,
            new_state=_make_state(attributes=routine),
            old_state=_make_state(attributes=routine),
        )

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.bt_hvac_mode == HVACMode.HEAT
        mock_bt.control_queue_task.put.assert_not_awaited()


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
    @pytest.mark.parametrize(
        ("step", "room_target", "written", "turned_to"),
        [
            pytest.param(0.5, 20.25, 20.0, 20.5, id="half_degree_grid"),
            pytest.param(1.0, 20.3, 20.0, 21.0, id="whole_degree_grid"),
        ],
    )
    async def test_a_knob_turn_toward_an_off_grid_target_is_adopted(
        self, mock_bt, step, room_target, written, turned_to
    ):
        """A setpoint is an echo only when it is a value Better Thermostat wrote.

        The room target lies between two steps of the device's grid, so the
        device holds it rounded. One step up from the written value lands
        closer to the room target than a step, and is still the user's turn.
        """
        self._set_target_temp_based(mock_bt)
        mock_bt.bt_target_temp = room_target
        mock_bt.bt_target_temp_step = step
        trv = mock_bt.real_trvs[ENTITY_ID]
        trv.target_temp_step = step
        trv.last_temperature = written
        trv.confirmed_setpoint = written

        old_state = _make_state(
            attributes={"temperature": written, "current_temperature": 18.0}
        )
        new_state = _make_state(
            attributes={"temperature": turned_to, "current_temperature": 18.0}
        )
        mock_bt.hass.states.get.return_value = new_state
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.bt_target_temp == turned_to

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


class TestReportAfterAnUnconfirmedWrite:
    """Setpoint reports judged against the writes the device may still hold.

    ``pending_setpoints`` carries every write since the confirmed setpoint
    since. A report equal to one of them is BT's own write coming back; a
    report outside them is a press.
    """

    _TRV_MODULE = "custom_components.better_thermostat.events.trv"

    @staticmethod
    def _prepare(mock_bt, *, last_temperature, echo_setpoints):
        mock_bt.bt_target_temp = 24.0
        mock_bt.contact_open = False
        trv = mock_bt.real_trvs[ENTITY_ID]
        trv.last_temperature = last_temperature
        for index, value in enumerate(echo_setpoints, start=1):
            trv.pending_setpoints.append(PendingSetpoint(value, index))
        trv.last_setpoint_write_id = len(echo_setpoints)
        trv.target_temp_received = True
        trv.system_mode_received = True
        trv.hvac_mode = HVACMode.HEAT
        trv.ignore_trv_states = False
        trv.advanced["child_lock"] = False

    @classmethod
    async def _report(cls, mock_bt, *, previous_setpoint, reported_setpoint):
        """Fire a state event whose room temperature moved from 18.0 to 18.5."""
        old_state = _make_state(
            attributes={"temperature": previous_setpoint, "current_temperature": 18.0}
        )
        new_state = _make_state(
            attributes={"temperature": reported_setpoint, "current_temperature": 18.5}
        )
        mock_bt.hass.states.get.return_value = new_state
        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)
        with patch(
            f"{cls._TRV_MODULE}.convert_inbound_states", return_value=HVACMode.HEAT
        ):
            await trigger_trv_change(mock_bt, event)

    @pytest.mark.asyncio
    async def test_a_report_of_an_earlier_write_is_an_echo(self, mock_bt, caplog):
        """A room-temperature event carrying the earlier 26.0 leaves the target alone."""
        self._prepare(mock_bt, last_temperature=25.0, echo_setpoints=[26.0, 25.0])

        with caplog.at_level(logging.DEBUG, logger=self._TRV_MODULE):
            await self._report(mock_bt, previous_setpoint=26.0, reported_setpoint=26.0)

        assert mock_bt.bt_target_temp == 24.0
        assert "decoded TRV target temp changed" not in caplog.text

    @pytest.mark.asyncio
    async def test_a_report_away_from_every_remembered_write_is_a_press(self, mock_bt):
        """A report of 27.0 against remembered 26.0 and 25.0 becomes the target."""
        self._prepare(mock_bt, last_temperature=25.0, echo_setpoints=[26.0, 25.0])

        await self._report(mock_bt, previous_setpoint=26.0, reported_setpoint=27.0)

        assert mock_bt.bt_target_temp == 27.0

    @pytest.mark.asyncio
    async def test_a_report_outside_the_remembered_writes_is_a_press_when_unchanged(
        self, mock_bt
    ):
        """A value nobody wrote is a press, even with the setpoint attribute still.

        Guards against over-suppression: with only the 25.0 command
        remembered, a report of 26.0 matches no known value and becomes the
        room target although the setpoint attribute did not change.
        """
        self._prepare(mock_bt, last_temperature=25.0, echo_setpoints=[25.0])

        await self._report(mock_bt, previous_setpoint=26.0, reported_setpoint=26.0)

        assert mock_bt.bt_target_temp == 26.0


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

    @pytest.mark.parametrize(
        ("hvac_modes", "reported"),
        [
            pytest.param([HVACMode.OFF, HVACMode.HEAT], "heat", id="heat_only"),
            pytest.param(
                [HVACMode.OFF, HVACMode.HEAT, HVACMode.AUTO], "heat", id="heat_and_auto"
            ),
            pytest.param(
                [HVACMode.OFF, HVACMode.HEAT, HVACMode.HEAT_COOL],
                "heat",
                id="both_spellings",
            ),
            pytest.param(
                [HVACMode.OFF, HVACMode.HEAT, HVACMode.HEAT_COOL],
                "heat_cool",
                id="both_spellings_reporting_heat_cool",
            ),
            pytest.param(
                [HVACMode.OFF, HVACMode.HEAT_COOL], "heat_cool", id="heat_cool_only"
            ),
        ],
    )
    def test_a_reported_heating_mode_is_carried_through(
        self, mock_bt, hvac_modes, reported
    ):
        """A device reporting that it heats yields HEAT, not nothing.

        The remap runs for real here: its result is the only thing this
        function judges, and a value it does not carry on leaves the whole
        mode adoption downstream without an input.
        """
        mock_bt.real_trvs[ENTITY_ID].hvac_modes = list(hvac_modes)
        state = _make_state(state_str=reported)

        assert convert_inbound_states(mock_bt, ENTITY_ID, state) == HVACMode.HEAT

    @pytest.mark.parametrize(
        "hvac_modes",
        [
            pytest.param([HVACMode.OFF, HVACMode.HEAT], id="heat_only"),
            pytest.param(
                [HVACMode.OFF, HVACMode.HEAT, HVACMode.AUTO], id="heat_and_auto"
            ),
            pytest.param(
                [HVACMode.OFF, HVACMode.HEAT, HVACMode.HEAT_COOL], id="both_spellings"
            ),
            pytest.param([HVACMode.OFF, HVACMode.HEAT_COOL], id="heat_cool_only"),
        ],
    )
    def test_a_reported_off_is_carried_through(self, mock_bt, hvac_modes):
        """The off direction reaches the entity for every offered set."""
        mock_bt.real_trvs[ENTITY_ID].hvac_modes = list(hvac_modes)
        state = _make_state(state_str="off")

        assert convert_inbound_states(mock_bt, ENTITY_ID, state) == HVACMode.OFF

    def test_a_swapped_heat_cool_device_reporting_heat_cool_heats(self, mock_bt):
        """A swapped device whose heating mode is heat_cool reports it as HEAT.

        Better Thermostat writes heat_cool to such a device, so switching it
        back on at the panel has to reach the adoption as heating.
        """
        trv = mock_bt.real_trvs[ENTITY_ID]
        trv.advanced["heat_auto_swapped"] = True
        trv.hvac_modes = [HVACMode.OFF, HVACMode.HEAT_COOL]
        state = _make_state(state_str="heat_cool")

        assert convert_inbound_states(mock_bt, ENTITY_ID, state) == HVACMode.HEAT

    def test_an_unswapped_device_reporting_auto_is_ignored(self, mock_bt):
        """A reported AUTO without the swap option reaches the adoption as no mode.

        AUTO names the device's heating mode only when the swap option says
        so; without it the report is ambiguous and must neither switch the
        room off nor on.
        """
        mock_bt.real_trvs[ENTITY_ID].hvac_modes = [
            HVACMode.OFF,
            HVACMode.HEAT,
            HVACMode.AUTO,
        ]
        state = _make_state(state_str="auto")

        assert convert_inbound_states(mock_bt, ENTITY_ID, state) is None

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

    def test_off_offered_in_the_device_spelling_switches_the_device_off(self, mock_bt):
        """A list naming its modes ``HVACMode.OFF`` still offers OFF.

        The cache holds the device's own spelling, so the min_temp
        substitution must not fire for a device that does offer OFF.
        """
        mock_bt.real_trvs[ENTITY_ID].hvac_modes = ["HVACMode.OFF", "HVACMode.HEAT"]
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
        assert result["system_mode"] == HVACMode.OFF
        assert result["temperature"] == 19.0

    def test_no_off_in_the_device_spelling_still_uses_min_temp(self, mock_bt):
        """A device genuinely without OFF keeps taking the min_temp path."""
        mock_bt.real_trvs[ENTITY_ID].hvac_modes = ["HVACMode.HEAT"]
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

    def test_unsupported_mode_writes_only_the_setpoint(self, mock_bt):
        """A device without a heating mode gets the setpoint and no mode."""
        mock_bt.real_trvs[ENTITY_ID].hvac_modes = [
            HVACMode.AUTO,
            HVACMode.COOL,
            HVACMode.OFF,
        ]
        mock_bt.real_trvs[ENTITY_ID].current_temperature = 18.0

        with patch(
            "custom_components.better_thermostat.events.trv.calculate_calibration_local",
            return_value=0.0,
        ):
            result = convert_outbound_states(mock_bt, ENTITY_ID, HVACMode.HEAT_COOL)

        assert result is not None
        assert result["system_mode"] is None
        assert result["temperature"] == mock_bt.bt_target_temp

    def test_swapped_device_in_a_cooler_room_is_switched_on(self, mock_bt):
        """A room-level HEAT_COOL reaches a swapped radiator as its own mode."""
        mock_bt.cooler_entity_id = "climate.the_ac"
        mock_bt.real_trvs[ENTITY_ID].hvac_modes = [HVACMode.OFF, HVACMode.AUTO]
        mock_bt.real_trvs[ENTITY_ID].advanced["heat_auto_swapped"] = True
        mock_bt.real_trvs[ENTITY_ID].current_temperature = 18.0

        with patch(
            "custom_components.better_thermostat.events.trv.calculate_calibration_local",
            return_value=0.0,
        ):
            result = convert_outbound_states(mock_bt, ENTITY_ID, HVACMode.HEAT_COOL)

        assert result is not None
        assert result["system_mode"] == HVACMode.AUTO
        assert result["temperature"] == mock_bt.bt_target_temp

    def test_off_without_off_mode_still_falls_back_to_min_temp(self, mock_bt):
        """OFF escapes the clamp so the min_temp substitution keeps working."""
        mock_bt.real_trvs[ENTITY_ID].hvac_modes = [HVACMode.AUTO, HVACMode.HEAT]
        mock_bt.real_trvs[ENTITY_ID].current_temperature = 18.0

        with patch(
            "custom_components.better_thermostat.events.trv.calculate_calibration_local",
            return_value=None,
        ):
            result = convert_outbound_states(mock_bt, ENTITY_ID, HVACMode.OFF)

        assert result == {
            "temperature": 5.0,
            "local_temperature": 18.0,
            "system_mode": None,
        }

    def test_no_off_system_mode_device_still_parks_at_min_temp(self, mock_bt):
        """The no_off_system_mode substitution survives the clamp."""
        mock_bt.real_trvs[ENTITY_ID].hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
        mock_bt.real_trvs[ENTITY_ID].advanced["no_off_system_mode"] = True
        mock_bt.real_trvs[ENTITY_ID].current_temperature = 18.0

        with patch(
            "custom_components.better_thermostat.events.trv.calculate_calibration_local",
            return_value=0.0,
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
    # Climate entities publish no unit attribute, so every temperature the
    # handler reads off a group member is interpreted in this system unit.
    bt.hass.config.units.temperature_unit = UnitOfTemperature.CELSIUS
    bt.device_name = "Grouped Thermostat"
    bt.bt_hvac_mode = bt_hvac_mode
    bt.map_on_hvac_mode = HVACMode.HEAT
    bt.bt_target_temp = 19.0
    bt.bt_min_temp = 5.0
    bt.bt_max_temp = 30.0
    bt.bt_target_cooltemp = 25.0
    bt.bt_target_temp_step = 0.5
    bt.cur_temp = 18.0
    bt.window_open = False
    bt.contact_open = False
    bt.tolerance = 0.3
    bt.startup_running = False
    bt.control_queue_task = AsyncMock()
    bt.bt_update_lock = False
    bt.cooler_entity_id = None
    bt.ignore_states = False
    bt.context = MagicMock()
    bt.async_write_ha_state = MagicMock()
    bt.hvac_mode = bt_hvac_mode
    bt._enforce_cool_above_heat = lambda **kwargs: (
        BetterThermostat._enforce_cool_above_heat(bt, **kwargs)
    )
    bt._clamp_inbound_heat_target = lambda v: (
        BetterThermostat._clamp_inbound_heat_target(bt, v)
    )
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

    @pytest.mark.asyncio
    async def test_group_knob_turn_stays_below_the_cool_target(self):
        """A knob turn on one member of a group with a cooler stays below cool.

        The cool target sits on ``bt_min_temp``, so the bound has no legal value
        below it and the residual tie-break has to separate the two targets.
        """
        trigger, other1, other2 = GRP_IDS
        bt = _make_group_bt(GRP_IDS, bt_hvac_mode=HVACMode.HEAT)
        bt.cooler_entity_id = "climate.ac"
        # A heating room with a cooler publishes heat_cool.
        bt.map_on_hvac_mode = HVACMode.HEAT_COOL
        bt.hvac_mode = HVACMode.HEAT_COOL
        bt.bt_target_cooltemp = 5.0
        bt.bt_min_temp = 5.0
        bt.bt_target_temp_step = 0.5
        _install_states(
            bt,
            {
                trigger: _grp_state(trigger, "heat", temperature=22.0),
                other1: _grp_state(other1, "heat"),
                other2: _grp_state(other2, "heat"),
            },
        )
        bt.real_trvs[trigger].hvac_mode = "heat"

        event = _make_event(
            bt,
            new_state=_grp_state(trigger, "heat", temperature=22.0),
            old_state=_grp_state(trigger, "heat", temperature=19.0),
            entity_id=trigger,
        )
        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=None,
        ):
            await trigger_trv_change(bt, event)

        assert bt.bt_target_temp == 5.0
        assert bt.bt_target_cooltemp == 5.5
        assert bt.bt_target_temp < bt.bt_target_cooltemp


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


class TestDualRoleEntityReports:
    """Reports from a device named as both the thermostat and the cooler.

    Such a device publishes one setpoint for two targets. The mode it reports
    is the statement about which target a press on its own controls meant, and
    what either channel wrote is not a press at all.
    """

    @pytest.fixture
    def shared_bt(self, mock_bt):
        """Make the tracked thermostat the configured cooler as well."""
        mock_bt.cooler_entity_id = ENTITY_ID
        mock_bt.bt_hvac_mode = HVACMode.HEAT
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_target_temp = 20.0
        mock_bt.bt_target_cooltemp = 24.0
        mock_bt.last_sent_cooler_temp = 24.0
        mock_bt.last_cooler_mode_decided = None
        mock_bt.real_trvs[ENTITY_ID].hvac_modes = [
            HVACMode.OFF,
            HVACMode.HEAT,
            HVACMode.COOL,
            HVACMode.HEAT_COOL,
        ]
        mock_bt.real_trvs[ENTITY_ID].last_temperature = 20.0
        mock_bt._clamp_inbound_cool_target = lambda v: (
            BetterThermostat._clamp_inbound_cool_target(mock_bt, v)
        )
        mock_bt._enforce_heat_below_cool = lambda: (
            BetterThermostat._enforce_heat_below_cool(mock_bt)
        )
        return mock_bt

    @staticmethod
    async def _report(bt, *, device_mode, reported_temp, previous_temp, step=None):
        """Drive one device report through the real TRV handler.

        ``step`` is the setpoint step the device publishes, if any.
        """
        published = {} if step is None else {"target_temp_step": step}
        old_state = _make_state(
            state_str=device_mode,
            attributes={
                "temperature": previous_temp,
                "current_temperature": 22.0,
                **published,
            },
        )
        new_state = _make_state(
            state_str=device_mode,
            attributes={
                "temperature": reported_temp,
                "current_temperature": 22.0,
                **published,
            },
        )
        # trigger_trv_change reads the state machine rather than the event's
        # new_state for the device's own mode, so both carry the report.
        bt.hass.states.get.return_value = new_state
        # BT already holds the mode and the internal temperature the device
        # reports, so the report under test carries a setpoint and nothing
        # else, and a queued control cycle is the setpoint's doing.
        bt.real_trvs[ENTITY_ID].hvac_mode = device_mode
        bt.real_trvs[ENTITY_ID].current_temperature = 22.0
        event = _make_event(bt, new_state=new_state, old_state=old_state)
        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(bt, event)

    @pytest.mark.asyncio
    async def test_shared_entity_reads_the_cooling_channel_write_as_an_echo(
        self, shared_bt
    ):
        """The cooling channel's own setpoint read back moves no target.

        The device reports the cooling target the cooling channel just wrote to
        it. Read as a press, it would drag the heating target up to just below
        the cooling one, which is the heating setpoint moving on its own the
        moment the room switches to cooling.
        """
        await self._report(
            shared_bt, device_mode="cool", reported_temp=24.0, previous_temp=20.0
        )

        assert shared_bt.bt_target_temp == 20.0
        assert shared_bt.bt_target_cooltemp == 24.0
        shared_bt.control_queue_task.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_shared_entity_files_a_press_under_the_cooling_channel_while_it_cools(
        self, shared_bt
    ):
        """A press on the remote while the unit cools names the cool target."""
        await self._report(
            shared_bt, device_mode="cool", reported_temp=26.0, previous_temp=24.0
        )

        assert shared_bt.bt_target_cooltemp == 26.0
        assert shared_bt.bt_target_temp == 20.0
        shared_bt.control_queue_task.put.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_shared_entity_files_a_press_under_the_heating_channel_while_it_heats(
        self, shared_bt
    ):
        """A press on the remote while the unit heats names the heat target."""
        await self._report(
            shared_bt, device_mode="heat", reported_temp=21.0, previous_temp=20.0
        )

        assert shared_bt.bt_target_temp == 21.0
        assert shared_bt.bt_target_cooltemp == 24.0
        shared_bt.control_queue_task.put.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_shared_entity_follows_the_latch_while_the_reported_mode_lags(
        self, shared_bt
    ):
        """A cooling setpoint written before its mode is still cooling's.

        The cooling channel writes the setpoint first and the mode second, so
        the report of that setpoint arrives while the device still names the
        mode it is leaving.
        """
        shared_bt.last_cooler_mode_decided = HVACMode.COOL
        shared_bt.last_sent_cooler_temp = None

        await self._report(
            shared_bt, device_mode="heat", reported_temp=26.0, previous_temp=20.0
        )

        assert shared_bt.bt_target_cooltemp == 26.0
        assert shared_bt.bt_target_temp == 20.0
        shared_bt.control_queue_task.put.assert_awaited_once()

    @pytest.mark.parametrize(
        ("cool_target", "held", "pressed"), [(24.3, 24.0, 25.0), (24.7, 25.0, 24.0)]
    )
    @pytest.mark.asyncio
    async def test_shared_entity_press_toward_an_off_grid_cool_target_is_adopted(
        self, shared_bt, cool_target, held, pressed
    ):
        """One press toward an off-grid cool target names the cool target.

        The device holds the cool target on its own grid, so a single press
        from there toward the target lands less than a step from it.
        """
        shared_bt.real_trvs[ENTITY_ID].target_temp_step = 1.0
        shared_bt.bt_target_cooltemp = cool_target
        shared_bt.last_sent_cooler_temp = cool_target

        await self._report(
            shared_bt,
            device_mode="cool",
            reported_temp=pressed,
            previous_temp=held,
            step=1.0,
        )

        assert shared_bt.bt_target_cooltemp == pressed
        assert shared_bt.bt_target_temp == 20.0
        shared_bt.control_queue_task.put.assert_awaited_once()

    @pytest.mark.parametrize(("cool_target", "held"), [(24.3, 24.0), (24.7, 25.0)])
    @pytest.mark.parametrize("send_cache_primed", [True, False])
    @pytest.mark.asyncio
    async def test_shared_entity_reads_an_off_grid_cooling_write_as_an_echo(
        self, shared_bt, cool_target, held, send_cache_primed
    ):
        """An off-grid cool target the device holds on its grid moves nothing."""
        shared_bt.real_trvs[ENTITY_ID].target_temp_step = 1.0
        shared_bt.bt_target_cooltemp = cool_target
        shared_bt.last_sent_cooler_temp = cool_target if send_cache_primed else None

        await self._report(
            shared_bt,
            device_mode="cool",
            reported_temp=held,
            previous_temp=20.0,
            step=1.0,
        )

        assert shared_bt.bt_target_cooltemp == cool_target
        assert shared_bt.bt_target_temp == 20.0
        shared_bt.control_queue_task.put.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_distinct_trv_setpoint_matching_the_cool_target_is_still_adopted(
        self, mock_bt
    ):
        """A radiator that is not the cooler keeps the narrow echo set.

        Its setpoint is never written by the cooling channel, so a knob turn
        that lands on the cooling target is a press like any other.
        """
        mock_bt.cooler_entity_id = "climate.split_unit"
        mock_bt.bt_target_temp = 19.0
        mock_bt.bt_target_cooltemp = 24.0
        mock_bt.last_sent_cooler_temp = 24.0
        mock_bt.bt_max_temp = 30.0
        mock_bt.real_trvs[ENTITY_ID].last_temperature = 19.0
        mock_bt.real_trvs[ENTITY_ID].max_temp = 30.0

        await self._report(
            mock_bt, device_mode="heat", reported_temp=24.0, previous_temp=19.0
        )

        assert mock_bt.bt_target_temp == 23.5
        mock_bt.control_queue_task.put.assert_awaited_once()


def _prepare_outage_room(bt, *, with_peer: bool):
    """Make ``bt`` a room whose head ``ENTITY_ID`` has just gone off the air.

    With ``with_peer`` a second head that is still on the air shares the room.
    The stand-in carries what the listener in climate.py reads before it hands
    the event on, and collects the handler it hands it to.
    """
    if with_peer:
        _add_homematicip_peer(bt)
    unavailable = State(ENTITY_ID, "unavailable")
    peer_state = State(
        PEER_ID, "heat", attributes={"current_temperature": 20.0, "temperature": 19.0}
    )
    bt.hass.states.get.side_effect = lambda entity_id: (
        unavailable if entity_id == ENTITY_ID else peer_state
    )
    bt.in_maintenance = False
    bt.devices_errors = []
    bt.devices_states = {}
    bt._critical_grace_until = None
    # The listener looks for TRVs startup went ahead without before it reads
    # the report; every head here was set up by startup.
    bt._trvs_initializing = set()
    bt._initialize_arrived_trvs = lambda: BetterThermostat._initialize_arrived_trvs(bt)
    spawned = []
    bt._spawn_owned = lambda coro, name=None: spawned.append(coro)
    return unavailable, spawned


class TestOutageReportThroughTheListener:
    """The report of a head going off the air, from the listener to the handler.

    ``BetterThermostat._trigger_trv_change`` receives the state change and
    hands it to ``trigger_trv_change``, whose outage branch drops the head's
    internal temperature and lets the first reading after the outage past the
    debounce. The two run together here, with the real availability check in
    front of the handler, because that is the way the report arrives.
    """

    @pytest.mark.parametrize(
        "with_peer", [True, False], ids=["one_of_two_heads", "the_only_head"]
    )
    @pytest.mark.asyncio
    async def test_the_outage_report_reaches_the_handler(self, mock_bt, with_peer):
        """A head that goes off the air stops counting as a live reading.

        Its last internal temperature is dropped, and its first reading after
        the outage is taken without waiting out the debounce, whether or not
        the room has another head.
        """
        unavailable, spawned = _prepare_outage_room(mock_bt, with_peer=with_peer)
        trv = mock_bt.real_trvs[ENTITY_ID]
        trv.last_internal_sensor_change = dt_util.now()
        recovered = _make_state(attributes={"current_temperature": 21.0})
        routed_states = mock_bt.hass.states.get.side_effect

        with (
            patch("custom_components.better_thermostat.utils.watcher.ir"),
            patch(
                "custom_components.better_thermostat.climate."
                "check_and_update_degraded_mode",
                AsyncMock(),
            ),
        ):
            await BetterThermostat._trigger_trv_change(
                mock_bt, _make_event(mock_bt, new_state=unavailable)
            )
            for handler in spawned:
                await handler
            spawned.clear()

            assert trv.current_temperature is None
            assert trv.accept_next_internal_temp is True

            mock_bt.hass.states.get.side_effect = lambda entity_id: (
                recovered if entity_id == ENTITY_ID else routed_states(entity_id)
            )
            await BetterThermostat._trigger_trv_change(
                mock_bt,
                _make_event(mock_bt, new_state=recovered, old_state=unavailable),
            )
            for handler in spawned:
                await handler

        assert trv.current_temperature == 21.0
