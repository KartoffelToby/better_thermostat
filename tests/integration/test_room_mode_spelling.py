"""A room that is on holds one mode, whichever way it was switched on.

Better Thermostat publishes a room with a cooler as ``heat_cool`` and a room
without one as ``heat``, and it drives each device in the mode that device
heats in. Behind both edges the room holds a single intent, "on", so every
way of switching it on (first start, the mode service, the temperature service
carrying a mode, a press at the device, a restart) ends in the same internal
mode, the same published state and the same command to the device.
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
from homeassistant.core import Context
from homeassistant.util import dt as dt_util
import pytest

from .conftest import (
    BT_ENTITY,
    COOLER_RESEND,
    WRITE_BUDGET,
    build_devices,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for_startup,
)
from .device_profiles import (
    DUAL_ROLE,
    DUAL_ROLE_AC,
    GENERIC_HEAT_TRV,
    HEAT_ONLY,
    SEPARATE_COOLER,
    RoleScenario,
)

HEAT_COOL_OFFERING_TRV_ROOM = replace(
    SEPARATE_COOLER,
    name="heat_cool_offering_trv_room",
    trv=replace(
        GENERIC_HEAT_TRV,
        name="heat_cool_offering_trv",
        hvac_modes=(HVACMode.OFF, HVACMode.HEAT, HVACMode.HEAT_COOL),
    ),
)
"""A room with a cooler whose radiator also offers ``heat_cool``.

