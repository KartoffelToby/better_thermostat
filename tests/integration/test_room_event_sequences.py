"""Random event sequences against a room, judged by rules that hold for any sequence.

The scenario tests in this directory each drive one path: one head gone, one
report inside one cycle. The defects that slipped past them sat where two of
those paths cross, a knob turned during a cycle while another head is off the
air. Writing every crossing out by hand does not scale, so this module draws
sequences of world events instead and judges each one by rules that hold
whatever the sequence was. After every event the room is let settle,
including one run of the five-minute reconciler, because a cycle that starts
before a report has been read is where a user's change gets written away.
Then:

* **intent**: the room's target and mode are the user's latest word, whether
  set on the Better Thermostat entity or at a head that is on the air, and
  the room knows whether its window is open.
* **convergence**: while the room heats, every head on the air heats and
  carries the room's target, as close as its own setpoint grid allows; while
  it is off or its window is open, every head on the air is off.
* **grid**: every setpoint written to a head lies on that head's grid and
  inside its range.
* **bulkhead**: nothing is commanded to a head that was off the air for the
  whole step.
* **surfaces**: every preset number offers the range the room accepts, from
  the moment the thermostat has started.

The events are a setpoint set on the entity, a knob turn at a head, a
setpoint set on the entity while the write to one head is held and another
head is turned, the room's mode set on the entity, a head switched on or off
at the device, the window opening or closing, a head dropping off the air or
coming back, a reload of the entry, and a restart of Home Assistant, in the
order a real boot sets the platforms up. At least one head stays reachable; a
room with none has nothing to converge.

A head that is off speaks for nobody: a knob turned while the room is off or
its window is open is not the user's word, and neither is one head switched
off while another reachable head still heats. A head switched on is, and turns
the room on, but not the setpoint a knob turned while it was off left on it.

Each room is searched with sequences from fixed seeds, so a red case
reproduces by its id. Its failure message names the broken rule, lists the
events and carries a ``BT_ROOM_REPLAY`` line that plays exactly those events
again through ``test_a_replayed_sequence_keeps_every_rule``.
``scripts/shrink_room_sequence.py`` takes that line and drops events for as
long as the same rule still breaks. A sequence worth keeping is pinned at the
end of this module as a test of its own. A longer search is a manual run::

    BT_ROOM_SEQUENCES=500 uv run pytest tests/integration/test_room_event_sequences.py -n auto
"""

from collections.abc import AsyncGenerator
import contextlib
from dataclasses import asdict, dataclass, field
from datetime import timedelta
import json
import os
import random
from typing import ClassVar
from unittest.mock import patch

from homeassistant.components.climate.const import (
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_TEMPERATURE,
    HVACMode,
)
from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import Context, CoreState, Event, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.better_thermostat.climate import BetterThermostat

from .boot_sequence import finish_boot, set_up_during_boot
from .conftest import (
    BT_ENTITY,
    CRITICAL_GRACE,
    DEGRADED_GRACE,
    WINDOW_ID,
    WRITE_BUDGET,
    SimulatedClimate,
    build_devices,
    make_entry,
    mode_commands,
    set_room_sensor,
    setpoint_commands,
    wait_for,
    wait_for_startup,
)
from .device_profiles import (
    GENERIC_HEAT_TRV,
    GROUP_OF_THREE,
    MIXED_GRID_GROUP,
    GroupScenario,
)
from .write_hold import holding_next_write, poll_until

SEQUENCES = int(os.environ.get("BT_ROOM_SEQUENCES", "12"))
STEPS = 8
REPLAY = os.environ.get("BT_ROOM_REPLAY")

SINGLE_HEAD = GroupScenario(name="single_head", profiles=(GENERIC_HEAT_TRV,))

ROOMS = {room.name: room for room in (SINGLE_HEAD, GROUP_OF_THREE, MIXED_GRID_GROUP)}
"""The rooms the search runs in, by name.

One head, three identical heads, and two heads that share neither grid nor
range. The defect pinned below shows on a single head already; the others
are where heads can disagree.
"""

