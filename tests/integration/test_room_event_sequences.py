"""Random event sequences against a room, judged by convergence.

The scenario tests in this directory each drive one path: one head gone, one
report inside one cycle. The defects that slipped past them sat where two of
those paths cross, a knob turned during a cycle while another head is off the
air. Writing every crossing out by hand does not scale, so this module draws
sequences of world events instead and judges each one by a rule that holds
whatever the sequence was:

* **The user's latest word is the room's target.** A setpoint set on the
  Better Thermostat entity, or turned at a head that is on the air, is the
  room's target once things have settled, until the user says something
  newer.
* **Every head on the air carries that target.** Once things have settled,
  each reachable head reports the setpoint the room wants.

"Settled" includes a run of the five-minute reconciler, because a cycle that
starts before a report has been read is where a user's change gets written
away.

The events are a setpoint set on the entity, a knob turn at a head, a
setpoint set on the entity whose write to one head is held while another
head is turned, and a head dropping off the air or coming back. At least one
head stays reachable; a room with none has nothing to converge.

Every sequence comes from a fixed seed, so a red case reproduces by its id,
and its failure message carries the trace of events up to the broken rule.
A sequence the search found is pinned below as a test of its own. A longer
search is a manual run::

    BT_ROOM_SEQUENCES=500 uv run pytest tests/integration/test_room_event_sequences.py -n auto
"""

from collections.abc import AsyncGenerator, Awaitable, Callable
import contextlib
from dataclasses import dataclass, field
from datetime import timedelta
import os
import random
from unittest.mock import patch

from homeassistant.components.climate.const import (
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_TEMPERATURE,
)
from homeassistant.core import Context, HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.better_thermostat.climate import BetterThermostat

from .conftest import (
    BT_ENTITY,
    WRITE_BUDGET,
    SimulatedClimate,
    build_devices,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import GENERIC_HEAT_TRV, GROUP_OF_THREE, GroupScenario
from .write_hold import holding_next_write, poll_until

SEQUENCES = int(os.environ.get("BT_ROOM_SEQUENCES", "20"))
STEPS = 8

# The room sensor stays where the heads measure, so a target-based
# calibration has no offset to add and a head's setpoint is the room's target.
ROOM_TEMPERATURE = 19.5

# Setpoints on the heads' half-degree grid, inside every head's range.
SETPOINTS = [value / 2 for value in range(34, 53)]

# How long a rule gets to come true after the room has settled.
CONVERGE_S = 3.0

RECONCILE_INTERVAL = timedelta(minutes=6)

# The retry that looks in on a head off the air waits out an exponential
# backoff of real minutes. The compressed sleeps turn that into milliseconds,
# so the room would cycle without pause for as long as a head is gone and
# never come to rest. A head that comes back announces itself with its own
# availability event, which queues a cycle at once; the retry is what notices
# a head that returns without one, and no event here does that.
REACHABILITY_RETRY = (
    "custom_components.better_thermostat.utils.controlling._schedule_reachability_retry"
)


@dataclass
class Room:
    """The room under test, what the user last asked of it, and the trace."""

    hass: HomeAssistant
    bt: BetterThermostat
    heads: list[SimulatedClimate]
    intent: float | None
    available: set[int] = field(default_factory=set)
    trace: list[str] = field(default_factory=list)
    reconciles: int = 0

    def reachable(self) -> list[int]:
        """Return the indices of the heads on the air, in configured order."""
        return sorted(self.available)

    def describe(self) -> str:
        """Return the trace as numbered lines for a failure message."""
        return "\n".join(f"  {n}. {line}" for n, line in enumerate(self.trace, 1))


@contextlib.asynccontextmanager
async def running_room(
    hass: HomeAssistant, devices: GroupScenario
) -> AsyncGenerator[Room]:
    """Set up an entry for ``devices`` and yield the room once it has settled."""
    heads = await build_devices(hass, *devices.profiles)
    for head in heads:
        head._attr_current_temperature = ROOM_TEMPERATURE
    set_room_sensor(hass, ROOM_TEMPERATURE)
    entry = make_entry(devices)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)

    with patch(WRITE_BUDGET, 0.0), patch(REACHABILITY_RETRY):
        room = Room(hass, bt, heads, intent=None, available=set(range(len(heads))))
        await _settle(room)
        room.intent = bt.bt_target_temp
        await assert_converged(room)
        yield room


async def step(room: Room, event: Awaitable[None]) -> None:
    """Let one event happen, let the room settle, and check both rules."""
    await event
    await _settle(room)
    await assert_converged(room)


def _publish(head: SimulatedClimate) -> None:
    """Publish the head's state as a change that came from the head itself."""
    head.async_set_context(Context())
    head.async_write_ha_state()


async def _quiet(room: Room) -> None:
    """Wait until no cycle runs and every reachable head confirmed its writes."""
    trvs = room.bt.real_trvs
    ids = [room.heads[i].entity_id for i in room.reachable()]
    assert await wait_for(
        room.hass,
        lambda: (
            not room.bt.ignore_states
            and all(
                trvs[i].target_temp_received and trvs[i].system_mode_received
                for i in ids
            )
        ),
    ), f"the room never came to rest\n{room.describe()}"


async def _settle(room: Room) -> None:
    """Let the room come to rest, including one run of the reconciler."""
    await _quiet(room)
    room.reconciles += 1
    async_fire_time_changed(
        room.hass, dt_util.utcnow() + room.reconciles * RECONCILE_INTERVAL
    )
    await room.hass.async_block_till_done()
    await _quiet(room)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


async def command(room: Room, value: float) -> None:
    """Set the room's target on the Better Thermostat entity."""
    room.trace.append(f"entity -> {value}")
    await room.hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {"entity_id": BT_ENTITY, "temperature": value},
        blocking=True,
    )
    room.intent = value


