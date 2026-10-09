"""The periodic re-send of the room temperature to TRVs that mirror it.

Some TRVs regulate on a room temperature written into an input of their
own and fall back to their internal sensor after a fixed silence, two
hours on a Sonoff TRVZB. Better Thermostat's own writes are driven by
room sensor changes, and a room holding its temperature produces none,
so the periodic re-send is what holds such a device on the external
value while the room is settled.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfTemperature
from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import pytest

from custom_components.better_thermostat.climate import (
    EXTERNAL_TEMPERATURE_KEEPALIVE_INTERVAL,
    BetterThermostat,
)
from custom_components.better_thermostat.trv import Trv
from tests.factories import ThermostatStandIn

TRV_ID = "climate.trv"
TRV_ID_2 = "climate.trv2"
SENSOR_ID = "sensor.room"
ROOM_TEMPERATURE = 21.4


def _publish_room_sensor(bt, state: str | None) -> None:
    """Give the stand-in a room sensor that publishes ``state``, or nothing."""
    bt.sensor_entity_id = SENSOR_ID
    published = (
        None
        if state is None
        else State(SENSOR_ID, state, {"unit_of_measurement": UnitOfTemperature.CELSIUS})
    )
    bt.hass.states.get = MagicMock(
        side_effect=lambda entity_id: published if entity_id == SENSOR_ID else None
    )


def _bt_with_two_trvs(quirks):
    """A BT stand-in holding a room reading and two TRVs carrying quirks."""
    bt = ThermostatStandIn()
    bt.device_name = "Test BT"
    bt.room_temperature = ROOM_TEMPERATURE
    bt.real_trvs = {
        TRV_ID: Trv(entity_id=TRV_ID, model_quirks=quirks),
        TRV_ID_2: Trv(entity_id=TRV_ID_2, model_quirks=quirks),
    }
    bt._temperature_filter_lock = None
    _publish_room_sensor(bt, str(ROOM_TEMPERATURE))
    return bt


def _written_values(quirks):
    """The (TRV, value) pairs the tick handed to the quirk."""
    return [
        call.args[1:] for call in quirks.maybe_set_external_temperature.await_args_list
    ]


@pytest.mark.asyncio
async def test_the_interval_stays_inside_the_shortest_known_fallback():
    """Two hours is the TRVZB's silence budget; the interval clears it.

    Pinned as a bound, not as a value: an interval raised past the
    fallback window would leave the device on its own sensor between
    writes, which is the state this tick exists to prevent.
    """
    assert EXTERNAL_TEMPERATURE_KEEPALIVE_INTERVAL.total_seconds() > 0
    assert EXTERNAL_TEMPERATURE_KEEPALIVE_INTERVAL.total_seconds() <= 3600


@pytest.mark.asyncio
async def test_the_tick_writes_the_room_temperature_to_every_trv():
    """Each TRV with the quirk gets the temperature BT is regulating on."""
    quirks = MagicMock()
    quirks.maybe_set_external_temperature = AsyncMock(return_value=True)
    bt = _bt_with_two_trvs(quirks)

    await BetterThermostat._external_temperature_keepalive(bt)

    assert _written_values(quirks) == [
        (TRV_ID, ROOM_TEMPERATURE),
        (TRV_ID_2, ROOM_TEMPERATURE),
    ]


@pytest.mark.asyncio
async def test_the_tick_skips_a_trv_that_still_awaits_its_initialization():
    """A TRV startup went on without is written to once it is initialized.

    Until then its quirk state is not set up, so the tick leaves it out and
    still reaches the other TRV.
    """
    quirks = MagicMock()
    quirks.maybe_set_external_temperature = AsyncMock(return_value=True)
    bt = _bt_with_two_trvs(quirks)
    bt.real_trvs[TRV_ID].awaiting_initialization = True

    await BetterThermostat._external_temperature_keepalive(bt)

    assert _written_values(quirks) == [(TRV_ID_2, ROOM_TEMPERATURE)]


@pytest.mark.asyncio
async def test_the_tick_writes_nothing_without_a_room_temperature():
    """No reading means no value to keep alive."""
    bt = ThermostatStandIn()
    bt.device_name = "Test BT"
    bt.room_temperature = None
    quirks = MagicMock()
    quirks.maybe_set_external_temperature = AsyncMock()
    bt.real_trvs = {TRV_ID: Trv(entity_id=TRV_ID, model_quirks=quirks)}

    await BetterThermostat._external_temperature_keepalive(bt)

    quirks.maybe_set_external_temperature.assert_not_awaited()


@pytest.mark.parametrize(
    "sensor_state",
    [None, STATE_UNAVAILABLE, STATE_UNKNOWN, "150.0"],
    ids=["removed", "unavailable", "unknown", "implausible"],
)
@pytest.mark.asyncio
async def test_the_tick_writes_nothing_while_the_room_sensor_gives_no_reading(
    sensor_state,
):
    """A room temperature nothing measures any more is not kept alive.

    The room temperature BT holds is the sensor's last reading, and a
    device that keeps receiving it regulates on a room that has moved on.
    Left without writes, the device falls back to its own sensor.
    """
    quirks = MagicMock()
    quirks.maybe_set_external_temperature = AsyncMock(return_value=True)
    bt = _bt_with_two_trvs(quirks)
    _publish_room_sensor(bt, sensor_state)

    await BetterThermostat._external_temperature_keepalive(bt)

    quirks.maybe_set_external_temperature.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_tick_writes_nothing_without_a_room_sensor():
    """A room without a sensor has no measured temperature to mirror."""
    quirks = MagicMock()
    quirks.maybe_set_external_temperature = AsyncMock(return_value=True)
    bt = _bt_with_two_trvs(quirks)
    bt.sensor_entity_id = None

    await BetterThermostat._external_temperature_keepalive(bt)

    quirks.maybe_set_external_temperature.assert_not_awaited()


@pytest.mark.parametrize(
    "refusal",
    [
        HomeAssistantError("device did not answer"),
        ServiceValidationError("value is out of range"),
        OSError("connection reset"),
    ],
    ids=["unreachable", "out_of_range", "transport"],
)
@pytest.mark.asyncio
async def test_a_trv_that_refuses_the_write_does_not_cost_the_others_their_tick(
    refusal,
):
    """The tick serves every TRV, whatever the one before it answered.

    A refused write is the normal answer of a device that is asleep or
    whose integration is reloading, and it says nothing about the TRVs
    further down the list. Letting it end the tick would drop them back
    onto their internal sensors for a full interval.
    """
    quirks = MagicMock()
    quirks.maybe_set_external_temperature = AsyncMock(side_effect=[refusal, True])
    bt = _bt_with_two_trvs(quirks)

    await BetterThermostat._external_temperature_keepalive(bt)

    assert _written_values(quirks) == [
        (TRV_ID, ROOM_TEMPERATURE),
        (TRV_ID_2, ROOM_TEMPERATURE),
    ]


@pytest.mark.asyncio
async def test_a_trv_that_never_answers_does_not_hold_the_tick():
    """A write that does not return in time is given up, and the next TRV is served.

    The tick holds the filter lock while it writes, so an unbounded wait on
    one device would also hold back every later room reading.
    """
    written = []

    async def _write(_bt, entity_id, value):
        if entity_id == TRV_ID:
            await asyncio.Event().wait()
        written.append((entity_id, value))
        return True

    quirks = MagicMock()
    quirks.maybe_set_external_temperature = _write
    bt = _bt_with_two_trvs(quirks)

    with patch(
        "custom_components.better_thermostat.climate.EXTERNAL_TEMPERATURE_WRITE_TIMEOUT_S",
        0.01,
    ):
        await asyncio.wait_for(
            BetterThermostat._external_temperature_keepalive(bt), timeout=5
        )

    assert written == [(TRV_ID_2, ROOM_TEMPERATURE)]
