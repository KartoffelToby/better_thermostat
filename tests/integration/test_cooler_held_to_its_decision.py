"""A cooler of its own is held to the mode Better Thermostat decided on.

The cooling channel decides COOL or OFF on every control cycle, and the
cooler is expected to hold that decision between cycles. A cooler that comes
back from an outage in the mode it had before, that its own remote switches,
or that lost the command switching it off holds another mode until a cycle
puts it back, and nothing in the room has to move for that cycle to come.

A cooler held off receives no setpoint: several integrations switch an air
conditioner that is off into cooling on any setpoint write. A cooler switched
on receives its mode before its setpoint.

The room runs the production default calibration, which registers no periodic
control cycle of its own, so a cycle that comes is one these paths asked for.
"""

import asyncio
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from homeassistant.components.climate import (
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_TEMPERATURE,
    HVACMode,
)
from homeassistant.const import ATTR_ENTITY_ID, EVENT_CALL_SERVICE
from homeassistant.core import Context
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.better_thermostat.utils.scheduler import request_control_cycle

from .conftest import (
    BT_ENTITY,
    COOLER_RESEND,
    WINDOW_ID,
    WRITE_BUDGET,
    build_devices,
    make_entry,
    mode_commands,
    set_room_sensor,
    setpoint_commands,
    setup_entry,
    wait_for_startup,
)
from .device_profiles import COOLER_ID, ROOM_AC_COOLER, SEPARATE_COOLER

ROOM = replace(
    SEPARATE_COOLER,
    trv=replace(SEPARATE_COOLER.trv, calibration_mode="heating_power_calibration"),
)
"""A heated room with an air conditioner, on the default calibration."""

# Above the cooler's own setpoint of 24 °C by more than any tolerance, so the
# cooling channel runs the unit whenever nothing holds it off.
WARM_ROOM = 27.0


async def _settle(hass, rounds=50):
    for _ in range(rounds):
        await asyncio.sleep(0)
        await hass.async_block_till_done()


async def _started_cooling(hass, *, with_window=False):
    """Start a thermostat in a warm room and return it with its cooler running."""
    _, cooler = await build_devices(hass, ROOM.trv, ROOM_AC_COOLER)
    set_room_sensor(hass, WARM_ROOM)
    if with_window:
        hass.states.async_set(WINDOW_ID, "off")
    entry = make_entry(ROOM, with_window=with_window)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    await _call(hass, "set_hvac_mode", {"hvac_mode": HVACMode.HEAT_COOL})
    assert hass.states.get(COOLER_ID).state == HVACMode.COOL
    return bt, cooler


async def _call(hass, service, data):
    await hass.services.async_call(
        CLIMATE_DOMAIN, service, {ATTR_ENTITY_ID: BT_ENTITY} | data, blocking=True
    )
    await _settle(hass)


async def _let_minutes_pass(hass, minutes):
    """Fire every minute's timers in turn, the way the clock would."""
    start = dt_util.utcnow()
    for minute in range(1, minutes + 1):
        async_fire_time_changed(hass, start + timedelta(minutes=minute))
        await _settle(hass, 20)


def _on_its_own(cooler):
    """Detach the cooler from the last command Better Thermostat sent it.

    The entity still holds that command's context, and a state written under
    it is read as the command's echo. A device that reports by itself does so
    under a new one.
    """
    cooler.async_set_context(Context())


def _switch_from_its_own_controls(cooler, hvac_mode):
    """Change the cooler's mode the way its remote or its app would."""
    _on_its_own(cooler)
    cooler._attr_hvac_mode = hvac_mode
    cooler.async_write_ha_state()


async def test_a_cooler_back_from_an_outage_in_cool_is_switched_off_while_the_room_is_off(
    hass,
):
    """The room was switched off while the cooler was away.

    The cycle the switch-off ran could not reach the cooler, and the cooler
    comes back in the COOL it had before.
    """
    with patch(WRITE_BUDGET, 0.0), patch(COOLER_RESEND, 0.0):
        _, cooler = await _started_cooling(hass)
        _on_its_own(cooler)
        cooler.set_available(False)
        await _settle(hass)
        await _call(hass, "set_hvac_mode", {"hvac_mode": HVACMode.OFF})

        _on_its_own(cooler)
        cooler.set_available(True)
        await _settle(hass)

        assert hass.states.get(COOLER_ID).state == HVACMode.OFF


async def test_a_cooler_back_from_an_outage_in_cool_is_switched_off_while_a_window_is_open(
    hass,
):
    """The window was opened while the cooler was away."""
    with patch(WRITE_BUDGET, 0.0), patch(COOLER_RESEND, 0.0):
        bt, cooler = await _started_cooling(hass, with_window=True)
        _on_its_own(cooler)
        cooler.set_available(False)
        await _settle(hass)
        hass.states.async_set(WINDOW_ID, "on")
        await _let_minutes_pass(hass, 3)
        assert bt.window_open is True

        _on_its_own(cooler)
        cooler.set_available(True)
        await _settle(hass)

        assert hass.states.get(COOLER_ID).state == HVACMode.OFF