# The room sensor stays where the heads measure, so a target-based
# calibration has no offset to add and a head's setpoint is the room's target.
ROOM_TEMPERATURE = 19.5

# The temperatures the user asks for, inside every head's range. Entity
# setpoints are drawn on the half degree; a turn lands on the turned head's
# own grid.
LOWEST = 17.0
HIGHEST = 26.0

# How long a rule gets to come true after the room has settled.
CONVERGE_S = 3.0

RECONCILE_INTERVAL = timedelta(minutes=6)

# A room restarted while a head is off the air waits out the startup grace
# windows for it, minutes of wall-clock time. Closed at once, startup goes
# ahead with the heads that are there, which is what it does once they have
# run out.
NO_GRACE = timedelta(0)


@dataclass
class Room:
    """The room under test, what the user last asked of it, and what happened."""

    hass: HomeAssistant
    bt: BetterThermostat
    entry: MockConfigEntry
    scenario: GroupScenario
    heads: list[SimulatedClimate]
    intent: float | None
    mode: HVACMode = HVACMode.HEAT
    window_open: bool = False
    available: set[int] = field(default_factory=set)
    events: list[RoomEvent] = field(default_factory=list)
    service_calls: list[Event] = field(default_factory=list)
    reconciles: int = 0
    checked: str = "at startup"

    def reachable(self) -> list[int]:
        """Return the indices of the heads on the air, in configured order."""
        return sorted(self.available)

    def device_mode(self, index: int, mode: HVACMode) -> HVACMode:
        """Return the name one head has for ``mode``; some call heating otherwise."""
        return self.heads[index].profile.hvac_mode if mode == HVACMode.HEAT else mode

    def heating(self) -> bool:
        """Return whether the user lets the room heat and its window is shut."""
        return self.mode == HVACMode.HEAT and not self.window_open

    def step_of(self, index: int) -> float:
        """Return the setpoint grid of one head."""
        return self.heads[index].profile.target_temperature_step

    def grid(self, index: int) -> list[float]:
        """Return the setpoints a user can turn one head to."""
        step = self.step_of(index)
        count = round((HIGHEST - LOWEST) / step)
        return [LOWEST + n * step for n in range(count + 1)]

    def replay_line(self) -> str:
        """Return the environment setting that plays these events again."""
        sequence = {
            "room": self.scenario.name,
            "events": [event.to_json() for event in self.events],
        }
        return f"BT_ROOM_REPLAY='{json.dumps(sequence)}'"

    def describe(self) -> str:
        """Return the events as numbered lines, and how to replay them."""
        lines = [f"  {n}. {event}" for n, event in enumerate(self.events, 1)]
        header = f"room {self.scenario.name}, checked {self.checked}:"
        return "\n".join([header, *lines, self.replay_line()])


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoomEvent:
    """One thing that happens to the room, as data that can be replayed."""

    kind: ClassVar[str]
    kinds: ClassVar[dict[str, type[RoomEvent]]] = {}

    def __init_subclass__(cls, **kwargs) -> None:
        """Register each kind of event under its name for replay."""
        super().__init_subclass__(**kwargs)
        RoomEvent.kinds[cls.kind] = cls

    def possible(self, room: Room) -> bool:
        """Return whether the event can happen in the room as it stands."""
        raise NotImplementedError

    async def happen(self, room: Room) -> None:
        """Let the event happen and record the user's word it carries."""
        raise NotImplementedError

    def to_json(self) -> dict:
        """Return the event as a JSON object."""
        return {"kind": self.kind, **asdict(self)}

    @staticmethod
    def from_json(data: dict) -> RoomEvent:
        """Rebuild an event from its JSON object."""
        fields = dict(data)
        return RoomEvent.kinds[fields.pop("kind")](**fields)


@dataclass(frozen=True)
class Command(RoomEvent):
    """The user sets the room's target on the Better Thermostat entity."""

    kind: ClassVar[str] = "command"
    value: float

    def possible(self, room: Room) -> bool:
        """An entity setpoint can always be set."""
        return True

    async def happen(self, room: Room) -> None:
        """Call the entity's set_temperature service."""
        await _set_on_entity(room, self.value)
        room.intent = self.value

    def __str__(self) -> str:
        return f"entity -> {self.value}"


