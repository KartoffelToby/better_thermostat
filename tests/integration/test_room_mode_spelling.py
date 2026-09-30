"""A room that is on holds one mode, whichever way it was switched on.

Better Thermostat publishes a room with a cooler as ``heat_cool`` and a room
without one as ``heat``, and it drives each device in the mode that device
heats in. Behind both edges the room holds a single intent, "on", so every
way of switching it on (first start, the mode service, the temperature service
carrying a mode, a press at the device, a restart) ends in the same internal
mode, the same published state and the same command to the device.
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.components.climate import (
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_TEMPERATURE,
    HVACMode,
)
from homeassistant.core import Context
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    setup_test_component_platform,
)

from .conftest import (
    SENSOR_ID,
    TRV_ID,
    FakeTrvEntity,
    make_entry,
    setup_entry,
    wait_for_startup,
)

BT_ENTITY = "climate.bt_test"
COOLER_ID = "climate.fake_cooler"


class FakeCoolerEntity(FakeTrvEntity):
    """A separate cooler that cools and switches off."""

    _attr_name = "fake cooler"
    _attr_hvac_modes = [HVACMode.COOL, HVACMode.OFF]

    def __init__(self):
        """Start off, holding a cooling setpoint."""
        super().__init__()
        self._attr_hvac_mode = HVACMode.OFF
        self._attr_target_temperature = 24.0


@dataclass(frozen=True)
class Room:
    """Which devices a room has and what its thermostat offers."""

    name: str
    trv_modes: tuple[HVACMode, ...]
    cooler: str | None
    separate_cooler: bool


HEAT_COOL_OFFERING_TRV_ROOM = Room(
    "heat_cool_offering_trv_room",
    (HVACMode.OFF, HVACMode.HEAT, HVACMode.HEAT_COOL),
    COOLER_ID,
    True,
)
"""A room with a cooler whose radiator also offers ``heat_cool``.

