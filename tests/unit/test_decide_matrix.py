"""Behaviour matrix for the decision kernel's precedence cascade.

The matrix crosses each input the cascade reads: whether the user has
released the room's devices, which of its TRVs the user has released on
their own, the lifecycle and maintenance gate, the room's HVAC mode, the
debounced window and door regions, the window detection switch over them,
the snapshot's heat demand, and boost heating with one reachable and one
unreachable TRV. Every cell compares the complete
:class:`DesiredState` that ``decide()`` returns, the block it names
included.

The expected values do not come from a second implementation of the
cascade. They come from three tables written down as data:

* ``DECISION_TABLE`` holds ten rows whose patterns do not overlap, so each
  row reads on its own and no row depends on the order of the others.
  ``test_decision_table_covers_each_cell_once`` holds that property.
* ``ADDRESSED`` says which TRVs receive an intent at all, and
  ``TRV_RELEASED`` which of them the user's release of a TRV takes out.
* ``GATED`` says which lifecycle and maintenance phases hold control back,
  and which block names the hold.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from itertools import product

import pytest

from custom_components.better_thermostat.core.decide import KernelState, decide
from custom_components.better_thermostat.core.desired import (
    DesiredState,
    HeatingBlock,
    Suppression,
    TrvDesired,
)
from custom_components.better_thermostat.core.fsm.lifecycle import (
    LifecyclePhase,
    LifecycleState,
)
from custom_components.better_thermostat.core.fsm.maintenance import (
    DUE_AT_FIRST_TICK,
    MAX_RUN_S,
    MaintenanceDue,
    MaintenanceIdle,
    MaintenanceRunning,
    MaintenanceState,
)
from custom_components.better_thermostat.core.fsm.mode import ModeState
from custom_components.better_thermostat.core.fsm.window import WindowPhase, WindowState
from custom_components.better_thermostat.core.snapshot import (
    HvacMode,
    Preset,
    TrvReported,
    WorldSnapshot,
)
from tests.factories import make_snapshot, make_state

TARGET = 21.0
NOW_MONOTONIC = 1000.0
REACHABLE = "climate.up"
UNREACHABLE = "climate.down"

# --- Axis values -----------------------------------------------------------

LIFECYCLE = {
    "initialising": LifecycleState(),
    "starting": LifecycleState(phase=LifecyclePhase.STARTING),
    "running": LifecycleState(phase=LifecyclePhase.RUNNING),
    "stopping": LifecycleState(phase=LifecyclePhase.STOPPING),
}

MAINTENANCE: dict[str, MaintenanceState] = {
    "idle": MaintenanceIdle(next_due=DUE_AT_FIRST_TICK),
    "due": MaintenanceDue(next_due=DUE_AT_FIRST_TICK),
    "running": MaintenanceRunning(
        running_since=NOW_MONOTONIC - 100.0, next_due=DUE_AT_FIRST_TICK
    ),
    "overrun": MaintenanceRunning(
        running_since=NOW_MONOTONIC - MAX_RUN_S - 1.0, next_due=DUE_AT_FIRST_TICK
    ),
}

MODES = {
    "off": HvacMode.OFF,
    "heat": HvacMode.HEAT,
    "cool": HvacMode.COOL,
    "heat_cool": HvacMode.HEAT_COOL,
    "auto": HvacMode.AUTO,
}

CONTACT = {"closed": WindowState(), "open": WindowState(phase=WindowPhase.OPEN)}

DETECTION = {"on": True, "off": False}

# Boost heating needs the boost preset and a room below the target.
BOOST = {
    "no_boost": {"preset_mode": None, "room_temperature": 19.0},
    "boost": {"preset_mode": Preset.BOOST, "room_temperature": 19.0},
    "boost_reached": {"preset_mode": Preset.BOOST, "room_temperature": 21.5},
}

DEMAND = {"demand": True, "no_demand": False}

RELEASED = {"held": False, "released": True}

# Which TRVs the user has released on their own: none, the reachable one,
# the unreachable one, or both.
TRV_RELEASED = {
    "no_trv_released": frozenset(),
    "up_released": frozenset({REACHABLE}),
    "down_released": frozenset({UNREACHABLE}),
    "both_released": frozenset({REACHABLE, UNREACHABLE}),
}

# --- Expected-outcome tables -----------------------------------------------

STARTUP = HeatingBlock.STARTUP
VALVE_MAINTENANCE = HeatingBlock.VALVE_MAINTENANCE

# Which lifecycle and maintenance phases hold control back, by the block
# that names the hold; None lets control through. A starting entity inside
# its grace window already controls; a maintenance run past its maximum
# runtime no longer holds control back. A startup names the hold even while
# a maintenance run blocks as well.
GATED = {
    ("initialising", "idle"): STARTUP,
    ("initialising", "due"): STARTUP,
    ("initialising", "running"): STARTUP,
    ("initialising", "overrun"): STARTUP,
    ("starting", "idle"): None,
    ("starting", "due"): None,
    ("starting", "running"): VALVE_MAINTENANCE,
    ("starting", "overrun"): None,
    ("running", "idle"): None,
    ("running", "due"): None,
    ("running", "running"): VALVE_MAINTENANCE,
    ("running", "overrun"): None,
    ("stopping", "idle"): None,
    ("stopping", "due"): None,
    ("stopping", "running"): VALVE_MAINTENANCE,
    ("stopping", "overrun"): None,
}

# Which TRVs receive an intent once control is not held back. An unreachable
# TRV is left alone unless boost heating is under way. A TRV released on its
# own is left alone even then: the cell's TRV_RELEASED set is taken out.
ADDRESSED = {
    "no_boost": {REACHABLE},
    "boost": {REACHABLE, UNREACHABLE},
    "boost_reached": {REACHABLE},
}

ANY = "*"
# Placeholders in the outcome columns, resolved per cell.
KEEP = "keep"  # call_for_heat repeats the snapshot's heat demand
ROOM_MODE = "room mode"  # the TRV is set to the room's own HVAC mode
RELEASE = "release"  # no TRV receives an intent
GATE = "gate"  # the block GATED names for the cell


@dataclass(frozen=True)
class Rule:
    """One row of the decision table: a pattern and the outcome it demands."""

    released: bool | str
    every_trv_released: bool | str
    gated: bool | str
    mode: str
    detection: str
    window: str
    door: str
    demand: bool | str
    trv_mode: HvacMode | str
    setpoint: float | None
    suppression: Suppression | None
    call_for_heat: bool | str
    devices_released: bool
    blocked_by: HeatingBlock | str | None

    def matches(self, cell: Cell) -> bool:
        """Return whether ``cell`` falls under this row's pattern."""
        mode = "off" if cell.mode == "off" else "other"
        return all(
            want in (ANY, have)
            for want, have in (
                (self.released, RELEASED[cell.released]),
                (
                    self.every_trv_released,
                    TRV_RELEASED[cell.trv_released] == {REACHABLE, UNREACHABLE},
                ),
                (self.gated, GATED[cell.lifecycle, cell.maintenance] is not None),
                (self.mode, mode),
                (self.detection, cell.detection),
                (self.window, cell.window),
                (self.door, cell.door),
                (self.demand, DEMAND[cell.demand]),
            )
        )