async def test_a_cooler_its_remote_switches_on_while_the_room_is_off_is_switched_off(
    hass,
):
    """A mode the cooler takes from its own controls is put back on the decision."""
    with patch(WRITE_BUDGET, 0.0), patch(COOLER_RESEND, 0.0):
        _, cooler = await _started_cooling(hass)
        await _call(hass, "set_hvac_mode", {"hvac_mode": HVACMode.OFF})
        assert hass.states.get(COOLER_ID).state == HVACMode.OFF

        _switch_from_its_own_controls(cooler, HVACMode.COOL)
        await _settle(hass)

        assert hass.states.get(COOLER_ID).state == HVACMode.OFF


async def test_a_lost_switch_off_is_sent_again_by_the_reconciler(hass):
    """A cooler that dropped the OFF command reports nothing new.

    No state change arrives to ask for a cycle, so the periodic reconciliation
    is what finds the cooler still cooling and sends the command again.
    """
    with patch(WRITE_BUDGET, 0.0), patch(COOLER_RESEND, 0.0):
        _, cooler = await _started_cooling(hass)
        applied = type(cooler).async_set_hvac_mode
        lost: list[HVACMode] = []

        async def lose_the_first_write(hvac_mode):
            if not lost:
                lost.append(hvac_mode)
                cooler.set_hvac_mode_calls.append(str(hvac_mode))
                return
            await applied(cooler, hvac_mode)

        with patch.object(cooler, "async_set_hvac_mode", lose_the_first_write):
            await _call(hass, "set_hvac_mode", {"hvac_mode": HVACMode.OFF})
            assert lost == [HVACMode.OFF]
            assert hass.states.get(COOLER_ID).state == HVACMode.COOL

            await _let_minutes_pass(hass, 6)

        assert hass.states.get(COOLER_ID).state == HVACMode.OFF


async def test_a_cooler_held_off_by_the_room_mode_receives_no_setpoint(hass):
    """A cooling target set while the room is off stays off the cooler.

    The next control cycle holds the cooler off and leaves its setpoint alone.
    """
    with patch(WRITE_BUDGET, 0.0), patch(COOLER_RESEND, 0.0):
        bt, _ = await _started_cooling(hass)
        await _call(hass, "set_hvac_mode", {"hvac_mode": HVACMode.OFF})
        assert hass.states.get(COOLER_ID).state == HVACMode.OFF
        await _call(
            hass, "set_temperature", {"target_temp_low": 20.0, "target_temp_high": 22.0}
        )
        dispatched = async_capture_events(hass, EVENT_CALL_SERVICE)

        request_control_cycle(bt)
        await _settle(hass)

        assert setpoint_commands(dispatched, COOLER_ID) == []
        assert hass.states.get(COOLER_ID).state == HVACMode.OFF


async def test_a_cooler_held_off_by_a_cool_room_receives_no_setpoint(hass):
    """A cooling target above a room that needs no cooling stays off the cooler."""
    with patch(WRITE_BUDGET, 0.0), patch(COOLER_RESEND, 0.0):
        await build_devices(hass, ROOM.trv, ROOM_AC_COOLER)
        set_room_sensor(hass, 22.0)
        entry = make_entry(ROOM)
        await setup_entry(hass, entry)
        await wait_for_startup(hass, entry)
        await _call(hass, "set_hvac_mode", {"hvac_mode": HVACMode.HEAT_COOL})
        assert hass.states.get(COOLER_ID).state == HVACMode.OFF
        dispatched = async_capture_events(hass, EVENT_CALL_SERVICE)

        await _call(
            hass, "set_temperature", {"target_temp_low": 20.0, "target_temp_high": 23.0}
        )

        assert setpoint_commands(dispatched, COOLER_ID) == []
        assert hass.states.get(COOLER_ID).state == HVACMode.OFF


async def test_a_cooler_switched_on_receives_its_mode_before_its_setpoint(hass):
    """Switching the room back on starts the cooler, then sets its target."""
    with patch(WRITE_BUDGET, 0.0), patch(COOLER_RESEND, 0.0):
        _, cooler = await _started_cooling(hass)
        await _call(hass, "set_hvac_mode", {"hvac_mode": HVACMode.OFF})
        await _call(
            hass, "set_temperature", {"target_temp_low": 20.0, "target_temp_high": 22.0}
        )
        dispatched = async_capture_events(hass, EVENT_CALL_SERVICE)

        await _call(hass, "set_hvac_mode", {"hvac_mode": HVACMode.HEAT_COOL})

        cooler_commands = [
            event.data["service"]
            for event in dispatched
            if event.data["domain"] == CLIMATE_DOMAIN
            and event.data["service_data"].get(ATTR_ENTITY_ID) == COOLER_ID
        ]
        assert cooler_commands == [SERVICE_SET_HVAC_MODE, SERVICE_SET_TEMPERATURE]
        assert mode_commands(dispatched, COOLER_ID) == [HVACMode.COOL]
        assert hass.states.get(COOLER_ID).state == HVACMode.COOL
        assert hass.states.get(COOLER_ID).attributes["temperature"] == 22.0