@dataclass(frozen=True)
class Turn(RoomEvent):
    """The user turns the setpoint at one head."""

    kind: ClassVar[str] = "turn"
    head: int
    value: float

    def possible(self, room: Room) -> bool:
        """Only a reachable head can be turned, and only to a new value."""
        return (
            self.head in room.available
            and self.value != room.heads[self.head].target_temperature
        )

    async def happen(self, room: Room) -> None:
        """Change the head's setpoint and publish it as the head's own report.

        A head that is off takes the turn but does not speak for the room.
        """
        _turn(room.heads[self.head], self.value)
        if room.heating():
            room.intent = self.value

    def __str__(self) -> str:
        return f"knob at head {self.head} -> {self.value}"


@dataclass(frozen=True)
class TurnDuringCycle(RoomEvent):
    """The user sets the entity, and turns one head while another's write is held.

    ``held`` comes after ``turned`` in the configured order, so the cycle is
    still running when ``turned``, which already took its write, is turned.
    The turn is newer than the command and is the user's word.
    """

    kind: ClassVar[str] = "turn_during_cycle"
    turned: int
    held: int
    commanded: float
    turned_to: float

    def possible(self, room: Room) -> bool:
        """The room heats, and both heads are reachable and written to."""
        return (
            room.heating()
            and self.turned < self.held
            and {self.turned, self.held} <= room.available
            and self.commanded != room.intent
            and _write_reaches(room, self.turned, self.commanded)
            and _write_reaches(room, self.held, self.commanded)
            and not _within_step(room, self.turned, self.turned_to, self.commanded)
        )

    async def happen(self, room: Room) -> None:
        """Hold the write to ``held``, turn ``turned`` meanwhile, then release."""
        head = room.heads[self.turned]
        trv = room.bt.real_trvs[head.entity_id]
        async with holding_next_write(
            room.heads[self.held], "async_set_temperature"
        ) as hold:
            await _set_on_entity(room, self.commanded)
            await hold.wait_reached(room.hass)
            assert await poll_until(
                room.hass,
                lambda: (
                    trv.target_temp_received
                    and _within_step(
                        room, self.turned, head.target_temperature, self.commanded
                    )
                ),
            ), f"head {self.turned} never took the command\n{room.describe()}"
            _turn(head, self.turned_to)
            hold.release()
        room.intent = self.turned_to

    def __str__(self) -> str:
        return (
            f"entity -> {self.commanded}, write to head {self.held} held, "
            f"knob at head {self.turned} -> {self.turned_to} during the cycle"
        )


@dataclass(frozen=True)
class Drop(RoomEvent):
    """One head drops off the air."""

    kind: ClassVar[str] = "drop"
    head: int

    def possible(self, room: Room) -> bool:
        """A reachable head can drop as long as another one stays."""
        return self.head in room.available and len(room.available) >= 2

    async def happen(self, room: Room) -> None:
        """Publish the head as unavailable."""
        room.heads[self.head].async_set_context(Context())
        room.heads[self.head].set_available(False)
        room.available.discard(self.head)

    def __str__(self) -> str:
        return f"head {self.head} off the air"


@dataclass(frozen=True)
class BringBack(RoomEvent):
    """One head comes back on the air, holding what it held before."""

    kind: ClassVar[str] = "bring_back"
    head: int

    def possible(self, room: Room) -> bool:
        """Only a head that is gone can come back."""
        return 0 <= self.head < len(room.heads) and self.head not in room.available

    async def happen(self, room: Room) -> None:
        """Publish the head as available again."""
        room.heads[self.head].async_set_context(Context())
        room.heads[self.head].set_available(True)
        room.available.add(self.head)

    def __str__(self) -> str:
        return f"head {self.head} back on the air"


