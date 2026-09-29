"""Tests for the TRV temperature standing in for a lost room sensor.

Covers which TRV readings may speak for the room and how the switch to the
fallback asks for a control cycle.
"""

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import State
import pytest

from custom_components.better_thermostat.events.temperature import (
    _schedule_room_sensor_fallback,
    refresh_room_temperature_from_trvs,
    trv_room_temperature,
)
from custom_components.better_thermostat.trv import Trv

SENSOR_ID = "sensor.room_temperature"
FIRST_TRV = "climate.first_trv"
SECOND_TRV = "climate.second_trv"


def _trv(entity_id: str, current_temperature: float | None) -> Trv:
    return Trv.from_legacy_dict(
        entity_id,
        {
            "hvac_mode": HVACMode.HEAT,
            "current_temperature": current_temperature,
            "model_quirks": None,
        },
    )


def _bt(states: dict[str, State], trvs: dict[str, Trv]) -> Any:
    bt = MagicMock()
    bt.device_name = "Test Thermostat"
    bt.sensor_entity_id = SENSOR_ID
    bt.hass.states.get.side_effect = states.get
    bt.real_trvs = trvs
    bt.cur_temp = 18.0
    bt.room_sensor_fallback = False
    bt.room_sensor_fallback_cancel = None
    bt.room_sensor_fallback_due = False
    bt.is_removed = False
    bt.in_maintenance = False
    bt._control_needed_after_maintenance = False
    bt.control_queue_task = asyncio.Queue(maxsize=1)
    bt.async_write_ha_state = MagicMock()
    return bt


def _heating(entity_id: str, current_temperature: float | str | None) -> State:
    attributes: dict[str, Any] = {"temperature": 22.0}
    if current_temperature is not None:
        attributes["current_temperature"] = current_temperature
    return State(entity_id, "heat", attributes)


class TestTrvRoomTemperature:
    """Only a live TRV reading may stand in for the room."""

    def test_an_unavailable_trv_does_not_speak_for_the_room(self):
        """A reading kept from before an outage is passed over."""
        bt = _bt(
            {
                FIRST_TRV: State(FIRST_TRV, STATE_UNAVAILABLE),
                SECOND_TRV: _heating(SECOND_TRV, 20.0),
            },
            {FIRST_TRV: _trv(FIRST_TRV, 25.0), SECOND_TRV: _trv(SECOND_TRV, 20.0)},
        )

        assert trv_room_temperature(bt) == 20.0

    def test_a_trv_without_a_reported_temperature_does_not_speak_for_the_room(self):
        """The startup placeholder of a TRV that reports none is passed over."""
        bt = _bt(
            {
                FIRST_TRV: _heating(FIRST_TRV, None),
                SECOND_TRV: _heating(SECOND_TRV, 20.0),
            },
            {FIRST_TRV: _trv(FIRST_TRV, 5.0), SECOND_TRV: _trv(SECOND_TRV, 20.0)},
        )

        assert trv_room_temperature(bt) == 20.0

    def test_a_trv_whose_reported_temperature_is_unusable_does_not_speak(self):
        """A reading the TRV handler could not convert leaves the stored one stale."""
        bt = _bt(
            {
                FIRST_TRV: _heating(FIRST_TRV, "unknown"),
                SECOND_TRV: _heating(SECOND_TRV, 20.0),
            },
            {FIRST_TRV: _trv(FIRST_TRV, 25.0), SECOND_TRV: _trv(SECOND_TRV, 20.0)},
        )

        assert trv_room_temperature(bt) == 20.0


async def _fire_fallback_timer(bt: Any) -> None:
    """Schedule the fallback and run its timer callback to completion."""
    with patch(
        "custom_components.better_thermostat.events.temperature.async_call_later"
    ) as call_later:
        _schedule_room_sensor_fallback(bt)
    callback = call_later.call_args.args[2]
    await asyncio.wait_for(callback(None), timeout=1)


class TestEnterFallback:
    """The switch to the fallback controls the room on the TRV value."""

    @pytest.mark.asyncio
    async def test_fallback_requests_a_cycle_when_the_temperature_is_unchanged(self):
        """The controllers drop the filtered value, so the room is controlled anew."""
        bt = _bt(
            {
                SENSOR_ID: State(SENSOR_ID, STATE_UNAVAILABLE),
                FIRST_TRV: _heating(FIRST_TRV, 18.0),
            },
            {FIRST_TRV: _trv(FIRST_TRV, 18.0)},
        )

        await _fire_fallback_timer(bt)

        assert bt.room_sensor_fallback is True
        assert bt.cur_temp == 18.0
        assert bt.control_queue_task.qsize() == 1

    @pytest.mark.asyncio
    async def test_fallback_does_not_wait_on_a_full_queue(self):
        """A full queue already holds a cycle; the callback does not block on it."""
        bt = _bt(
            {
                SENSOR_ID: State(SENSOR_ID, STATE_UNAVAILABLE),
                FIRST_TRV: _heating(FIRST_TRV, 21.0),
            },
            {FIRST_TRV: _trv(FIRST_TRV, 21.0)},
        )
        bt.control_queue_task.put_nowait(bt)

        await _fire_fallback_timer(bt)

        assert bt.cur_temp == 21.0
        assert bt.control_queue_task.qsize() == 1

    @pytest.mark.asyncio
    async def test_fallback_timer_does_nothing_after_removal(self):
        """A timer that fires after the entity is removed leaves it alone."""
        bt = _bt(
            {
                SENSOR_ID: State(SENSOR_ID, STATE_UNAVAILABLE),
                FIRST_TRV: _heating(FIRST_TRV, 21.0),
            },
            {FIRST_TRV: _trv(FIRST_TRV, 21.0)},
        )
        bt.is_removed = True

        await _fire_fallback_timer(bt)

        assert bt.room_sensor_fallback is False
        assert bt.cur_temp == 18.0
        assert bt.control_queue_task.qsize() == 0

    @pytest.mark.asyncio
    async def test_fallback_waits_for_a_trv_temperature(self):
        """Without a TRV temperature the room keeps its reading and does not hand over."""
        states = {
            SENSOR_ID: State(SENSOR_ID, STATE_UNAVAILABLE),
            FIRST_TRV: State(FIRST_TRV, STATE_UNAVAILABLE),
        }
        bt = _bt(states, {FIRST_TRV: _trv(FIRST_TRV, None)})

        await _fire_fallback_timer(bt)

        assert bt.room_sensor_fallback is False
        assert bt.cur_temp == 18.0
        assert bt.control_queue_task.qsize() == 0

        # The first TRV that reports completes the handover, even with the
        # temperature the sensor last sent.
        states[FIRST_TRV] = _heating(FIRST_TRV, 18.0)
        bt.real_trvs[FIRST_TRV].current_temperature = 18.0

        assert refresh_room_temperature_from_trvs(bt) is True
        assert bt.room_sensor_fallback is True
        assert bt.cur_temp == 18.0