A device that runs ``heat_cool`` runs its own thermostat against its own pair
of targets, so the radiator has to be driven in ``heat``.
"""

HEAT_COOL_OFFERING_DUAL_ROLE = replace(
    DUAL_ROLE,
    name="heat_cool_offering_dual_role",
    trv=replace(
        DUAL_ROLE_AC,
        name="heat_cool_offering_dual_role_ac",
        hvac_modes=(HVACMode.HEAT, HVACMode.COOL, HVACMode.HEAT_COOL, HVACMode.OFF),
    ),
)
"""One entity that heats and cools and also offers ``heat_cool``."""

SCENARIOS = [HEAT_COOL_OFFERING_TRV_ROOM, HEAT_COOL_OFFERING_DUAL_ROLE, HEAT_ONLY]


@pytest.fixture(autouse=True)
def _no_write_budget():
    """Let a write follow the previous one without waiting out the budget."""
    with patch(WRITE_BUDGET, 0.0):
        yield


async def _settle(hass, rounds: int = 120) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)
        await hass.async_block_till_done()


def _on_spelling(scenario: RoleScenario) -> HVACMode:
    """Return the mode the room publishes while it is on."""
    return HVACMode.HEAT if scenario.cooler_entity_id is None else HVACMode.HEAT_COOL


async def _start(hass, scenario: RoleScenario):
    devices = await build_devices(
        hass, *(p for p in (scenario.trv, scenario.cooler) if p is not None)
    )
    set_room_sensor(hass, 19.0)
    entry = make_entry(scenario)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    await _settle(hass)
    return entry, bt, devices[0]


async def _set_mode(hass, mode: HVACMode) -> None:
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_HVAC_MODE,
        {"entity_id": BT_ENTITY, "hvac_mode": mode},
        blocking=True,
    )
    await _settle(hass)


def _assert_on(hass, bt, device, scenario: RoleScenario) -> None:
    """The room holds "on", publishes its on spelling and heats the device."""
    assert bt.bt_hvac_mode == HVACMode.HEAT
    assert hass.states.get(BT_ENTITY).state == _on_spelling(scenario)
    assert device.hvac_mode == HVACMode.HEAT
    assert HVACMode.HEAT_COOL not in device.set_hvac_mode_calls


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
async def test_first_start_holds_the_room_on(hass, scenario):
    """A room that starts heating holds "on"."""
    _, bt, device = await _start(hass, scenario)

    _assert_on(hass, bt, device, scenario)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
async def test_the_mode_service_holds_the_room_on(hass, scenario):
    """Switching the room on through the mode service holds "on"."""
    _, bt, device = await _start(hass, scenario)
    await _set_mode(hass, HVACMode.OFF)

    await _set_mode(hass, _on_spelling(scenario))

    _assert_on(hass, bt, device, scenario)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
@pytest.mark.parametrize("spelling", [HVACMode.HEAT, HVACMode.HEAT_COOL])
async def test_either_on_spelling_holds_the_room_on(hass, scenario, spelling):
    """The entity's mode handler takes both on spellings as "on".

    The published mode list offers only one of them, but device actions and
    the entity's own callers reach the handler with either.
    """
    _, bt, device = await _start(hass, scenario)
    await _set_mode(hass, HVACMode.OFF)

    await bt.async_set_hvac_mode(spelling)
    await _settle(hass)

    _assert_on(hass, bt, device, scenario)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
@pytest.mark.parametrize("spelling", [HVACMode.HEAT, HVACMode.HEAT_COOL])
async def test_the_temperature_service_with_a_mode_holds_the_room_on(
    hass, scenario, spelling
):
    """A temperature call that carries either on spelling holds "on"."""
    _, bt, device = await _start(hass, scenario)
    await _set_mode(hass, HVACMode.OFF)

    await bt.async_set_temperature(temperature=21.0, hvac_mode=spelling)
    await _settle(hass)

    _assert_on(hass, bt, device, scenario)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
async def test_a_press_at_the_device_holds_the_room_on(hass, scenario):
    """A device switched to heat at the device turns the room on and stays in heat."""
    _, bt, device = await _start(hass, scenario)
    await _set_mode(hass, HVACMode.OFF)
    device.set_hvac_mode_calls.clear()

    device._attr_hvac_mode = HVACMode.HEAT
    device.async_set_context(Context())
    device.async_write_ha_state()
    await _settle(hass)

    _assert_on(hass, bt, device, scenario)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
async def test_a_restart_holds_the_room_on_and_leaves_the_device_alone(hass, scenario):
    """A room restored from its published state holds "on" and sends no mode."""
    entry, _, device = await _start(hass, scenario)
    await _set_mode(hass, HVACMode.OFF)
    await _set_mode(hass, _on_spelling(scenario))
    device.set_hvac_mode_calls.clear()

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    bt = await wait_for_startup(hass, entry)
    await _settle(hass)

    _assert_on(hass, bt, device, scenario)
    assert device.set_hvac_mode_calls == []
    assert hass.states.get(BT_ENTITY).attributes.get("main_mode") == HVACMode.HEAT


async def test_a_heat_cool_offering_radiator_is_never_switched_to_heat_cool(hass):
    """Across every way of switching a cooler room, its radiator stays in heat.

    The room is switched off and on through the service, on at the radiator,
    on again through the service and restarted. A radiator that offers
    ``heat_cool`` would take that mode as an instruction to run its own
    thermostat, and the press at the radiator must stand as the user made it.
    """
    scenario = HEAT_COOL_OFFERING_TRV_ROOM
    entry, bt, device = await _start(hass, scenario)
    await _set_mode(hass, HVACMode.OFF)
    await _set_mode(hass, HVACMode.HEAT_COOL)
    await _set_mode(hass, HVACMode.OFF)
    device._attr_hvac_mode = HVACMode.HEAT
    device.async_set_context(Context())
    device.async_write_ha_state()
    await _settle(hass)
    await _set_mode(hass, HVACMode.HEAT_COOL)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    bt = await wait_for_startup(hass, entry)
    await _settle(hass)

    _assert_on(hass, bt, device, scenario)
    assert device.set_hvac_mode_calls == [HVACMode.OFF, HVACMode.HEAT, HVACMode.OFF]


async def _room_at(hass, bt, room: float) -> None:
    """Let the room sensor report ``room`` and the following cycle run."""
    bt.last_external_sensor_change = dt_util.now() - timedelta(hours=1)
    set_room_sensor(hass, room)
    await _settle(hass)


@pytest.mark.parametrize("path", ["first_start", "service", "knob", "restart"])
async def test_a_dual_role_device_cools_and_heats_whichever_way_the_room_is_on(
    hass, path
):
    """A device that is heater and cooler cools when due and heats when due.

    It is handed from one channel to the other in cool and heat, never in
    ``heat_cool``, which would make it run its own pair of targets.
    """
    scenario = HEAT_COOL_OFFERING_DUAL_ROLE
    with patch(COOLER_RESEND, 0.0):
        entry, bt, device = await _start(hass, scenario)
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {"entity_id": BT_ENTITY, "target_temp_low": 20.0, "target_temp_high": 24.0},
            blocking=True,
        )
        await _settle(hass)
        if path != "first_start":
            await _set_mode(hass, HVACMode.OFF)
        if path in ("service", "restart"):
            await _set_mode(hass, HVACMode.HEAT_COOL)
        if path == "knob":
            device._attr_hvac_mode = HVACMode.HEAT
            device.async_set_context(Context())
            device.async_write_ha_state()
            await _settle(hass)
        if path == "restart":
            assert await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()
            bt = await wait_for_startup(hass, entry)
            await _settle(hass)

        await _room_at(hass, bt, 27.0)
        assert (device.hvac_mode, device.target_temperature) == (HVACMode.COOL, 24.0)

        await _room_at(hass, bt, 22.0)
        assert device.hvac_mode == HVACMode.HEAT

        await _room_at(hass, bt, 27.0)
        assert (device.hvac_mode, device.target_temperature) == (HVACMode.COOL, 24.0)
        assert HVACMode.HEAT_COOL not in device.set_hvac_mode_calls
