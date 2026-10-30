"""A temperature call that carries ``hvac_mode: off`` turns the devices off.

``climate.set_temperature`` accepts a mode next to the target. A call that
switches the room off keeps its target for later, and the devices follow the
room off in the same way they follow the mode service, not on some later
report or reconcile tick.
"""

import asyncio
from dataclasses import replace
from unittest.mock import patch

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_TEMPERATURE,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE
import pytest

from .conftest import (
    BT_ENTITY,
    WRITE_BUDGET,
    build_devices,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for_startup,
)
from .device_profiles import (
    GENERIC_HEAT_TRV,
    HEAT_ONLY,
    RANGED_COOLER,
    SEPARATE_COOLER,
    RoleScenario,
)

HEAT_ONLY_WITHOUT_OFF_MODE = replace(
    HEAT_ONLY,
    name="heat_only_without_off_mode",
    trv=replace(
        GENERIC_HEAT_TRV, name="trv_without_off_mode", hvac_modes=(HVACMode.HEAT,)
    ),
)
"""A room whose radiator offers no OFF mode and is turned off by its minimum."""


@pytest.fixture(autouse=True)
def _no_write_budget():
    """Let a write follow the previous one without waiting out the budget."""
    with patch(WRITE_BUDGET, 0.0):
        yield


async def _settle(hass, rounds: int = 120) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)
        await hass.async_block_till_done()


async def _start(hass, scenario: RoleScenario):
    devices = await build_devices(
        hass, *(p for p in (scenario.trv, scenario.cooler) if p is not None)
    )
    set_room_sensor(hass, 19.0)
    entry = make_entry(scenario)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    await _settle(hass)
    return bt, devices


async def _set_temperature_and_mode(
    hass, temperature: float, mode: HVACMode, *, ranged: bool = False
) -> None:
    """Call the temperature service with a mode; a ranged room takes a pair."""
    targets = (
        {ATTR_TARGET_TEMP_LOW: temperature, ATTR_TARGET_TEMP_HIGH: temperature + 3.0}
        if ranged
        else {ATTR_TEMPERATURE: temperature}
    )
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {"entity_id": BT_ENTITY, ATTR_HVAC_MODE: mode, **targets},
        blocking=True,
    )
    await _settle(hass)


@pytest.mark.parametrize(
    "scenario", [HEAT_ONLY, SEPARATE_COOLER, RANGED_COOLER], ids=lambda s: s.name
)
async def test_the_devices_follow_the_room_off(hass, scenario):
    """The radiator and the cooler are off once the call has been handled."""
    bt, devices = await _start(hass, scenario)
    assert devices[0].hvac_mode == HVACMode.HEAT

    await _set_temperature_and_mode(
        hass, 23.0, HVACMode.OFF, ranged=scenario.cooler is not None
    )

    assert hass.states.get(BT_ENTITY).state == HVACMode.OFF
    assert bt.heat_target_temperature == pytest.approx(23.0)
    assert [device.hvac_mode for device in devices] == [HVACMode.OFF] * len(devices)


async def test_a_radiator_without_off_mode_drops_to_its_minimum(hass):
    """A radiator without an OFF mode receives its minimum setpoint."""
    _, (trv,) = await _start(hass, HEAT_ONLY_WITHOUT_OFF_MODE)
    assert trv.target_temperature != pytest.approx(
        HEAT_ONLY_WITHOUT_OFF_MODE.trv.min_temp
    )

    await _set_temperature_and_mode(hass, 23.0, HVACMode.OFF)

    assert hass.states.get(BT_ENTITY).state == HVACMode.OFF
    assert trv.target_temperature == pytest.approx(
        HEAT_ONLY_WITHOUT_OFF_MODE.trv.min_temp
    )


async def test_a_room_already_off_sends_nothing(hass):
    """A call that leaves the room off only stores the target."""
    bt, (trv,) = await _start(hass, HEAT_ONLY_WITHOUT_OFF_MODE)
    await _set_temperature_and_mode(hass, 21.0, HVACMode.OFF)
    written = list(trv.set_temperature_calls)

    await _set_temperature_and_mode(hass, 24.0, HVACMode.OFF)

    assert bt.heat_target_temperature == pytest.approx(24.0)
    assert trv.set_temperature_calls == written