# fmt: off
DECISION_TABLE = (
    #     released every_trv gated  mode     detection window    door      demand  | trv mode      setpoint  suppression                    call_for_heat  released  blocked by
    Rule(True,     ANY,      ANY,   ANY,     ANY,      ANY,      ANY,      ANY,      RELEASE,      None,     None,                          KEEP,          True,     HeatingBlock.DEVICES_RELEASED),
    Rule(False,    True,     ANY,   ANY,     ANY,      ANY,      ANY,      ANY,      RELEASE,      None,     None,                          KEEP,          False,    HeatingBlock.DEVICES_RELEASED),
    Rule(False,    False,    True,  ANY,     ANY,      ANY,      ANY,      ANY,      RELEASE,      None,     None,                          KEEP,          False,    GATE),
    Rule(False,    False,    False, "off",   ANY,      ANY,      ANY,      ANY,      HvacMode.OFF, None,     None,                          False,         False,    HeatingBlock.HVAC_OFF),
    Rule(False,    False,    False, "other", "on",     "open",   ANY,      ANY,      HvacMode.OFF, None,     Suppression.WINDOW,            KEEP,          False,    HeatingBlock.WINDOW),
    Rule(False,    False,    False, "other", "on",     "closed", "open",   ANY,      HvacMode.OFF, None,     Suppression.DOOR,              KEEP,          False,    HeatingBlock.DOOR),
    Rule(False,    False,    False, "other", "on",     "closed", "closed", False,    HvacMode.OFF, None,     Suppression.NO_CALL_FOR_HEAT,  False,         False,    HeatingBlock.NO_CALL_FOR_HEAT),
    Rule(False,    False,    False, "other", "on",     "closed", "closed", True,     ROOM_MODE,    TARGET,   None,                          True,          False,    None),
    Rule(False,    False,    False, "other", "off",    ANY,      ANY,      False,    HvacMode.OFF, None,     Suppression.NO_CALL_FOR_HEAT,  False,         False,    HeatingBlock.NO_CALL_FOR_HEAT),
    Rule(False,    False,    False, "other", "off",    ANY,      ANY,      True,     ROOM_MODE,    TARGET,   None,                          True,          False,    None),
)
# fmt: on