@dataclass(frozen=True)
class SetMode(RoomEvent):
    """The user sets the room's mode on the Better Thermostat entity."""

    kind: ClassVar[str] = "set_mode"
    mode: str

    def possible(self, room: Room) -> bool:
        """Only a mode the room is not in yet is set."""
        return self.mode != room.mode

    async def happen(self, room: Room) -> None:
        """Call the entity's set_hvac_mode service."""
        await room.hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_HVAC_MODE,
            {"entity_id": BT_ENTITY, "hvac_mode": self.mode},
            blocking=True,
        )
        room.mode = HVACMode(self.mode)

    def __str__(self) -> str:
        return f"entity mode -> {self.mode}"


@dataclass(frozen=True)
class SwitchHead(RoomEvent):
    """The user switches one head on or off at the device.

    Switched on, the head turns the room on; the room keeps its target, and
    a setpoint turned at the head while it was off is written over. Switched
    off, it turns the room off only if every other head on the air is off
    already; otherwise one head would switch off a room the others still heat.
    """

    kind: ClassVar[str] = "switch_head"
    head: int
    mode: str

    def possible(self, room: Room) -> bool:
        """Only a reachable head can be switched, and only to a new mode."""
        return (
            self.head in room.available
            and room.device_mode(self.head, HVACMode(self.mode))
            != room.heads[self.head].hvac_mode
        )

    async def happen(self, room: Room) -> None:
        """Change the head's mode and publish it as the head's own report."""
        others_off = all(
            room.heads[i].hvac_mode == HVACMode.OFF
            for i in room.available - {self.head}
        )
        head = room.heads[self.head]
        head._attr_hvac_mode = room.device_mode(self.head, HVACMode(self.mode))
        head.async_set_context(Context())
        head.async_write_ha_state()
        if self.mode == HVACMode.HEAT or others_off:
            room.mode = HVACMode(self.mode)

    def __str__(self) -> str:
        return f"head {self.head} switched {self.mode} at the device"


@dataclass(frozen=True)
class Window(RoomEvent):
    """The room's window opens or shuts."""

    kind: ClassVar[str] = "window"
    open: bool

    def possible(self, room: Room) -> bool:
        """The window only moves to the position it is not in."""
        return self.open != room.window_open

    async def happen(self, room: Room) -> None:
        """Publish the window sensor's new reading."""
        room.hass.states.async_set(WINDOW_ID, "on" if self.open else "off")
        room.window_open = self.open

    def __str__(self) -> str:
        return "window opened" if self.open else "window shut"


@dataclass(frozen=True)
class Reload(RoomEvent):
    """The entry is reloaded, as saving its options does."""

    kind: ClassVar[str] = "reload"

    def possible(self, room: Room) -> bool:
        """An entry can always be reloaded."""
        return True

    async def happen(self, room: Room) -> None:
        """Reload the entry and pick up the thermostat it starts."""
        await room.hass.config_entries.async_reload(room.entry.entry_id)
        await room.hass.async_block_till_done()
        room.bt = await wait_for_startup(room.hass, room.entry)
        await assert_surfaces(room)

    def __str__(self) -> str:
        return "entry reloaded"


@dataclass(frozen=True)
class Restart(RoomEvent):
    """Home Assistant restarts and sets the entry up the way a boot does.

    Every platform is built while Home Assistant is still starting, and the
    thermostat's startup waits for it to have started. What the thermostat
    knows afterwards is what it saved and what the heads report.
    """

    kind: ClassVar[str] = "restart"

    def possible(self, room: Room) -> bool:
        """Home Assistant can always restart."""
        return True

    async def happen(self, room: Room) -> None:
        """Unload the entry, then set it up again during a boot."""
        hass = room.hass
        assert await hass.config_entries.async_unload(room.entry.entry_id)
        await hass.async_block_till_done()
        hass.set_state(CoreState.starting)
        assert await hass.config_entries.async_setup(room.entry.entry_id)
        await hass.async_block_till_done()
        room.bt = await finish_boot(hass, room.entry)
        await assert_surfaces(room)

    def __str__(self) -> str:
        return "Home Assistant restarted"


