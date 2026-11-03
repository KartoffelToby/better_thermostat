"""Readings that arrive inside a debounce interval are taken once it is over."""

from datetime import timedelta
from unittest.mock import patch

from homeassistant.core import Context
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from .conftest import (
    SENSOR_ID,
    TRV_ID,
    make_entry,
    setup_entry,
    wait_for,
    wait_for_startup,
)

DEBOUNCE_OVER = timedelta(seconds=6)


def _set_room(hass, value: float) -> None:
    hass.states.async_set(SENSOR_ID, str(value), {"unit_of_measurement": "°C"})


def _report(fake_trv) -> None:
    """Publish the device's state as a report of its own."""
    fake_trv.async_set_context(Context())
    fake_trv.async_write_ha_state()


async def _started(hass):
    _set_room(hass, 19.5)
    entry = make_entry()
    await setup_entry(hass, entry)
    return await wait_for_startup(hass, entry)


async def test_a_room_reading_inside_the_debounce_is_taken_once_it_is_over(
    hass, fake_trv
):
    """The second of two quick room readings is the one the room acts on."""
    bt = await _started(hass)

    _set_room(hass, 18.4)
    assert await wait_for(hass, lambda: bt.cur_temp == 18.4)
    _set_room(hass, 22.3)
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + DEBOUNCE_OVER)

    assert await wait_for(hass, lambda: bt.cur_temp == 22.3, 3.0), (
        f"the room stayed at {bt.cur_temp}"
    )


async def test_a_head_reading_inside_the_debounce_is_taken_once_it_is_over(
    hass, fake_trv
):
    """The second of two quick head readings is the one the room acts on."""
    bt = await _started(hass)
    trv = bt.real_trvs[TRV_ID]

    fake_trv._attr_current_temperature = 21.9
    _report(fake_trv)
    assert await wait_for(hass, lambda: trv.current_temperature == pytest.approx(21.9))
    fake_trv._attr_current_temperature = 23.9
    _report(fake_trv)
    await hass.async_block_till_done()
    # The reread measures the interval on the wall clock, which the timer
    # firing early does not move.
    _later = dt_util.now() + DEBOUNCE_OVER
    with patch(
        "custom_components.better_thermostat.events.trv.dt_util.now",
        return_value=_later,
    ):
        async_fire_time_changed(hass, dt_util.utcnow() + DEBOUNCE_OVER)

        assert await wait_for(
            hass, lambda: trv.current_temperature == pytest.approx(23.9), 3.0
        ), f"the head stayed at {trv.current_temperature}"