@dataclass(frozen=True)
class Cell:
    """One combination of the matrix axes, by axis-value name."""

    lifecycle: str
    maintenance: str
    mode: str
    window: str
    door: str
    demand: str
    boost: str
    detection: str = "on"
    released: str = "held"
    trv_released: str = "no_trv_released"

    @property
    def id(self) -> str:
        """Return the pytest id naming each axis value.

        Window detection is on and the devices are held by default, and
        no TRV is released on its own, so only a cell with detection off,
        the devices released or a TRV released names that axis, and every
        cell keeps the id it had before the axis was added.
        """
        parts = (
            self.lifecycle,
            self.maintenance,
            self.mode,
            f"window_{self.window}",
            f"door_{self.door}",
            self.demand,
            self.boost,
        )
        if self.detection != "on":
            parts = (*parts, f"detection_{self.detection}")
        if self.released != "held":
            parts = (*parts, self.released)
        if self.trv_released != "no_trv_released":
            parts = (*parts, self.trv_released)
        return "-".join(parts)

    def inputs(self) -> tuple[WorldSnapshot, KernelState]:
        """Build the snapshot and kernel state this cell describes."""
        snapshot = make_snapshot(
            now_monotonic=NOW_MONOTONIC,
            heat_target_temperature=TARGET,
            call_for_heat=DEMAND[self.demand],
            devices_released=RELEASED[self.released],
            trvs={
                entity_id: TrvReported(
                    entity_id=entity_id,
                    available=entity_id == REACHABLE,
                    released=entity_id in TRV_RELEASED[self.trv_released],
                )
                for entity_id in (REACHABLE, UNREACHABLE)
            },
            **BOOST[self.boost],
        )
        lifecycle = LIFECYCLE[self.lifecycle]
        if lifecycle.phase == LifecyclePhase.STARTING:
            lifecycle = LifecycleState(
                phase=LifecyclePhase.STARTING,
                grace_until=snapshot.now + timedelta(minutes=10),
            )
        state = make_state(
            lifecycle=lifecycle,
            maintenance=MAINTENANCE[self.maintenance],
            mode=ModeState(hvac_mode=MODES[self.mode]),
            window=CONTACT[self.window],
            door=CONTACT[self.door],
            window_detection=DETECTION[self.detection],
        )
        return snapshot, state


def _cells(
    *,
    lifecycles: tuple[str, ...],
    maintenance: tuple[str, ...] = tuple(MAINTENANCE),
    modes: tuple[str, ...] = tuple(MODES),
    doors: tuple[str, ...] = tuple(CONTACT),
    detection: tuple[str, ...] = tuple(DETECTION),
    trv_released: tuple[str, ...] = tuple(TRV_RELEASED),
) -> list[Cell]:
    return [
        Cell(*values)
        for values in product(
            lifecycles,
            maintenance,
            modes,
            CONTACT,
            doors,
            DEMAND,
            BOOST,
            detection,
            RELEASED,
            trv_released,
        )
    ]


# The full lifecycle axis is crossed with the maintenance axis in
# ``GATED`` already; downstream of the gate one held-back and one
# controlling phase are enough.
#
# A TRV released on its own changes which TRVs are addressed, the same way
# in every tier, and adds the tier that holds when all of them are. Its axis
# is therefore crossed with one value per outcome of the other axes rather
# than with all of them: a held-back and a controlling phase, an idle and a
# running maintenance, the off mode and one other, the window (which stands
# for the door), and detection on (whose rows address the TRVs as the rows
# with detection off do). ``test_each_row_is_reached_with_a_trv_released``
# holds that every other row is reached with a TRV released.
MATRIX = _cells(
    lifecycles=("initialising", "running"), trv_released=("no_trv_released",)
) + _cells(
    lifecycles=("initialising", "running"),
    maintenance=("idle", "running"),
    modes=("off", "heat"),
    doors=("closed",),
    detection=("on",),
    trv_released=tuple(name for name in TRV_RELEASED if name != "no_trv_released"),
)
ALL_CELLS = _cells(lifecycles=tuple(LIFECYCLE))