async def turn(room: Room, index: int, value: float) -> None:
    """Turn the setpoint at one reachable head."""
    room.trace.append(f"knob at head {index} -> {value}")
    head = room.heads[index]
    head._attr_target_temperature = value
    _publish(head)
    room.intent = value


async def turn_during_cycle(
    room: Room, turned: int, held: int, commanded: float, turned_to: float
) -> None:
    """Set the room on the entity and turn one head while the write to another is held.

    ``held`` comes after ``turned`` in the configured order, so the cycle is
    still running when ``turned``, which already confirmed its write, is
    turned. The turn is newer than the command and is the user's word.
    """
    room.trace.append(
        f"entity -> {commanded}, write to head {held} held, "
        f"knob at head {turned} -> {turned_to} during the cycle"
    )
    head = room.heads[turned]
    trv = room.bt.real_trvs[head.entity_id]
    async with holding_next_write(room.heads[held], "async_set_temperature") as hold:
        await room.hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {"entity_id": BT_ENTITY, "temperature": commanded},
            blocking=True,
        )
        await hold.wait_reached(room.hass)
        assert await poll_until(
            room.hass,
            lambda: trv.target_temp_received and head.target_temperature == commanded,
        ), f"head {turned} never confirmed the command\n{room.describe()}"
        head._attr_target_temperature = turned_to
        _publish(head)
        hold.release()
    room.intent = turned_to


async def drop(room: Room, index: int) -> None:
    """Take one head off the air."""
    room.trace.append(f"head {index} off the air")
    room.heads[index].async_set_context(Context())
    room.heads[index].set_available(False)
    room.available.discard(index)


async def bring_back(room: Room, index: int) -> None:
    """Bring one head back on the air, holding what it held before."""
    room.trace.append(f"head {index} back on the air")
    room.heads[index].async_set_context(Context())
    room.heads[index].set_available(True)
    room.available.add(index)


def _new_setpoint(room: Room, rng: random.Random) -> float:
    """Draw a setpoint the user has not asked for last."""
    return rng.choice([value for value in SETPOINTS if value != room.intent])


def _draw_command(room: Room, rng: random.Random) -> Awaitable[None]:
    return command(room, _new_setpoint(room, rng))


def _draw_turn(room: Room, rng: random.Random) -> Awaitable[None]:
    return turn(room, rng.choice(room.reachable()), _new_setpoint(room, rng))


def _draw_turn_during_cycle(room: Room, rng: random.Random) -> Awaitable[None]:
    turned, held = sorted(rng.sample(room.reachable(), 2))
    commanded = _new_setpoint(room, rng)
    turned_to = rng.choice([value for value in SETPOINTS if value != commanded])
    return turn_during_cycle(room, turned, held, commanded, turned_to)


def _draw_drop(room: Room, rng: random.Random) -> Awaitable[None]:
    return drop(room, rng.choice(room.reachable()))


def _draw_bring_back(room: Room, rng: random.Random) -> Awaitable[None]:
    gone = sorted(set(range(len(room.heads))) - room.available)
    return bring_back(room, rng.choice(gone))


EVENTS: list[
    tuple[Callable[[Room, random.Random], Awaitable[None]], Callable[[Room], bool]]
] = [
    (_draw_command, lambda _room: True),
    (_draw_turn, lambda _room: True),
    (_draw_turn_during_cycle, lambda room: len(room.available) >= 2),
    (_draw_drop, lambda room: len(room.available) >= 2),
    (_draw_bring_back, lambda room: len(room.available) < len(room.heads)),
]


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


async def assert_converged(room: Room) -> None:
    """Fail unless both rules hold once the room has settled."""
    bt = room.bt

    def target_is_intent() -> bool:
        return bt.bt_target_temp == pytest.approx(room.intent)

    assert await wait_for(room.hass, target_is_intent, CONVERGE_S), (
        f"the room's target is {bt.bt_target_temp}, the user last asked for "
        f"{room.intent}\n{room.describe()}"
    )

    def heads_carry_target() -> bool:
        return all(
            room.heads[i].target_temperature == pytest.approx(bt.bt_target_temp)
            for i in room.reachable()
        )

    assert await wait_for(room.hass, heads_carry_target, CONVERGE_S), (
        "reachable heads carry "
        + ", ".join(
            f"head {i}: {room.heads[i].target_temperature}" for i in room.reachable()
        )
        + f", the room's target is {bt.bt_target_temp}\n{room.describe()}"
    )


# ---------------------------------------------------------------------------
# The search
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(SEQUENCES), ids=lambda seed: f"seed{seed}")
async def test_a_room_converges_on_the_users_latest_word(hass, seed):
    """Whatever the sequence, the room ends on the user's latest setpoint."""
    rng = random.Random(seed)
    async with running_room(hass, GROUP_OF_THREE) as room:
        for _ in range(STEPS):
            draw = rng.choice([draw for draw, possible in EVENTS if possible(room)])
            await step(room, draw(room, rng))


# ---------------------------------------------------------------------------
# Sequences the search found
# ---------------------------------------------------------------------------

SINGLE_HEAD = GroupScenario(name="single_head", profiles=(GENERIC_HEAT_TRV,))


async def test_a_head_turned_back_to_its_last_confirmed_setpoint_is_adopted(hass):
    """A knob turned up and back down again ends where the user left it.

    The user sets the room on the entity, the head confirms it, and the user
    turns the head up and then back to where it was. Only the turn back is
    the user's word, even though its value is one Better Thermostat once
    wrote to that head.
    """
    async with running_room(hass, SINGLE_HEAD) as room:
        await step(room, command(room, 20.5))
        await step(room, turn(room, 0, 22.0))
        await step(room, turn(room, 0, 20.5))
