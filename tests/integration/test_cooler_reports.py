"""What a separate cooler reports, and which of it reaches the cooling target.

The cooler's own controls can move the cooling target: a press on the air
conditioner's remote or in its app is the user's choice like one on the
thermostat. Not everything a cooler publishes is such a press, though. An air
conditioner that is off publishes whatever its integration shows for that
state, Tado for instance its 5 °C minimum, and a unit that holds whole degrees
answers a write of 22.5 °C with 22 °C, often from a poll that arrives well
after the write. Either of those taken as the cooling target would move the
room the user asked for.

The devices run through the real climate services, and every report that
does not come back from a write of the thermostat's own carries a context of
its own, the way a cloud poll or a press on the device does.
"""

import asyncio
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN, HVACMode
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE
from homeassistant.core import Context
from homeassistant.util import dt as dt_util
import pytest

from .conftest import (
    BT_ENTITY,
    COOLER_RESEND,
    WRITE_BUDGET,
    SimulatedClimate,
    make_entry,
    profile_id,
    set_room_sensor,
    setup_entry,
    wait_for_startup,
)
from .device_profiles import ROOM_AC_COOLER, SEPARATE_COOLER

TADO_LIKE_COOLER = replace(
    ROOM_AC_COOLER, name="tado_like_cooler", off_target_temperature=5.0
)
"""An air conditioner that publishes a 5 °C placeholder while it is off."""

HEATER_WITH_A_TADO_LIKE_COOLER = replace(
    SEPARATE_COOLER, name="heater_with_a_tado_like_cooler", cooler=TADO_LIKE_COOLER
)

HEAT_TARGET = 20.0
COOL_TARGET = 24.0


async def _settle(hass, rounds: int = 120) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)
        await hass.async_block_till_done()


async def _call(hass, service, data):
    """Drive one climate service call on the thermostat to completion."""
    await hass.services.async_call(
        CLIMATE_DOMAIN, service, {ATTR_ENTITY_ID: BT_ENTITY} | data, blocking=True
    )
    await _settle(hass)


async def _room(hass, bt, temperature: float) -> None:
    """Let the room sensor report ``temperature`` and the following cycle run."""
    bt.last_external_sensor_change = dt_util.now() - timedelta(hours=1)
    set_room_sensor(hass, temperature)
    await _settle(hass)


async def _cooling(hass, scenario):
    """Start a thermostat on ``scenario`` and bring its cooler into cooling."""
    set_room_sensor(hass, 22.0)
    entry = make_entry(scenario)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    await _call(hass, "set_hvac_mode", {"hvac_mode": HVACMode.HEAT_COOL})
    await _call(
        hass,
        "set_temperature",
        {"target_temp_low": HEAT_TARGET, "target_temp_high": COOL_TARGET},
    )
    await _room(hass, bt, 27.0)
    return bt


def _report(hass, cooler: SimulatedClimate) -> None:
    """Publish the cooler's state under a context that is not the thermostat's."""
    cooler.async_set_context(Context())
    cooler.async_write_ha_state()


@pytest.mark.parametrize(
    "device_role", [HEATER_WITH_A_TADO_LIKE_COOLER], indirect=True, ids=profile_id
)
async def test_a_first_start_with_the_cooler_off_takes_the_preset_cool_target(
    hass, device_role
):
    """An air conditioner that is off at the first start seeds nothing.

    It publishes its 5 °C placeholder, which raised above the heating target
    would leave a cooling target half a degree above it. The preset's cooling
    temperature is taken instead.
    """
    assert hass.states.get(device_role.cooler.entity_id).attributes[
        ATTR_TEMPERATURE
    ] == (5.0)
    set_room_sensor(hass, 22.0)
    entry = make_entry(device_role.scenario)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)

    assert bt.cool_target_temperature == COOL_TARGET


@pytest.mark.parametrize(
    "device_role", [HEATER_WITH_A_TADO_LIKE_COOLER], indirect=True, ids=profile_id
)
async def test_an_air_conditioner_switched_off_in_its_app_keeps_the_cool_target(
    hass, device_role
):
    """Switching the unit off in its app does not move the cooling target.

    The off report carries the 5 °C placeholder. Taken as a press, it would
    pull the cooling target down onto the heating target, and the room would be
    cooled to just above 20 °C instead of 24 °C.
    """
    cooler = device_role.cooler
    with patch(WRITE_BUDGET, 0.0), patch(COOLER_RESEND, 0.0):
        bt = await _cooling(hass, device_role.scenario)
        assert hass.states.get(cooler.entity_id).state == HVACMode.COOL

        cooler._attr_hvac_mode = HVACMode.OFF
        _report(hass, cooler)
        await _settle(hass)
        assert hass.states.get(cooler.entity_id).attributes[ATTR_TEMPERATURE] == 5.0
        cooler.set_hvac_mode_calls.clear()
        await _room(hass, bt, 23.0)

    assert bt.cool_target_temperature == COOL_TARGET
    assert hass.states.get(BT_ENTITY).attributes["target_temp_high"] == COOL_TARGET
    assert HVACMode.COOL not in cooler.set_hvac_mode_calls
    assert hass.states.get(cooler.entity_id).state == HVACMode.OFF