def _within_step(
    room: Room, index: int, value: float | None, target: float | None
) -> bool:
    """Return whether ``value`` is ``target`` as head ``index`` can express it."""
    if value is None or target is None:
        return False
    return abs(value - target) <= room.step_of(index) / 2 + 1e-6


def _write_reaches(room: Room, index: int, value: float) -> bool:
    """Return whether setting ``value`` on the room has to write to the head."""
    return not _within_step(room, index, room.heads[index].target_temperature, value)


def _turn(head: SimulatedClimate, value: float) -> None:
    """Change a head's setpoint the way a knob press reaches Home Assistant."""
    head._attr_target_temperature = value
    head.async_set_context(Context())
    head.async_write_ha_state()


async def _set_on_entity(room: Room, value: float) -> None:
    """Call set_temperature on the Better Thermostat entity."""
    await room.hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {"entity_id": BT_ENTITY, "temperature": value},
        blocking=True,
    )


def draw(room: Room, rng: random.Random) -> RoomEvent:
    """Draw one event that can happen in the room as it stands."""
    reachable = room.reachable()
    gone = sorted(set(range(len(room.heads))) - room.available)
    entity_setpoints = [
        LOWEST + n / 2 for n in range(round(2 * (HIGHEST - LOWEST)) + 1)
    ]
    candidates: list[RoomEvent] = []
    command = rng.choice([value for value in entity_setpoints if value != room.intent])
    candidates.append(Command(command))
    head = rng.choice(reachable)
    candidates.append(Turn(head, rng.choice(room.grid(head))))
    if len(reachable) >= 2:
        turned, held = sorted(rng.sample(reachable, 2))
        candidates.append(
            TurnDuringCycle(turned, held, command, rng.choice(room.grid(turned)))
        )
        candidates.append(Drop(rng.choice(reachable)))
    if gone:
        candidates.append(BringBack(rng.choice(gone)))
    candidates.append(SetMode(rng.choice([HVACMode.HEAT, HVACMode.OFF]).value))
    candidates.append(
        SwitchHead(rng.choice(reachable), rng.choice([HVACMode.HEAT, HVACMode.OFF]))
    )
    candidates.append(Window(not room.window_open))
    candidates += [Reload(), Restart()]
    possible = [event for event in candidates if event.possible(room)]
    return rng.choice(possible)


# ---------------------------------------------------------------------------
# Running a room
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def running_room(
    hass: HomeAssistant, scenario: GroupScenario
) -> AsyncGenerator[Room]:
    """Boot an entry for ``scenario`` and yield the room once it has settled."""
    heads = await build_devices(hass, *scenario.profiles)
    for head in heads:
        head._attr_current_temperature = ROOM_TEMPERATURE
    set_room_sensor(hass, ROOM_TEMPERATURE)
    hass.states.async_set(WINDOW_ID, "off")
    entry = make_entry(scenario, with_window=True)
    # Set up the way Home Assistant boots with the entry already configured,
    # which is how a room first comes up: the platforms are built before the
    # thermostat has read its heads, and nothing saved fills the gap yet.
    await set_up_during_boot(hass, entry)
    bt = await finish_boot(hass, entry)

    with (
        patch(WRITE_BUDGET, 0.0),
        patch(CRITICAL_GRACE, NO_GRACE),
        patch(DEGRADED_GRACE, NO_GRACE),
    ):
        room = Room(
            hass,
            bt,
            entry,
            scenario,
            heads,
            intent=None,
            available=set(range(len(heads))),
            service_calls=async_capture_events(hass, EVENT_CALL_SERVICE),
        )
        await assert_surfaces(room)
        await _settle(room)
        room.intent = bt.bt_target_temp
        await assert_rules(room, _Before(room))
        yield room


async def step(room: Room, event: RoomEvent) -> None:
    """Let one event happen and check every rule, at once and after the reconciler.

    The first check is what makes the room answer the event itself: a change
    the reconciler carries out minutes later passes the second check alone.
    """
    assert event.possible(room), f"{event} cannot happen here\n{room.describe()}"
    before = _Before(room)
    room.events.append(event)
    await event.happen(room)
    await _quiet(room)
    room.checked = "at once"
    await assert_rules(room, before)
    await _reconcile(room)
    room.checked = "after the reconciler"
    await assert_rules(room, before)


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
    ), f"[settle] the room never came to rest\n{room.describe()}"


