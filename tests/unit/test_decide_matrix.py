"""Behaviour matrix for the decision kernel's precedence cascade.

The matrix crosses each input the cascade reads: the lifecycle and
maintenance gate, the room's HVAC mode, the debounced window and door
regions, the window detection switch over them, the snapshot's heat
demand, and boost heating with one reachable and one unreachable TRV. Every cell compares the complete
:class:`DesiredState` that ``decide()`` returns.

The expected values do not come from a second implementation of the
cascade. They come from three tables written down as data:

* ``DECISION_TABLE`` holds eight rows whose patterns do not overlap, so each
  row reads on its own and no row depends on the order of the others.
  ``test_decision_table_covers_each_cell_once`` holds that property.
* ``ADDRESSED`` says which TRVs receive an intent at all.
* ``GATED`` says which lifecycle and maintenance phases hold control back.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from itertools import product

import pytest

from custom_components.better_thermostat.core.decide import KernelState, decide
from custom_components.better_thermostat.core.desired import (
    DesiredState,
    Suppression,
    TrvDesired,
)
from custom_components.better_thermostat.core.fsm.lifecycle import (
    LifecyclePhase,
    LifecycleState,
)
from custom_components.better_thermostat.core.fsm.maintenance import (
    MAX_RUN_S,
    MaintenancePhase,
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

MAINTENANCE = {
    "idle": MaintenanceState(),
    "due": MaintenanceState(phase=MaintenancePhase.DUE),
    "running": MaintenanceState(
        phase=MaintenancePhase.RUNNING, running_since=NOW_MONOTONIC - 100.0
    ),
    "overrun": MaintenanceState(
        phase=MaintenancePhase.RUNNING, running_since=NOW_MONOTONIC - MAX_RUN_S - 1.0
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

# --- Expected-outcome tables -----------------------------------------------

# Which lifecycle and maintenance phases hold control back. A starting
# entity inside its grace window already controls; a maintenance run past
# its maximum runtime no longer holds control back.
GATED = {
    ("initialising", "idle"): True,
    ("initialising", "due"): True,
    ("initialising", "running"): True,
    ("initialising", "overrun"): True,
    ("starting", "idle"): False,
    ("starting", "due"): False,
    ("starting", "running"): True,
    ("starting", "overrun"): False,
    ("running", "idle"): False,
    ("running", "due"): False,
    ("running", "running"): True,
    ("running", "overrun"): False,
    ("stopping", "idle"): False,
    ("stopping", "due"): False,
    ("stopping", "running"): True,
    ("stopping", "overrun"): False,
}

# Which TRVs receive an intent once control is not held back. An unreachable
# TRV is left alone unless boost heating is under way.
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


@dataclass(frozen=True)
class Rule:
    """One row of the decision table: a pattern and the outcome it demands."""

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

    def matches(self, cell: Cell) -> bool:
        """Return whether ``cell`` falls under this row's pattern."""
        mode = "off" if cell.mode == "off" else "other"
        return all(
            want in (ANY, have)
            for want, have in (
                (self.gated, GATED[cell.lifecycle, cell.maintenance]),
                (self.mode, mode),
                (self.detection, cell.detection),
                (self.window, cell.window),
                (self.door, cell.door),
                (self.demand, DEMAND[cell.demand]),
            )
        )


# fmt: off
DECISION_TABLE = (
    #     gated  mode     detection window    door      demand  | trv mode      setpoint  suppression                    call_for_heat
    Rule(True,  ANY,     ANY,      ANY,      ANY,      ANY,      RELEASE,      None,     None,                          KEEP),
    Rule(False, "off",   ANY,      ANY,      ANY,      ANY,      HvacMode.OFF, None,     None,                          False),
    Rule(False, "other", "on",     "open",   ANY,      ANY,      HvacMode.OFF, None,     Suppression.WINDOW,            KEEP),
    Rule(False, "other", "on",     "closed", "open",   ANY,      HvacMode.OFF, None,     Suppression.DOOR,              KEEP),
    Rule(False, "other", "on",     "closed", "closed", False,    HvacMode.OFF, None,     Suppression.NO_CALL_FOR_HEAT,  False),
    Rule(False, "other", "on",     "closed", "closed", True,     ROOM_MODE,    TARGET,   None,                          True),
    Rule(False, "other", "off",    ANY,      ANY,      False,    HvacMode.OFF, None,     Suppression.NO_CALL_FOR_HEAT,  False),
    Rule(False, "other", "off",    ANY,      ANY,      True,     ROOM_MODE,    TARGET,   None,                          True),
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

    @property
    def id(self) -> str:
        """Return the pytest id naming each axis value.

        Window detection is on by default, so only a cell with it off
        names it.
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
        return "-".join(parts)

    def inputs(self) -> tuple[WorldSnapshot, KernelState]:
        """Build the snapshot and kernel state this cell describes."""
        snapshot = make_snapshot(
            now_monotonic=NOW_MONOTONIC,
            heat_target_temperature=TARGET,
            call_for_heat=DEMAND[self.demand],
            trvs={
                REACHABLE: TrvReported(entity_id=REACHABLE, available=True),
                UNREACHABLE: TrvReported(entity_id=UNREACHABLE, available=False),
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


def _cells(*, lifecycles: tuple[str, ...]) -> list[Cell]:
    return [
        Cell(*values)
        for values in product(
            lifecycles, MAINTENANCE, MODES, CONTACT, CONTACT, DEMAND, BOOST, DETECTION
        )
    ]


# The full lifecycle axis is crossed with the maintenance axis in
# ``GATED`` already; downstream of the gate one held-back and one
# controlling phase are enough.
MATRIX = _cells(lifecycles=("initialising", "running"))
ALL_CELLS = _cells(lifecycles=tuple(LIFECYCLE))


def _expected(cell: Cell) -> DesiredState:
    """Read the cell's outcome off the tables."""
    (rule,) = [rule for rule in DECISION_TABLE if rule.matches(cell)]
    demand = DEMAND[cell.demand]
    call_for_heat = demand if rule.call_for_heat == KEEP else rule.call_for_heat
    assert isinstance(call_for_heat, bool)
    if rule.trv_mode == RELEASE:
        return DesiredState(call_for_heat=call_for_heat)
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
            for entity_id in ADDRESSED[cell.boost]
        },
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
    assert (desired.trvs == {}) is GATED[lifecycle, maintenance]