@pytest.mark.parametrize(
    "device_role", [HEATER_WITH_A_TADO_LIKE_COOLER], indirect=True, ids=profile_id
)
async def test_a_late_off_report_after_the_thermostat_switched_off_keeps_the_target(
    hass, device_role
):
    """The cloud confirming the thermostat's own off command moves nothing.

    A cloud integration publishes the command it was given on its next poll,
    which can arrive long after the thermostat sent it and under a context of
    its own. That report carries the 5 °C placeholder as well.
    """
    cooler = device_role.cooler
    with patch(WRITE_BUDGET, 0.0), patch(COOLER_RESEND, 0.0):
        bt = await _cooling(hass, device_role.scenario)
        acknowledged: list[HVACMode] = []

        async def acknowledge_on_the_next_poll(hvac_mode) -> None:
            cooler.set_hvac_mode_calls.append(str(hvac_mode))
            acknowledged.append(hvac_mode)

        cooler.async_set_hvac_mode = acknowledge_on_the_next_poll
        await _room(hass, bt, 23.0)
        assert acknowledged == [HVACMode.OFF]

        cooler._attr_hvac_mode = HVACMode.OFF
        _report(hass, cooler)
        await _settle(hass)
        acknowledged.clear()
        await _room(hass, bt, 22.5)
        await _room(hass, bt, 23.0)

    assert bt.cool_target_temperature == COOL_TARGET
    assert HVACMode.COOL not in acknowledged


def _hold_whole_degrees(hass, cooler: SimulatedClimate, answer: str) -> None:
    """Make the cooler hold a setpoint write in whole degrees.

    The unit publishes a step of 0.5 °C but holds the written setpoint
    truncated to the degree. With ``answer`` ``"in_call"`` it publishes that
    setpoint before the call returns, the way an integration that refreshes
    its device after a command does, under the context of the call. With
    ``"later_poll"`` it publishes nothing in answer to the call, and its next
    poll reports the setpoint under a context of its own.
    """
    apply_the_write = cooler.async_set_temperature

    async def hold_whole_degrees(**kwargs) -> None:
        written = kwargs.get(ATTR_TEMPERATURE)
        if not isinstance(written, float):
            await apply_the_write(**kwargs)
            return
        cooler.set_temperature_calls.append(written)
        if answer == "in_call":
            cooler._attr_target_temperature = float(int(written))
            cooler.async_write_ha_state()
            return

        def poll() -> None:
            cooler._attr_target_temperature = float(int(written))
            _report(hass, cooler)

        hass.loop.call_soon(poll)

    cooler.async_set_temperature = hold_whole_degrees


@pytest.mark.parametrize("answer", ["in_call", "later_poll"])
@pytest.mark.parametrize(
    "device_role", [SEPARATE_COOLER], indirect=True, ids=profile_id
)
async def test_a_whole_degree_answer_to_a_half_degree_write_is_not_a_press(
    hass, device_role, answer
):
    """The setpoint a coarser device holds for a write is that write coming back.

    The unit is already cooling when the cooling target moves to 22.5 °C, so
    the report of the 22 °C it holds is a report in cooling mode. The cooling
    target stays 22.5 °C. A press on the unit after it has answered is still
    read as one: one degree up from the 22 °C it holds lands half a degree
    from the write and is the user's.
    """
    cooler = device_role.cooler
    _hold_whole_degrees(hass, cooler, answer)
    with patch(WRITE_BUDGET, 0.0), patch(COOLER_RESEND, 0.0):
        bt = await _cooling(hass, device_role.scenario)
        assert hass.states.get(cooler.entity_id).state == HVACMode.COOL
        await _call(
            hass,
            "set_temperature",
            {"target_temp_low": HEAT_TARGET, "target_temp_high": 22.5},
        )
        assert 22.5 in cooler.set_temperature_calls
        assert hass.states.get(cooler.entity_id).attributes[ATTR_TEMPERATURE] == 22.0
        for temperature in (27.2, 27.4, 27.1):
            await _room(hass, bt, temperature)

        assert bt.cool_target_temperature == 22.5
        assert hass.states.get(BT_ENTITY).attributes["target_temp_high"] == 22.5

        cooler._attr_target_temperature = 23.0
        _report(hass, cooler)
        await _settle(hass)

    assert bt.cool_target_temperature == 23.0