async def _settle(room: Room) -> None:
    """Let the room come to rest, including one run of the reconciler."""
    await _quiet(room)
    await _reconcile(room)


async def _reconcile(room: Room) -> None:
    """Run the reconciler once and let the room come to rest again."""
    room.reconciles += 1
    async_fire_time_changed(
        room.hass, dt_util.utcnow() + room.reconciles * RECONCILE_INTERVAL
    )
    await room.hass.async_block_till_done()
    await _quiet(room)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


class _Before:
    """What the room looked like when a step began."""

    def __init__(self, room: Room) -> None:
        self.available = set(room.available)
        self.writes = [len(head.set_temperature_calls) for head in room.heads]
        self.service_calls = len(room.service_calls)


async def assert_rules(room: Room, before: _Before) -> None:
    """Fail unless every rule holds for the step that began at ``before``."""
    bt = room.bt

    assert await wait_for(
        room.hass, lambda: bt.bt_target_temp == pytest.approx(room.intent), CONVERGE_S
    ), (
        f"[intent] the room's target is {bt.bt_target_temp}, the user last "
        f"asked for {room.intent}\n{room.describe()}"
    )
    assert await wait_for(room.hass, lambda: bt.hvac_mode == room.mode, CONVERGE_S), (
        f"[intent] the room is {bt.hvac_mode}, the user last asked for "
        f"{room.mode}\n{room.describe()}"
    )
    assert bt.window_open == room.window_open, (
        f"[intent] the room has its window {'open' if bt.window_open else 'shut'}, "
        f"the sensor says {'open' if room.window_open else 'shut'}\n"
        f"{room.describe()}"
    )

    def heads_carry_the_room() -> bool:
        if not room.heating():
            return all(
                room.heads[i].hvac_mode == HVACMode.OFF for i in room.reachable()
            )
        return all(
            room.heads[i].hvac_mode == room.device_mode(i, HVACMode.HEAT)
            and _within_step(
                room, i, room.heads[i].target_temperature, bt.bt_target_temp
            )
            for i in room.reachable()
        )

    assert await wait_for(room.hass, heads_carry_the_room, CONVERGE_S), (
        f"[convergence] the room {'heats' if room.heating() else 'is off'} at "
        f"{bt.bt_target_temp}, reachable heads carry "
        + ", ".join(
            f"head {i}: {room.heads[i].hvac_mode} {room.heads[i].target_temperature}"
            for i in room.reachable()
        )
        + f"\n{room.describe()}"
    )

    for index, head in enumerate(room.heads):
        profile = head.profile
        for value in head.set_temperature_calls[before.writes[index] :]:
            assert isinstance(value, float | int), (
                f"[grid] head {index} was written a band {value}\n{room.describe()}"
            )
            steps = value / profile.target_temperature_step
            assert abs(steps - round(steps)) < 1e-6, (
                f"[grid] head {index} was written {value}, off its "
                f"{profile.target_temperature_step} grid\n{room.describe()}"
            )
            assert profile.min_temp <= value <= profile.max_temp, (
                f"[grid] head {index} was written {value}, outside "
                f"[{profile.min_temp}, {profile.max_temp}]\n{room.describe()}"
            )

    calls = room.service_calls[before.service_calls :]
    for index in sorted(
        set(range(len(room.heads))) - before.available - room.available
    ):
        entity_id = room.heads[index].entity_id
        sent = setpoint_commands(calls, entity_id) + mode_commands(calls, entity_id)
        assert not sent, (
            f"[bulkhead] head {index} was off the air and was sent {sent}\n"
            f"{room.describe()}"
        )

    await assert_surfaces(room)