A device that runs ``heat_cool`` runs its own thermostat against its own pair
of targets, so the radiator has to be driven in ``heat``.
"""

HEAT_COOL_OFFERING_DUAL_ROLE = Room(
    "heat_cool_offering_dual_role",
    (HVACMode.HEAT, HVACMode.COOL, HVACMode.HEAT_COOL, HVACMode.OFF),
    TRV_ID,
    False,
)
"""One entity that heats and cools and also offers ``heat_cool``."""

HEAT_ONLY = Room("heat_only", (HVACMode.HEAT, HVACMode.OFF), None, False)

SCENARIOS = [HEAT_COOL_OFFERING_TRV_ROOM, HEAT_COOL_OFFERING_DUAL_ROLE, HEAT_ONLY]


async def _settle(hass, rounds: int = 120) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)
        await hass.async_block_till_done()


def _on_spelling(room: Room) -> HVACMode:
    """Return the mode the room publishes while it is on."""
    return HVACMode.HEAT if room.cooler is None else HVACMode.HEAT_COOL


async def _start(hass, room: Room):
    device = FakeTrvEntity()
    device._attr_hvac_modes = list(room.trv_modes)
    entities = [device]
    if room.separate_cooler:
        entities.append(FakeCoolerEntity())
    setup_test_component_platform(hass, CLIMATE_DOMAIN, entities)
    assert await async_setup_component(
        hass, CLIMATE_DOMAIN, {CLIMATE_DOMAIN: {"platform": "test"}}
    )
    await hass.async_block_till_done()
    hass.states.async_set(SENSOR_ID, "19.0", {"unit_of_measurement": "°C"})
    entry = make_entry()
    if room.cooler is not None:
        entry = MockConfigEntry(
            domain=entry.domain,
            version=entry.version,
            data={**entry.data, "cooler": room.cooler},
            title=entry.title,
        )
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    await _settle(hass)
    return entry, bt, device


async def _set_mode(hass, mode: HVACMode) -> None:
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_HVAC_MODE,
        {"entity_id": BT_ENTITY, "hvac_mode": mode},
        blocking=True,
    )
    await _settle(hass)


def _press_heat(hass, device) -> None:
    device._attr_hvac_mode = HVACMode.HEAT
    device.async_set_context(Context())
    device.async_write_ha_state()


def _assert_on(hass, bt, device, room: Room) -> None:
    """The room holds "on", publishes its on spelling and heats the device."""
    assert bt.bt_hvac_mode == HVACMode.HEAT
    assert hass.states.get(BT_ENTITY).state == _on_spelling(room)
    assert device.hvac_mode == HVACMode.HEAT
    assert HVACMode.HEAT_COOL not in device.set_hvac_mode_calls


@pytest.mark.parametrize("room", SCENARIOS, ids=lambda r: r.name)
async def test_first_start_holds_the_room_on(hass, room):
    """A room that starts heating holds "on"."""
    _, bt, device = await _start(hass, room)

    _assert_on(hass, bt, device, room)


@pytest.mark.parametrize("room", SCENARIOS, ids=lambda r: r.name)
async def test_the_mode_service_holds_the_room_on(hass, room):
    """Switching the room on through the mode service holds "on"."""
    _, bt, device = await _start(hass, room)
    await _set_mode(hass, HVACMode.OFF)

    await _set_mode(hass, _on_spelling(room))

    _assert_on(hass, bt, device, room)


@pytest.mark.parametrize("room", SCENARIOS, ids=lambda r: r.name)
@pytest.mark.parametrize("spelling", [HVACMode.HEAT, HVACMode.HEAT_COOL])
async def test_either_on_spelling_holds_the_room_on(hass, room, spelling):
    """The entity's mode handler takes both on spellings as "on".

    The published mode list offers only one of them, but device actions and
    the entity's own callers reach the handler with either.
    """
    _, bt, device = await _start(hass, room)
    await _set_mode(hass, HVACMode.OFF)

    await bt.async_set_hvac_mode(spelling)
    await _settle(hass)

    _assert_on(hass, bt, device, room)


@pytest.mark.parametrize("room", SCENARIOS, ids=lambda r: r.name)
@pytest.mark.parametrize("spelling", [HVACMode.HEAT, HVACMode.HEAT_COOL])
async def test_the_temperature_service_with_a_mode_holds_the_room_on(
    hass, room, spelling
):
    """A temperature call that carries either on spelling holds "on"."""
    _, bt, device = await _start(hass, room)
    await _set_mode(hass, HVACMode.OFF)

    await bt.async_set_temperature(temperature=21.0, hvac_mode=spelling)
    await _settle(hass)

    _assert_on(hass, bt, device, room)


@pytest.mark.parametrize("room", SCENARIOS, ids=lambda r: r.name)
async def test_a_press_at_the_device_holds_the_room_on(hass, room):
    """A device switched to heat at the device turns the room on and stays in heat."""
    _, bt, device = await _start(hass, room)
    await _set_mode(hass, HVACMode.OFF)
    device.set_hvac_mode_calls.clear()

    _press_heat(hass, device)
    await _settle(hass)

    _assert_on(hass, bt, device, room)


@pytest.mark.parametrize("room", SCENARIOS, ids=lambda r: r.name)
async def test_a_restart_holds_the_room_on_and_leaves_the_device_alone(hass, room):
    """A room restored from its published state holds "on" and sends no mode."""
    entry, _, device = await _start(hass, room)
    await _set_mode(hass, HVACMode.OFF)
    await _set_mode(hass, _on_spelling(room))
    device.set_hvac_mode_calls.clear()

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    bt = await wait_for_startup(hass, entry)
    await _settle(hass)

    _assert_on(hass, bt, device, room)
    assert device.set_hvac_mode_calls == []
    assert hass.states.get(BT_ENTITY).attributes.get("main_mode") == HVACMode.HEAT


async def test_a_heat_cool_offering_radiator_is_never_switched_to_heat_cool(hass):
    """Across every way of switching a cooler room, its radiator stays in heat.

    The room is switched off and on through the service, on at the radiator,
    on again through the service and restarted. A radiator that offers
    ``heat_cool`` would take that mode as an instruction to run its own
    thermostat, and the press at the radiator must stand as the user made it.
    """
    room = HEAT_COOL_OFFERING_TRV_ROOM
    entry, bt, device = await _start(hass, room)
    await _set_mode(hass, HVACMode.OFF)
    await _set_mode(hass, HVACMode.HEAT_COOL)
    await _set_mode(hass, HVACMode.OFF)
    _press_heat(hass, device)
    await _settle(hass)
    await _set_mode(hass, HVACMode.HEAT_COOL)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    bt = await wait_for_startup(hass, entry)
    await _settle(hass)

    _assert_on(hass, bt, device, room)
    assert device.set_hvac_mode_calls == [HVACMode.OFF, HVACMode.HEAT, HVACMode.OFF]


async def _room_at(hass, bt, room: float) -> None:
    """Let the room sensor report ``room`` and the following cycle run."""
    bt.last_external_sensor_change = dt_util.now() - timedelta(hours=1)
    hass.states.async_set(SENSOR_ID, str(room), {"unit_of_measurement": "°C"})
    await _settle(hass)


@pytest.mark.parametrize("path", ["first_start", "service", "knob", "restart"])
async def test_a_dual_role_device_cools_and_heats_whichever_way_the_room_is_on(
    hass, path
):
    """A device that is heater and cooler cools when due and heats when due.

    It is handed from one channel to the other in cool and heat, never in
    ``heat_cool``, which would make it run its own pair of targets.
    """
    room = HEAT_COOL_OFFERING_DUAL_ROLE
    entry, bt, device = await _start(hass, room)
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
        _press_heat(hass, device)
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
