"""A room with one head off the air still answers every one of its triggers.

Each listener in climate.py runs the real critical-entity check before it
hands the event on. One head of the room is unavailable; the reachable head,
the window and door contacts, the room, humidity and outdoor sensors, the
cooler and both ticks still have to reach their handlers. The control cycle
leaves the unreachable head out on its own.
"""

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import State
import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.trv import Trv

_CLIMATE = "custom_components.better_thermostat.climate"
_WATCHER = "custom_components.better_thermostat.utils.watcher"

GONE_ID = "climate.gone_trv"
PRESENT_ID = "climate.present_trv"
WINDOW_ID = "binary_sensor.window"
DOOR_ID = "binary_sensor.door"
SENSOR_ID = "sensor.room_temperature"
HUMIDITY_ID = "sensor.room_humidity"
OUTDOOR_ID = "sensor.outdoor_temperature"
COOLER_ID = "climate.cooler"


def _room(*, one_head_gone: bool) -> MagicMock:
    """Build a room of two heads and its sensors; one head may be gone."""
    bt = MagicMock()
    bt.device_name = "Test BT"
    bt.real_trvs = {
        GONE_ID: Trv(entity_id=GONE_ID),
        PRESENT_ID: Trv(entity_id=PRESENT_ID),
    }
    bt.window_id = WINDOW_ID
    bt.door_id = DOOR_ID
    bt.sensor_entity_id = SENSOR_ID
    bt.humidity_sensor_entity_id = HUMIDITY_ID
    bt.outdoor_sensor = OUTDOOR_ID
    bt.cooler_entity_id = COOLER_ID
    bt.in_maintenance = False
    bt.devices_errors = []
    bt.devices_states = {}
    bt._critical_grace_until = None
    bt._current_humidity = None
    bt.call_for_heat = True
    bt._last_call_for_heat = True
    bt.control_queue_task = MagicMock(put=AsyncMock())
    bt.async_update_ha_state = AsyncMock()
    bt._trigger_contact_change = lambda *args: BetterThermostat._trigger_contact_change(
        bt, *args
    )

    states = {
        GONE_ID: State(GONE_ID, "unavailable" if one_head_gone else "heat"),
        PRESENT_ID: State(PRESENT_ID, "heat", {"current_temperature": 20.0}),
        WINDOW_ID: State(WINDOW_ID, "on"),
        DOOR_ID: State(DOOR_ID, "on"),
        SENSOR_ID: State(SENSOR_ID, "20.5"),
        HUMIDITY_ID: State(HUMIDITY_ID, "55"),
        OUTDOOR_ID: State(OUTDOOR_ID, "8.0"),
        COOLER_ID: State(COOLER_ID, "cool"),
    }
    bt.hass.states.get.side_effect = states.get

    spawned: list[Any] = []
    bt._spawn_owned = lambda coro, name=None: spawned.append(coro)
    bt.spawned = spawned
    return bt


def _event(bt: MagicMock, entity_id: str) -> MagicMock:
    """Wrap the entity's current state into a state-change event."""
    state = bt.hass.states.get(entity_id)
    event = MagicMock()
    event.data = {"entity_id": entity_id, "old_state": state, "new_state": state}
    return event


@dataclass(frozen=True)
class Entrance:
    """One way into the room: the listener, its source, and what it reaches."""

    name: str
    listener: str
    source: str | None
    handler: str


ENTRANCES = [
    Entrance("window", "_trigger_window_change", WINDOW_ID, "trigger_window_change"),
    Entrance("door", "_trigger_door_change", DOOR_ID, "trigger_door_change"),
    Entrance(
        "room_sensor",
        "_trigger_temperature_change",
        SENSOR_ID,
        "trigger_temperature_change",
    ),
    Entrance("humidity", "_trigger_humidity_change", HUMIDITY_ID, "humidity"),
    Entrance("present_head", "_trigger_trv_change", PRESENT_ID, "trigger_trv_change"),
    Entrance("cooler", "_trigger_cooler_change", COOLER_ID, "trigger_cooler_change"),
    Entrance(
        "outdoor_sensor",
        "_trigger_outdoor_change",
        OUTDOOR_ID,
        "check_ambient_air_temperature",
    ),
    Entrance("weather_tick", "_trigger_check_weather", None, "check_weather"),
    Entrance("periodic_tick", "_trigger_time", None, "check_ambient_air_temperature"),
]

_PATCHED_HANDLERS = (
    "trigger_window_change",
    "trigger_door_change",
    "trigger_temperature_change",
    "trigger_trv_change",
    "trigger_cooler_change",
    "check_ambient_air_temperature",
    "check_weather",
)


async def _report(bt: MagicMock, entrance: Entrance) -> bool:
    """Deliver the entrance's event; return whether it reached its handler."""
    handlers = {name: AsyncMock() for name in _PATCHED_HANDLERS}
    patches = [patch(f"{_CLIMATE}.{name}", mock) for name, mock in handlers.items()]
    patches += [
        patch(f"{_CLIMATE}.check_and_update_degraded_mode", AsyncMock()),
        patch(f"{_WATCHER}.ir"),
    ]
    for active in patches:
        active.start()
    try:
        event = _event(bt, entrance.source) if entrance.source else MagicMock()
        await getattr(BetterThermostat, entrance.listener)(bt, event)
        for coro in bt.spawned:
            await coro
    finally:
        for active in reversed(patches):
            active.stop()

    if entrance.handler == "humidity":
        return bt._current_humidity == 55.0
    return handlers[entrance.handler].await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("entrance", ENTRANCES, ids=lambda e: e.name)
async def test_every_entrance_reaches_a_room_with_all_heads(entrance):
    """With every head on the air, each trigger reaches its handler."""
    bt = _room(one_head_gone=False)

    assert await _report(bt, entrance)
    assert bt.devices_errors == []


@pytest.mark.asyncio
@pytest.mark.parametrize("entrance", ENTRANCES, ids=lambda e: e.name)
async def test_every_entrance_reaches_the_room_while_one_head_is_gone(entrance):
    """A head off the air takes only itself out of the room.

    The room still hears its window, door, sensors, cooler, the reachable
    head and both ticks, and the absent head is reported as the only error.
    """
    bt = _room(one_head_gone=True)

    assert await _report(bt, entrance)
    assert bt.devices_errors == [GONE_ID]


@pytest.mark.asyncio
async def test_the_periodic_tick_asks_for_a_cycle_while_one_head_is_gone():
    """The tick still queues a control cycle for the heads that answer."""
    bt = _room(one_head_gone=True)

    assert await _report(bt, ENTRANCES[-1])
    bt.control_queue_task.put.assert_awaited_once_with(bt)
