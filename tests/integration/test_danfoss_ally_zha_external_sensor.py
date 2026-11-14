"""A Danfoss Ally under ZHA regulates on the room temperature BT writes."""

from dataclasses import replace
from unittest.mock import patch

import pytest

from .conftest import (
    WRITE_BUDGET,
    make_entry,
    profile_id,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import ZHA_DANFOSS_ALLY_TRV


@pytest.fixture(autouse=True)
def immediate_writes():
    """Exercise the device writes without waiting for ordinary write spacing."""
    with patch(WRITE_BUDGET, 0.0):
        yield


async def _start(hass, profile):
    entry = make_entry(profile)
    await setup_entry(hass, entry)
    return await wait_for_startup(hass, entry)


@pytest.mark.parametrize(
    "fake_trv", [ZHA_DANFOSS_ALLY_TRV], indirect=True, ids=profile_id
)
async def test_the_room_temperature_reaches_the_external_sensor_input(hass, fake_trv):
    """The input ZHA registers for the device follows the room sensor in °C."""
    number = fake_trv.danfoss_ally_external_sensor
    assert number is not None
    set_room_sensor(hass, 19.4)
    await _start(hass, fake_trv.profile)

    assert await wait_for(hass, lambda: 19.4 in number.set_value_calls)

    set_room_sensor(hass, 20.6)
    assert await wait_for(hass, lambda: number.set_value_calls[-1] == 20.6)


@pytest.mark.parametrize(
    "fake_trv",
    [replace(ZHA_DANFOSS_ALLY_TRV, calibration_mode="default")],
    indirect=True,
    ids=["calibrating"],
)
async def test_a_calibrating_ally_gets_no_room_temperature(hass, fake_trv):
    """A calibration on top would correct the device's own bias a second time."""
    number = fake_trv.danfoss_ally_external_sensor
    assert number is not None
    set_room_sensor(hass, 19.4)
    await _start(hass, fake_trv.profile)
    set_room_sensor(hass, 20.6)
    await hass.async_block_till_done()

    assert number.set_value_calls == []