def _expected(cell: Cell) -> DesiredState:
    """Read the cell's outcome off the tables."""
    (rule,) = [rule for rule in DECISION_TABLE if rule.matches(cell)]
    demand = DEMAND[cell.demand]
    call_for_heat = demand if rule.call_for_heat == KEEP else rule.call_for_heat
    assert isinstance(call_for_heat, bool)
    blocked_by = (
        GATED[cell.lifecycle, cell.maintenance]
        if rule.blocked_by == GATE
        else rule.blocked_by
    )
    assert blocked_by is None or isinstance(blocked_by, HeatingBlock)
    if rule.trv_mode == RELEASE:
        return DesiredState(
            call_for_heat=call_for_heat,
            devices_released=rule.devices_released,
            heating_blocked_by=blocked_by,
        )
    trv_mode = MODES[cell.mode] if rule.trv_mode == ROOM_MODE else rule.trv_mode
    assert isinstance(trv_mode, HvacMode)
    return DesiredState(
        call_for_heat=call_for_heat,
        trvs={
            entity_id: TrvDesired(
                entity_id=entity_id,
                hvac_mode=trv_mode,
                setpoint=rule.setpoint,
                suppression=rule.suppression,
            )
            for entity_id in ADDRESSED[cell.boost] - TRV_RELEASED[cell.trv_released]
        },
        heating_blocked_by=blocked_by,
    )


def test_decision_table_covers_each_cell_once():
    """Each matrix cell falls under exactly one row of the decision table.

    A gap would leave a cell without an expected outcome, and an overlap
    would make the outcome depend on row order, which the table is
    written to avoid.
    """
    counts = {
        cell.id: sum(rule.matches(cell) for rule in DECISION_TABLE)
        for cell in ALL_CELLS
    }
    assert {cell_id: n for cell_id, n in counts.items() if n != 1} == {}
    for rule in DECISION_TABLE:
        assert any(rule.matches(cell) for cell in MATRIX), rule


def test_each_row_is_reached_with_a_trv_released():
    """The reduced crossing of the per-TRV release reaches every row it can.

    It leaves out an open door and detection off, so the door row and the
    two rows with detection off are reached without a TRV released only.
    Each other row is reached with one.
    """
    trv_cells = [cell for cell in MATRIX if cell.trv_released != "no_trv_released"]
    unreached = [
        rule
        for rule in DECISION_TABLE
        if rule.detection != "off"
        and rule.door != "open"
        and not any(rule.matches(cell) for cell in trv_cells)
    ]
    assert unreached == []


@pytest.mark.parametrize("cell", MATRIX, ids=lambda cell: cell.id)
def test_decide_matches_the_decision_table(cell: Cell):
    """The kernel's intent per TRV and its heat demand follow the table."""
    snapshot, state = cell.inputs()
    desired, _ = decide(snapshot, state)
    assert desired == _expected(cell)


@pytest.mark.parametrize(
    ("lifecycle", "maintenance"), list(GATED), ids=["-".join(key) for key in GATED]
)
def test_gate_holds_control_back_in_the_listed_phases(lifecycle, maintenance):
    """A held-back cycle addresses no TRV and repeats the heat demand.

    Every other cell of the gate table reaches the heating intent of an
    open, demanding room.
    """
    cell = Cell(
        lifecycle, maintenance, "heat", "closed", "closed", "demand", "no_boost"
    )
    snapshot, state = cell.inputs()
    desired, _ = decide(snapshot, state)
    assert desired == _expected(cell)
    assert (desired.trvs == {}) is (GATED[lifecycle, maintenance] is not None)


def test_every_block_rules_some_cell():
    """Each block of the closed list is reachable, and the heating rung names none.

    The values are the public ``heating_blocked_by`` attribute: a block no
    cell reaches would be a documented value the thermostat never shows.
    """
    named = {decide(*cell.inputs())[0].heating_blocked_by for cell in ALL_CELLS}
    assert named == {*HeatingBlock, None}


def test_a_suppressed_intent_names_its_block():
    """Where the TRVs carry a suppression, the decision names the same word."""
    named: dict[Suppression, set[HeatingBlock | None]] = {}
    for cell in MATRIX:
        desired, _ = decide(*cell.inputs())
        for intent in desired.trvs.values():
            if intent.suppression is not None:
                named.setdefault(intent.suppression, set()).add(
                    desired.heating_blocked_by
                )
    assert named == {
        suppression: {HeatingBlock(suppression.value)} for suppression in Suppression
    }
