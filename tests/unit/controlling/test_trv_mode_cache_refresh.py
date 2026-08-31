"""What the per-TRV mode cache holds once a control cycle has ended.

A control cycle is a window in which Better Thermostat drops inbound TRV
events, and it stays open for seconds while the adapters wait for their writes
to be confirmed. Each test here drives one cycle with a device that changes
mode inside that window and states what the cache owes the user afterwards.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import STATE_UNAVAILABLE, UnitOfTemperature
from homeassistant.core import State
import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.events.trv import trigger_trv_change
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.const import (
    CONF_HOMEMATICIP,
    CalibrationMode,
    CalibrationType,
)
from custom_components.better_thermostat.utils.controlling import control_queue

ENTITY_ID = "climate.test_trv"

# The mode list of an ordinary radiator valve.
OFFERED_MODES = [HVACMode.OFF, HVACMode.HEAT]


def _reported_state(mode: str, setpoint: float = 19.0) -> State:
    """Build the state one TRV publishes."""
    return State(
        ENTITY_ID,
        mode,
        attributes={
            "current_temperature": 18.0,
            "temperature": setpoint,
            "hvac_modes": OFFERED_MODES,
        },
    )


@pytest.fixture
def reported_states() -> dict[str, State]:
    """Hold what each device publishes, so a test can change it mid-cycle."""
    return {ENTITY_ID: _reported_state("heat")}


@pytest.fixture
def thermostat(reported_states):
    """Build a Better Thermostat driving one TRV that heats."""
    bt = MagicMock()
    bt.hass = MagicMock()
    # Climate entities publish no unit attribute, so every temperature read off
    # a TRV state resolves through the system unit.
    bt.hass.config.units.temperature_unit = UnitOfTemperature.CELSIUS
    bt.hass.states.get.side_effect = reported_states.get
    bt.device_name = "Test Thermostat"
    bt.bt_hvac_mode = HVACMode.HEAT
    bt.hvac_mode = HVACMode.HEAT
    bt.bt_target_temp = 19.0
    bt.bt_min_temp = 5.0
    bt.bt_max_temp = 30.0
    bt.bt_target_cooltemp = 25.0
    bt.bt_target_temp_step = 0.5
    bt.cur_temp = 18.0
    bt.tolerance = 0.3
    bt.window_open = False
    bt.contact_open = False
    bt.startup_running = False
    bt.bt_update_lock = False
    bt.in_maintenance = False
    bt.ignore_states = False
    bt.cooler_entity_id = None
    bt.context = MagicMock()  # unique context so != event.context
    bt.async_write_ha_state = MagicMock()
    bt.calculate_heating_power = AsyncMock()
    bt.calculate_heat_loss = AsyncMock()
    bt.all_trvs = [{"advanced": {CONF_HOMEMATICIP: False}}]
    bt._enforce_cool_above_heat = lambda **kwargs: (
        BetterThermostat._enforce_cool_above_heat(bt, **kwargs)
    )
    bt._clamp_inbound_heat_target = lambda value: (
        BetterThermostat._clamp_inbound_heat_target(bt, value)
    )
    bt.real_trvs = {
        ENTITY_ID: Trv.from_legacy_dict(
            ENTITY_ID,
            {
                "hvac_mode": "heat",
                "hvac_modes": OFFERED_MODES,
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


async def _run_one_cycle(thermostat, reported_states, published_inside) -> None:
    """Drive one control cycle, with the device publishing inside it.

    ``published_inside`` is the state the TRV publishes while the cycle holds
    it, which is the window in which an event from that TRV is dropped.
    ``None`` stands for a device that publishes no state at all.
    """
    queue = asyncio.Queue()
    thermostat.control_queue_task = queue
    await queue.put(thermostat)

    async def _control_trv(*args, **kwargs) -> bool:
        assert thermostat.ignore_states is True
        if published_inside is None:
            reported_states.pop(ENTITY_ID)
        else:
            reported_states[ENTITY_ID] = published_inside
        return True

    with patch(
        "custom_components.better_thermostat.utils.controlling.control_trv",
        new=_control_trv,
    ):
        cycle = asyncio.create_task(control_queue(thermostat))
        try:
            await asyncio.wait_for(queue.join(), timeout=5)
        finally:
            cycle.cancel()
            try:
                await cycle
            except asyncio.CancelledError:
                pass


def _press_setpoint(thermostat, reported_states, setpoint: float):
    """Build the event a TRV sends when its knob was turned to ``setpoint``."""
    old_state = reported_states[ENTITY_ID]
    new_state = _reported_state(old_state.state, setpoint=setpoint)
    reported_states[ENTITY_ID] = new_state

    event = MagicMock()
    event.data = {
        "old_state": old_state,
        "new_state": new_state,
        "entity_id": ENTITY_ID,
    }
    event.context = MagicMock()  # differs from thermostat.context
    return event


class TestModeCacheAfterACycle:
    """The cached mode a TRV reports, once the cycle that hid it has ended."""

    @pytest.mark.asyncio
    async def test_a_mode_reported_inside_the_cycle_reaches_the_cache(
        self, thermostat, reported_states
    ):
        """A TRV that comes on during a cycle is cached as heating."""
        thermostat.real_trvs[ENTITY_ID].hvac_mode = "off"
        reported_states[ENTITY_ID] = _reported_state("off")

        await _run_one_cycle(thermostat, reported_states, _reported_state("heat"))

        assert thermostat.real_trvs[ENTITY_ID].hvac_mode == "heat"

    @pytest.mark.asyncio
    async def test_a_setpoint_pressed_after_the_cycle_is_adopted(
        self, thermostat, reported_states
    ):
        """A knob turned after such a cycle moves the heating target.

        This is the half the user feels: the setpoint guard turns away a press
        from a device it holds as switched off, so a cache left behind by the
        cycle silently drops the press.
        """
        thermostat.real_trvs[ENTITY_ID].hvac_mode = "off"
        reported_states[ENTITY_ID] = _reported_state("off")

        await _run_one_cycle(thermostat, reported_states, _reported_state("heat"))
        await trigger_trv_change(
            thermostat, _press_setpoint(thermostat, reported_states, 22.0)
        )

        assert thermostat.bt_target_temp == 22.0

    @pytest.mark.asyncio
    async def test_the_entity_keeps_the_mode_it_was_left_with(
        self, thermostat, reported_states
    ):
        """Switching a TRV off during a cycle leaves the room heating.

        The cache follows the device, the entity does not: a mode reported
        while the handler stood down was not adopted as user intent, and the
        end of the cycle is too late to read it as one.
        """
        await _run_one_cycle(thermostat, reported_states, _reported_state("off"))

        assert thermostat.real_trvs[ENTITY_ID].hvac_mode == "off"
        assert thermostat.bt_hvac_mode == HVACMode.HEAT

    @pytest.mark.asyncio
    async def test_an_unavailable_device_keeps_its_cached_mode(
        self, thermostat, reported_states
    ):
        """A TRV that drops off the network keeps the mode it last reported."""
        await _run_one_cycle(
            thermostat, reported_states, State(ENTITY_ID, STATE_UNAVAILABLE)
        )

        assert thermostat.real_trvs[ENTITY_ID].hvac_mode == "heat"

    @pytest.mark.asyncio
    async def test_a_device_that_publishes_nothing_keeps_its_cached_mode(
        self, thermostat, reported_states
    ):
        """A TRV with no state at all keeps the mode it last reported."""
        await _run_one_cycle(thermostat, reported_states, None)

        assert thermostat.real_trvs[ENTITY_ID].hvac_mode == "heat"

    @pytest.mark.asyncio
    async def test_a_child_locked_device_keeps_its_cached_mode(
        self, thermostat, reported_states
    ):
        """A child lock holds the cache as firmly as it holds the handler.

        The lock exists so that what happens at the device does not reach
        Better Thermostat, and the end of a cycle is not a way around it.
        """
        thermostat.real_trvs[ENTITY_ID].advanced["child_lock"] = True

        await _run_one_cycle(thermostat, reported_states, _reported_state("off"))

        assert thermostat.real_trvs[ENTITY_ID].hvac_mode == "heat"