@pytest.mark.parametrize(
    "device_role", [SEPARATE_COOLER], indirect=True, ids=profile_id
)
async def test_a_press_right_after_a_write_is_adopted(hass, device_role):
    """A one-step press after the unit took a write is the user's.

    The unit holds the half-degree grid it publishes and reports the write
    back while the thermostat's call is still running. A press half a degree
    up from there lands within the distance a coarser device may answer a
    write with, but the unit has already answered this one.
    """
    cooler = device_role.cooler
    with patch(WRITE_BUDGET, 0.0), patch(COOLER_RESEND, 0.0):
        bt = await _cooling(hass, device_role.scenario)
        await _call(
            hass,
            "set_temperature",
            {"target_temp_low": HEAT_TARGET, "target_temp_high": 23.0},
        )
        assert 23.0 in cooler.set_temperature_calls
        assert hass.states.get(cooler.entity_id).attributes[ATTR_TEMPERATURE] == 23.0

        cooler._attr_target_temperature = 23.5
        _report(hass, cooler)
        await _settle(hass)

    assert bt.cool_target_temperature == 23.5
    assert hass.states.get(BT_ENTITY).attributes["target_temp_high"] == 23.5
    assert hass.states.get(cooler.entity_id).attributes[ATTR_TEMPERATURE] == 23.5


@pytest.mark.parametrize(
    "device_role", [SEPARATE_COOLER], indirect=True, ids=profile_id
)
async def test_a_press_back_to_the_answer_of_an_earlier_write_is_adopted(
    hass, device_role
):
    """After a press, the answer to the write before it is no answer any more.

    The whole-degree unit answers a write of 22.5 °C with 22 °C. The user
    presses up to 23 °C and then back down to 22 °C. That second press is
    the user's as well; the thermostat does not write 23 °C over it.
    """
    cooler = device_role.cooler
    _hold_whole_degrees(hass, cooler, "later_poll")
    with patch(WRITE_BUDGET, 0.0), patch(COOLER_RESEND, 0.0):
        bt = await _cooling(hass, device_role.scenario)
        await _call(
            hass,
            "set_temperature",
            {"target_temp_low": HEAT_TARGET, "target_temp_high": 22.5},
        )
        assert hass.states.get(cooler.entity_id).attributes[ATTR_TEMPERATURE] == 22.0
        await _room(hass, bt, 27.2)
        assert bt.cool_target_temperature == 22.5

        cooler._attr_target_temperature = 23.0
        _report(hass, cooler)
        await _settle(hass)
        assert bt.cool_target_temperature == 23.0
        await _room(hass, bt, 27.4)

        cooler.set_temperature_calls.clear()
        cooler._attr_target_temperature = 22.0
        _report(hass, cooler)
        await _settle(hass)
        await _room(hass, bt, 27.1)

    assert bt.cool_target_temperature == 22.0
    assert cooler.set_temperature_calls == []
    assert hass.states.get(cooler.entity_id).attributes[ATTR_TEMPERATURE] == 22.0


FAN_MODE_COOLER = replace(
    ROOM_AC_COOLER,
    name="fan_mode_cooler",
    hvac_modes=(HVACMode.COOL, HVACMode.FAN_ONLY, HVACMode.OFF),
    hvac_mode=HVACMode.FAN_ONLY,
    target_temperature=18.0,
)
"""An air conditioner running its fan, with an 18 °C setpoint from earlier."""

HEATER_WITH_A_FAN_MODE_COOLER = replace(
    SEPARATE_COOLER, name="heater_with_a_fan_mode_cooler", cooler=FAN_MODE_COOLER
)


@pytest.mark.parametrize(
    "device_role", [HEATER_WITH_A_FAN_MODE_COOLER], indirect=True, ids=profile_id
)
async def test_a_first_start_with_the_cooler_in_fan_mode_takes_the_preset(
    hass, device_role
):
    """Only a cooling mode's setpoint seeds the cooling target at the first start."""
    set_room_sensor(hass, 22.0)
    entry = make_entry(device_role.scenario)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)

    assert bt.cool_target_temperature == COOL_TARGET


@pytest.mark.parametrize(
    "device_role", [HEATER_WITH_A_FAN_MODE_COOLER], indirect=True, ids=profile_id
)
async def test_a_cooler_back_in_fan_mode_seeds_the_preset_cool_target(
    hass, device_role
):
    """A cooler that was away at the start and returns in fan mode seeds nothing.

    The setpoint a unit shows while it runs only its fan belongs to no
    cooling. The preset's cooling temperature is taken instead.
    """
    cooler = device_role.cooler
    cooler.set_available(False)
    set_room_sensor(hass, 22.0)
    entry = make_entry(device_role.scenario)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    assert bt.cool_target_temperature is None

    cooler.async_set_context(Context())
    cooler.set_available(True)
    await _settle(hass)

    assert bt.cool_target_temperature == COOL_TARGET
