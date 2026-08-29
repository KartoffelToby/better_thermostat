"""Tests for events/trv.py – TRV event handlers and conversion helpers.

Covers guard clauses, internal temperature changes, HVAC action/valve caching,
mode synchronisation, target-temperature adoption, control-queue triggering,
and the convert_inbound_states / convert_outbound_states helpers.
"""

from datetime import timedelta
import logging
from unittest.mock import AsyncMock, MagicMock, patch

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
from custom_components.better_thermostat.trv import Trv
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
    # climate entities publish no unit attribute, so every temperature read off
    # a TRV state resolves through the system unit.
    bt.hass.config.units.temperature_unit = UnitOfTemperature.CELSIUS
    bt.device_name = "Test Thermostat"
    bt.bt_hvac_mode = HVACMode.HEAT
    bt.hvac_mode = HVACMode.HEAT
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
    bt.control_queue_task = MagicMock()
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


def _add_homematicip_peer(bt):
    """Put a second, HomematicIP-flagged valve into the same room.

    Returns the state the peer reports, so a caller can route
    ``hass.states.get`` to the right state per entity.
    """
    peer = Trv.from_legacy_dict(
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
    bt.real_trvs[PEER_ID] = peer
    bt.all_trvs = [
        {"advanced": {CONF_HOMEMATICIP: False}},
        {"advanced": {CONF_HOMEMATICIP: True}},
    ]
    peer_state = State(
        PEER_ID, "heat", attributes={"current_temperature": 20.0, "temperature": 19.0}
    )

    def _state_for(entity_id):
        return (
            peer_state
            if entity_id == PEER_ID
            else _make_state(attributes={"current_temperature": 20.0})
        )

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


# ---------------------------------------------------------------------------
# 1. Guard clauses
# ---------------------------------------------------------------------------


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
        mock_bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_early_old_state_none(self, mock_bt):
        """Return early when old_state is None."""
        event = _make_event(mock_bt)
        event.data["old_state"] = None
        await trigger_trv_change(mock_bt, event)
        mock_bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_own_context(self, mock_bt):
        """Skip processing when event context matches BT's own context."""
        event = _make_event(mock_bt)
        event.context = mock_bt.context
        await trigger_trv_change(mock_bt, event)
        mock_bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_org_trv_state_none_returns_early(self, mock_bt):
        """Return early when hass.states.get() returns None (no crash)."""
        mock_bt.hass.states.get.return_value = None
        event = _make_event(mock_bt)

        await trigger_trv_change(mock_bt, event)
        mock_bt.control_queue_task.put_nowait.assert_not_called()


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
    async def test_unavailable_trv_invalidates_internal_temperature(self, mock_bt):
        """An unavailable TRV's stored internal temperature is cleared.

        SENSOR_FALLBACK and the ladder must stop treating it as live.
        """
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

        mock_bt.control_queue_task.put_nowait.assert_not_called()

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
                autospec=True,
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

    @pytest.mark.asyncio
    async def test_entry_removed_during_await_completes(self, mock_bt):
        """The handler survives the entry vanishing mid-flight.

        A reconfigure/unload can remove the real_trvs entry while the
        handler awaits get_current_offset; the handler keeps working on
        its local Trv object and completes without raising.
        """
        trv = mock_bt.real_trvs[ENTITY_ID]
        trv.calibration_received = False
        trv.calibration = 0
        trv_state = _make_state(attributes={"current_temperature": 20.0})
        mock_bt.hass.states.get.return_value = trv_state
        trv.current_temperature = 18.0

        event = _make_event(mock_bt, new_state=trv_state, old_state=trv_state)

        async def pop_entry_and_return_offset(bt, entity_id):
            bt.real_trvs.pop(entity_id)
            return 2.5

        with (
            patch(
                "custom_components.better_thermostat.events.trv.get_current_offset",
                autospec=True,
                side_effect=pop_entry_and_return_offset,
            ),
            patch(
                "custom_components.better_thermostat.events.trv.convert_inbound_states",
                return_value=HVACMode.HEAT,
            ),
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.real_trvs == {}
        # Writes landed on the detached Trv object.
        assert trv.calibration_received is True
        assert trv.last_calibration == 2.5
        assert trv.current_temperature == 20.0

    @pytest.mark.asyncio
    async def test_entry_removed_before_offset_read_skips_it(self, mock_bt):
        """A removal before the offset read skips it instead of raising.

        The handler awaits model detection before the calibration branch;
        the entry can vanish there. The unpatched offset read resolves the
        adapter through a raw ``real_trvs[entity_id]`` index, so reaching
        it after the removal would raise KeyError.
        """
        trv = mock_bt.real_trvs[ENTITY_ID]
        trv.calibration_received = False
        trv.calibration = 0
        trv.model = None
        trv_state = _make_state(
            attributes={"current_temperature": 20.0, "model_id": "TRV-X"}
        )
        mock_bt.hass.states.get.return_value = trv_state
        trv.current_temperature = 18.0

        event = _make_event(mock_bt, new_state=trv_state, old_state=trv_state)

        async def pop_entry(bt, entity_id):
            bt.real_trvs.pop(entity_id)

        with (
            patch(
                "custom_components.better_thermostat.events.trv.get_device_model",
                side_effect=pop_entry,
            ),
            patch(
                "custom_components.better_thermostat.events.trv.convert_inbound_states",
                return_value=HVACMode.HEAT,
            ),
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.real_trvs == {}
        # The offset read was skipped; the flag still flipped on the
        # detached Trv object.
        assert trv.calibration_received is True
        assert trv.last_calibration == 0.0


# ---------------------------------------------------------------------------
# 3. HVAC action and valve position
# ---------------------------------------------------------------------------


class _UnprintableValvePosition:
    """A valve reading that cannot be rendered as text."""

    def __str__(self):
        raise RuntimeError("boom")


class TestHvacActionAndValvePosition:
    """Tests for hvac_action / valve_position cache updates."""

    @pytest.mark.asyncio
    async def test_unreadable_valve_position_propagates(self, mock_bt):
        """A valve reading that cannot be rendered as text reaches the caller."""
        trv_state = _make_state(
            attributes={
                "current_temperature": 18.0,
                "temperature": 19.0,
                "valve_position": _UnprintableValvePosition(),
            }
        )
        mock_bt.hass.states.get.return_value = trv_state
        event = _make_event(mock_bt, new_state=trv_state, old_state=trv_state)

        with (
            patch(
                "custom_components.better_thermostat.events.trv.convert_inbound_states",
                return_value=HVACMode.HEAT,
            ),
            pytest.raises(RuntimeError),
        ):
            await trigger_trv_change(mock_bt, event)

    @pytest.mark.asyncio
    async def test_valve_position_cached_from_attribute(self, mock_bt):
        """A numeric valve reading is cached on the TRV."""
        trv_state = _make_state(
            attributes={
                "current_temperature": 18.0,
                "temperature": 19.0,
                "valve_position": "42",
            }
        )
        mock_bt.hass.states.get.return_value = trv_state
        event = _make_event(mock_bt, new_state=trv_state, old_state=trv_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.real_trvs[ENTITY_ID].valve_position == 42.0

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

        mock_bt.control_queue_task.put_nowait.assert_called_once()

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
    async def test_hvac_modes_cached_from_attribute(self, mock_bt):
        """Cache the offered mode list so a runtime change is picked up."""
        trv_state = _make_state(
            attributes={
                "current_temperature": 18.0,
                "temperature": 19.0,
                "hvac_modes": ["off", "heat"],
            }
        )
        mock_bt.hass.states.get.return_value = trv_state
        mock_bt.real_trvs[ENTITY_ID].hvac_modes = [HVACMode.OFF, HVACMode.AUTO]

        event = _make_event(mock_bt, new_state=trv_state, old_state=trv_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.real_trvs[ENTITY_ID].hvac_modes == ["off", "heat"]

    @pytest.mark.asyncio
    async def test_missing_hvac_modes_keeps_the_cached_list(self, mock_bt):
        """A state without the attribute leaves the cached mode list intact."""
        trv_state = _make_state(
            attributes={"current_temperature": 18.0, "temperature": 19.0}
        )
        mock_bt.hass.states.get.return_value = trv_state
        mock_bt.real_trvs[ENTITY_ID].hvac_modes = [HVACMode.OFF, HVACMode.HEAT]

        event = _make_event(mock_bt, new_state=trv_state, old_state=trv_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.real_trvs[ENTITY_ID].hvac_modes == [HVACMode.OFF, HVACMode.HEAT]

    @pytest.mark.asyncio
    async def test_changed_mode_list_clears_the_annunciation_set(self, mock_bt):
        """A genuine capability change lets the unsupported-mode error re-fire."""
        trv_state = _make_state(
            attributes={
                "current_temperature": 18.0,
                "temperature": 19.0,
                "hvac_modes": ["off", "heat"],
            }
        )
        mock_bt.hass.states.get.return_value = trv_state
        mock_bt.real_trvs[ENTITY_ID].hvac_modes = [HVACMode.OFF, HVACMode.AUTO]
        mock_bt.real_trvs[ENTITY_ID].unsupported_modes_logged = {"heat"}

        event = _make_event(mock_bt, new_state=trv_state, old_state=trv_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.real_trvs[ENTITY_ID].unsupported_modes_logged == set()

    @pytest.mark.asyncio
    async def test_empty_hvac_modes_keeps_the_cached_list(self, mock_bt):
        """An empty list means "nothing reported", not "no modes offered"."""
        trv_state = _make_state(
            attributes={
                "current_temperature": 18.0,
                "temperature": 19.0,
                "hvac_modes": [],
            }
        )
        mock_bt.hass.states.get.return_value = trv_state
        mock_bt.real_trvs[ENTITY_ID].hvac_modes = [HVACMode.OFF, HVACMode.HEAT]

        event = _make_event(mock_bt, new_state=trv_state, old_state=trv_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.real_trvs[ENTITY_ID].hvac_modes == [HVACMode.OFF, HVACMode.HEAT]

    @pytest.mark.asyncio
    async def test_unchanged_mode_list_keeps_the_annunciation_set(self, mock_bt):
        """Repeating the same list keeps the error suppressed across cycles."""
        trv_state = _make_state(
            attributes={
                "current_temperature": 18.0,
                "temperature": 19.0,
                "hvac_modes": ["off", "heat"],
            }
        )
        mock_bt.hass.states.get.return_value = trv_state
        mock_bt.real_trvs[ENTITY_ID].hvac_modes = ["off", "heat"]
        mock_bt.real_trvs[ENTITY_ID].unsupported_modes_logged = {"heat_cool"}

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
    async def test_same_capabilities_keep_the_annunciation_set(
        self, mock_bt, republished
    ):
        """The same offered modes in another spelling are not a change."""
        trv_state = _make_state(
            attributes={
                "current_temperature": 18.0,
                "temperature": 19.0,
                "hvac_modes": republished,
            }
        )
        mock_bt.hass.states.get.return_value = trv_state
        mock_bt.real_trvs[ENTITY_ID].hvac_modes = ["auto", "cool", "off"]
        mock_bt.real_trvs[ENTITY_ID].unsupported_modes_logged = {"heat_cool"}

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

        trv_state = _make_state(
            attributes={
                "current_temperature": 18.0,
                "temperature": 19.0,
                "hvac_modes": ["cool", "off", "auto"],
            }
        )
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
            attributes={
                "current_temperature": 18.0,
                "temperature": 19.0,
                "hvac_modes": [HVACMode.OFF, HVACMode.HEAT_COOL],
            },
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
            attributes={
                "current_temperature": 18.0,
                "temperature": 19.0,
                "hvac_modes": ["HVACMode.OFF", "HVACMode.HEAT"],
            }
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
    async def test_clamped_setpoint_that_is_adopted_is_reported(self, mock_bt, caplog):
        """A clamp that changes BT's target is worth a warning."""
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
        caplog.set_level(logging.WARNING)

        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.bt_target_temp == 30.0
        assert "setpoint outside of range" in caplog.text

    @pytest.mark.asyncio
    async def test_clamped_setpoint_that_is_discarded_is_silent(self, mock_bt, caplog):
        """A clamp on a value the handler drops changes nothing to report.

        The clamp pulls the reported value onto the heating target BT already
        holds, so it is BT's own write coming back, not a user's out-of-range
        input.
        """
        mock_bt.bt_target_temp = 30.0
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
        mock_bt.real_trvs[ENTITY_ID].last_temperature = 30.0
        caplog.set_level(logging.WARNING)

        event = _make_event(mock_bt, new_state=new_state, old_state=old_state)

        with patch(
            "custom_components.better_thermostat.events.trv.convert_inbound_states",
            return_value=HVACMode.HEAT,
        ):
            await trigger_trv_change(mock_bt, event)

        assert mock_bt.bt_target_temp == 30.0
        assert "setpoint outside of range" not in caplog.text

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
        trv.temperature = 5.0
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
        """A reported target that already clears the cool target is taken as is."""
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
        assert mock_bt.bt_target_temp < mock_bt.bt_target_cooltemp

    @pytest.mark.asyncio
    async def test_adopted_target_is_capped_below_the_cool_target(self, mock_bt):
        """A knob turn onto the cool target is capped one step below it.

        The TRV speaks for the heating channel only, so the cool target keeps the
        value the user set on the cooler.
        """
        mock_bt.cooler_entity_id = "climate.cooler"
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_target_cooltemp = 22.0  # equal to the reported target
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
        assert mock_bt.bt_target_cooltemp == 22.0  # untouched
        assert mock_bt.bt_target_temp < mock_bt.bt_target_cooltemp

    @pytest.mark.asyncio
    async def test_adopted_target_above_cool_target_is_capped(self, mock_bt, caplog):
        """A knob turn past the cool target yields to it and says so.

        The knob is turned to 24.0 while the cooler holds 22.5, so the heating
        target stops one step below the cool target instead of pushing it up.
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
        assert mock_bt.bt_target_cooltemp == 22.5  # untouched
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
    async def test_no_legal_value_below_cool_target_costs_one_step(self, mock_bt):
        """At the range minimum the cool target yields, but only by one step.

        With the cool target on bt_min_temp no heating value below it exists, so
        the cap stops at the minimum and the ordering fallback lifts the cool
        target one step — not the full distance to the reported value.
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
    async def test_a_range_narrowed_above_the_cool_target_moves_it_further(
        self, mock_bt
    ):
        """A cool target below the minimum is the one case that moves further.

        The range is recomputed from the children, so it can end up above a
        target already in place. The cap holds the heating setpoint at the
        minimum and the ordering fallback then lifts the cool target clear of
        it, which takes more than one step.
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
    async def test_cooler_sync_survives_unset_cooltemp(self, mock_bt):
        """An unset cool target does not break adoption of the heat target.

        A cooler that has not reported a setpoint yet leaves
        ``bt_target_cooltemp`` at None, and the handler runs in a background
        task where an exception would abandon the adoption half-done.
        """
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
        mock_bt.control_queue_task.put_nowait.assert_called_once()

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
    async def test_no_off_wakeup_is_capped_without_a_tie_break(self, mock_bt):
        """A no_off wakeup against a cool target with room below it needs no fallback.

        The mode is still OFF while the setpoint is adopted, so the ordering
        check is gated out for the whole event. The cap alone has to keep the
        two targets apart, and it does because it keys off the configured
        cooler rather than the live mode.
        """
        mock_bt.real_trvs[ENTITY_ID].advanced["no_off_system_mode"] = True
        mock_bt.real_trvs[ENTITY_ID].min_temp = 5.0
        mock_bt.bt_hvac_mode = HVACMode.OFF
        mock_bt.hvac_mode = HVACMode.OFF
        mock_bt.cooler_entity_id = "climate.cooler"
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
        assert mock_bt.hvac_mode == HVACMode.OFF
        assert mock_bt.bt_target_temp == 21.5
        assert mock_bt.bt_target_cooltemp == 22.0
        assert mock_bt.bt_target_temp < mock_bt.bt_target_cooltemp

    @pytest.mark.asyncio
    async def test_no_off_wakeup_keeps_targets_separated(self, mock_bt, caplog):
        """A no_off valve waking the group up still separates the two targets.

        The group is OFF while the setpoint is adopted, so the mode is not
        HEAT_COOL yet and the ordering check cannot bite. The same event then
        resolves the mode to HEAT_COOL, and with the cool target sitting on
        ``bt_min_temp`` the adopted heat target lands on that very value.

        That is the corner where the kept value equals the target it yielded to,
        so the annunciation is pinned here too: it may only claim that the report
        did not clear the cooling target, never that the kept value ends up below
        it.
        """
        mock_bt.real_trvs[ENTITY_ID].advanced["no_off_system_mode"] = True
        mock_bt.real_trvs[ENTITY_ID].min_temp = 5.0
        mock_bt.bt_hvac_mode = HVACMode.OFF
        mock_bt.cooler_entity_id = "climate.cooler"
        mock_bt.bt_target_cooltemp = 5.0
        mock_bt.bt_min_temp = 5.0
        mock_bt.bt_target_temp_step = 0.5
        _bind_cooler_hvac_mode(mock_bt)

        old_state = _make_state(
            attributes={"temperature": 5.0, "current_temperature": 18.0}
        )
        new_state = _make_state(
            attributes={"temperature": 22.0, "current_temperature": 18.0}
        )
        trv_state = _make_state(
            state_str="heat",
            attributes={"current_temperature": 18.0, "temperature": 22.0},
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
            "reported setpoint 22.00 does not clear the cooling target 5.00, "
            "keeping 5.00" in caplog.text
        )
        levels = {
            record.levelno
            for record in caplog.records
            if "does not clear the cooling target" in record.getMessage()
        }
        assert levels == {logging.INFO}
        assert "to stay below cooling target" not in caplog.text


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
        """_main_change=True should request a control cycle."""
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

        mock_bt.control_queue_task.put_nowait.assert_called_once()
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
        mock_bt.control_queue_task.put_nowait.assert_not_called()


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

    def test_unsupported_mode_suppresses_system_mode_but_keeps_setpoint(self, mock_bt):
        """A mode the device does not offer drops out of the payload.

        The real mode_remap runs here; the setpoint must survive the
        suppressed mode write.
        """
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

    def test_off_without_off_in_mode_list_still_substitutes_min_temp(self, mock_bt):
        """OFF stays exempt from the clamp so the min-temp branch fires."""
        mock_bt.real_trvs[ENTITY_ID].hvac_modes = [HVACMode.AUTO, HVACMode.HEAT]
        mock_bt.real_trvs[ENTITY_ID].current_temperature = 18.0

        with patch(
            "custom_components.better_thermostat.events.trv.calculate_calibration_local",
            return_value=0.0,
        ):
            result = convert_outbound_states(mock_bt, ENTITY_ID, HVACMode.OFF)

        assert result == {
            "temperature": 5.0,
            "local_temperature": 18.0,
            "system_mode": None,
            "local_temperature_calibration": 0.0,
        }

    def test_off_with_no_off_system_mode_flag_still_substitutes_min_temp(self, mock_bt):
        """The no_off_system_mode path is unaffected by the clamp."""
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
    # climate entities publish no unit attribute, so every temperature read off
    # a TRV state resolves through the system unit.
    bt.hass.config.units.temperature_unit = UnitOfTemperature.CELSIUS
    bt.device_name = "Grouped Thermostat"
    bt.bt_hvac_mode = bt_hvac_mode
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
        """A single-TRV instance adopts its only valve's off report."""
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

        The cool target sits on ``bt_min_temp``, so the cap has no legal value
        below it and the ordering fallback has to separate the two targets.
        """
        trigger, other1, other2 = GRP_IDS
        bt = _make_group_bt(GRP_IDS, bt_hvac_mode=HVACMode.HEAT_COOL)
        bt.cooler_entity_id = "climate.ac"
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
        mock_bt.bt_hvac_mode = HVACMode.HEAT_COOL
        mock_bt.hvac_mode = HVACMode.HEAT_COOL
        mock_bt.bt_target_temp = 20.0
        mock_bt.bt_target_cooltemp = 24.0
        mock_bt._cooler_last_sent = {"temperature": (24.0, 0.0)}
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
    async def _report(bt, *, device_mode, reported_temp, previous_temp):
        """Drive one device report through the real TRV handler."""
        old_state = _make_state(
            state_str=device_mode,
            attributes={"temperature": previous_temp, "current_temperature": 22.0},
        )
        new_state = _make_state(
            state_str=device_mode,
            attributes={"temperature": reported_temp, "current_temperature": 22.0},
        )
        # trigger_trv_change reads the state machine rather than the event's
        # new_state for the device's own mode, so both carry the report.
        bt.hass.states.get.return_value = new_state
        # BT already holds the mode and the internal temperature the device
        # reports, so the report under test carries a setpoint and nothing
        # else, and a requested control cycle is the setpoint's doing.
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
        shared_bt.control_queue_task.put_nowait.assert_not_called()

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
        shared_bt.control_queue_task.put_nowait.assert_called_once()

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
        shared_bt.control_queue_task.put_nowait.assert_called_once()

    @pytest.mark.asyncio
    async def test_shared_entity_follows_the_latch_while_the_reported_mode_lags(
        self, shared_bt
    ):
        """A cooling setpoint written before its mode is still cooling's.

        The cooling channel writes the setpoint first and the mode second, so
        the report of that setpoint arrives while the device still names the
        mode it is leaving.
        """
        shared_bt._cooler_last_sent = {"hvac_mode_decided": HVACMode.COOL}

        await self._report(
            shared_bt, device_mode="heat", reported_temp=26.0, previous_temp=20.0
        )

        assert shared_bt.bt_target_cooltemp == 26.0
        assert shared_bt.bt_target_temp == 20.0
        shared_bt.control_queue_task.put_nowait.assert_called_once()

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
        mock_bt._cooler_last_sent = {"temperature": (24.0, 0.0)}
        mock_bt.bt_max_temp = 30.0
        mock_bt.real_trvs[ENTITY_ID].last_temperature = 19.0
        mock_bt.real_trvs[ENTITY_ID].max_temp = 30.0

        await self._report(
            mock_bt, device_mode="heat", reported_temp=24.0, previous_temp=19.0
        )

        assert mock_bt.bt_target_temp == 23.5
        mock_bt.control_queue_task.put_nowait.assert_called_once()