async def assert_surfaces(room: Room) -> None:
    """Fail unless every preset number offers the range the room accepts.

    Checked once the thermostat has started and before any time passes: Home
    Assistant polls the numbers every thirty seconds, and a poll republishes
    a stale range as the current one, so a check after the room has settled
    would only ever see the range half a minute late.
    """
    bt = room.bt
    numbers = _preset_numbers(room)
    assert numbers, f"[surfaces] the room has no preset number\n{room.describe()}"

    def offered_ranges() -> dict[str, tuple]:
        ranges = {}
        for entity_id in numbers:
            state = room.hass.states.get(entity_id)
            if state is not None:
                ranges[entity_id] = (
                    state.attributes.get("min"),
                    state.attributes.get("max"),
                )
        return ranges

    accepted = (bt.min_temp, bt.max_temp)
    assert await wait_for(
        room.hass,
        lambda: all(offered == accepted for offered in offered_ranges().values()),
        CONVERGE_S,
    ), (
        f"[surfaces] the room accepts {accepted}, its preset numbers offer "
        f"{offered_ranges()}\n{room.describe()}"
    )


def _preset_numbers(room: Room) -> list[str]:
    """Return the entity ids of the room's preset numbers."""
    prefix = f"{room.bt.unique_id}_preset_"
    registry = er.async_get(room.hass)
    return [
        entry.entity_id
        for entry in er.async_entries_for_config_entry(registry, room.entry.entry_id)
        if entry.domain == "number" and entry.unique_id.startswith(prefix)
    ]


# ---------------------------------------------------------------------------
# The search
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", ROOMS.values(), ids=list(ROOMS))
@pytest.mark.parametrize("seed", range(SEQUENCES), ids=lambda seed: f"seed{seed}")
async def test_a_room_keeps_every_rule(hass, scenario, seed):
    """Whatever the sequence, every rule holds after every event."""
    rng = random.Random(f"{scenario.name}-{seed}")
    async with running_room(hass, scenario) as room:
        for _ in range(STEPS):
            await step(room, draw(room, rng))


@pytest.mark.skipif(REPLAY is None, reason="BT_ROOM_REPLAY names no sequence")
async def test_a_replayed_sequence_keeps_every_rule(hass):
    """The sequence in BT_ROOM_REPLAY keeps every rule.

    A step that cannot happen in the room as the earlier steps left it skips
    the test: a sequence with a step dropped is a different sequence, and
    that is what a shrinker needs to tell from one that still fails.
    """
    sequence = json.loads(REPLAY or "{}")
    events = [RoomEvent.from_json(data) for data in sequence["events"]]
    async with running_room(hass, ROOMS[sequence["room"]]) as room:
        for number, event in enumerate(events, 1):
            if not event.possible(room):
                pytest.skip(f"step {number} ({event}) cannot happen at that point")
            await step(room, event)


# ---------------------------------------------------------------------------
# Sequences the search found
# ---------------------------------------------------------------------------


async def test_a_head_turned_back_to_its_last_confirmed_setpoint_is_adopted(hass):
    """A knob turned up and back down again ends where the user left it.

    The user sets the room on the entity, the head confirms it, and the user
    turns the head up and then back to where it was. Only the turn back is
    the user's word, even though its value is one Better Thermostat once
    wrote to that head.
    """
    async with running_room(hass, SINGLE_HEAD) as room:
        for event in (Command(20.5), Turn(0, 22.0), Turn(0, 20.5)):
            await step(room, event)


@pytest.mark.parametrize(
    "between", [(), (Restart(),)], ids=["straight_on", "across_a_restart"]
)
async def test_a_head_switched_on_does_not_bring_what_was_turned_while_it_was_off(
    hass, between
):
    """Switching a head on turns the room on at the room's own target.

    The user switches the room off, turns the head's knob while it is off,
    and switches the head on at the device. The knob turn was not the user's
    word while the head was off, and the report that switches it on does not
    make it one. The answer is the same whether or not Home Assistant
    restarted in between, when the setpoint the head shows is the one Better
    Thermostat reads back at startup.
    """
    async with running_room(hass, SINGLE_HEAD) as room:
        events = (
            SetMode(HVACMode.OFF),
            Turn(0, 22.5),
            *between,
            SwitchHead(0, HVACMode.HEAT),
        )
        for event in events:
            await step(room, event)
