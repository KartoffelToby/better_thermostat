"""The periodic re-send of the room temperature to TRVs that mirror it.

Some TRVs regulate on a room temperature written into an input of their
own and fall back to their internal sensor after a fixed silence, two
hours on a Sonoff TRVZB. Better Thermostat's own writes are driven by
room sensor changes, and a room holding its temperature produces none,
so the periodic re-send is what holds such a device on the external
value while the room is settled.
"""

from unittest.mock import AsyncMock, MagicMock

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import pytest

from custom_components.better_thermostat.climate import (
    EXTERNAL_TEMPERATURE_KEEPALIVE_INTERVAL,
    BetterThermostat,
)
from custom_components.better_thermostat.trv import Trv

TRV_ID = "climate.trv"
TRV_ID_2 = "climate.trv2"
ROOM_TEMPERATURE = 21.4


def _bt_with_two_trvs(quirks):
    """A BT stand-in holding a room reading and two TRVs carrying quirks."""
    bt = MagicMock()
    bt.device_name = "Test BT"
    bt.cur_temp = ROOM_TEMPERATURE
    bt.real_trvs = {
        TRV_ID: Trv(entity_id=TRV_ID, model_quirks=quirks),
        TRV_ID_2: Trv(entity_id=TRV_ID_2, model_quirks=quirks),
    }
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
async def test_the_tick_writes_nothing_without_a_room_temperature():
    """No reading means no value to keep alive."""
    bt = MagicMock()
    bt.device_name = "Test BT"
    bt.cur_temp = None
    quirks = MagicMock()
    quirks.maybe_set_external_temperature = AsyncMock()
    bt.real_trvs = {TRV_ID: Trv(entity_id=TRV_ID, model_quirks=quirks)}

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
