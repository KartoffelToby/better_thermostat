"""Decision kernel of Better Thermostat.

``decide(snapshot, state)`` maps one immutable observation onto the
desired state of every TRV — the precedence cascade as a single pure
function. It performs no IO and reads no clocks; time arrives inside
the snapshot.

The cascade (top wins):

1. Release conditions — while startup or valve maintenance runs, nothing
   is commanded.
2. Mode — OFF turns every TRV off.
3. Window open — an open window or door turns every TRV off without
   touching the mode.
4. No call for heat — without heat demand every TRV is turned off.
5. Heating — every addressed TRV is asked to heat towards the room
   target. Under the ladder's HOLD rung no calibration runs; the intent
   carries the raw user target (passthrough) so the device stays locked
   on the last known target with the safety hull's frost floor. The
   calibrated numbers (setpoint corrections, offsets, valve
   percentages) are computed in the shell by the calibration
   strategies.

Tiers 1, 3 and 4 are conditions with a meaning (``core/conditions.py``):
``decide()`` collects the active ones, and the highest-ranked one rules
with the effect its meaning fixes. The window and door regions are
two sources of the one window-open meaning; the window's suppression
reason wins the annunciation when both are open. Mode OFF is the user's
choice rather than a condition and ranks between the release conditions
and the others.

Reachability is an address filter applied across the cascade rather than
a tier of it: an unreachable TRV is dropped from the addressed set and
receives no intent, except while boost heating is active (boost keeps
commanding so the TRV catches up the moment it returns).

Neither half of the control-mode region branches the cascade: the
degraded annunciation (unavailable optional sensors) is bookkeeping, and
the ladder rung is read by the shell, which picks the calibration
strategy from it. The kernel emits the same heating intent either way —
under HOLD the shell simply passes the raw target through uncalibrated.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from .conditions import (
    EFFECTS,
    RANK_ABOVE_MODE_OFF,
    RANK_BELOW_MODE_OFF,
    Action,
    Condition,
    Meaning,
    strongest,
)
from .desired import DesiredState, Suppression, TrvDesired
from .fsm.control_mode import ControlModeState
from .fsm.lifecycle import LifecyclePhase, LifecycleState, tick as lifecycle_tick
from .fsm.maintenance import MaintenanceState
from .fsm.mode import ModeState
from .fsm.reachability import ReachabilityState, step as reachability_step
from .fsm.window import WindowState
from .snapshot import HvacMode, Preset, WorldSnapshot


@dataclass(frozen=True)
class KernelState:
    """Aggregate controller-side state threaded through ``decide()``.

    Immutable like the regions it aggregates: every update goes through
    ``dataclasses.replace``, so a state value can be recorded, compared
    and replayed without defensive copies.

    The regions are authoritative: ``decide()`` branches on them, not on
    the mirrored snapshot flags (those remain pure observations for the
    flight recorder and annunciation).

    None of the regions is persisted across restarts. They are
    re-derived from live observations: lifecycle through the startup
    sequence, window/door/maintenance/mode from the first events, and the
    ladder and reachability within one debounce window. Only controller
    state with learning value (PID/TPI/MPC, thermal stats, filters)
    persists — via the StateManager, never through entity attributes.
    """

    window: WindowState = field(default_factory=WindowState)
    door: WindowState = field(default_factory=WindowState)
    maintenance: MaintenanceState = field(default_factory=MaintenanceState)
    lifecycle: LifecycleState = field(default_factory=LifecycleState)
    mode: ModeState = field(default_factory=ModeState)
    control_mode: ControlModeState = field(default_factory=ControlModeState)
    reachability: Mapping[str, ReachabilityState] = field(default_factory=dict)
    # Watchdog heartbeat: monotonic time of the last completed control pass.
    last_control_monotonic: float | None = None

    def __post_init__(self) -> None:
        """Freeze the reachability mapping so the invariant holds at runtime.

        ``@dataclass(frozen=True)`` only blocks attribute reassignment;
        wrapping the dict in a :class:`~types.MappingProxyType` also
        prevents in-place mutation, keeping every update on the
        ``dataclasses.replace`` path.
        """
        object.__setattr__(
            self, "reachability", MappingProxyType(dict(self.reachability))
        )


def running_kernel_state() -> KernelState:
    """Return a KernelState for an entity that has finished starting up.

    Convenience for tests and tooling; the live entity reaches this
    state through the lifecycle region's startup transitions.
    """
    return KernelState(
        lifecycle=LifecycleState(phase=LifecyclePhase.RUNNING),
        mode=ModeState(hvac_mode=HvacMode.HEAT),
    )


def is_boost_heating(snapshot: WorldSnapshot) -> bool:
    """Return whether boost heating is currently demanded.

    Parameters
    ----------
    snapshot : WorldSnapshot
        Immutable observation of the world to evaluate.

    Returns
    -------
    bool
        ``True`` while the boost preset is active and the room temperature
        is below the target, ``False`` otherwise.
    """
    return (
        snapshot.preset_mode == Preset.BOOST
        and snapshot.room_temperature is not None
        and snapshot.heat_target_temperature is not None
        and snapshot.room_temperature < snapshot.heat_target_temperature
    )


def _addressed(snapshot: WorldSnapshot) -> list[str]:
    """Entity ids of every TRV that should be commanded at all.

    Unreachable TRVs are skipped so the shell does not write into the
    void — unless boost heating is active, which keeps commanding.
    """
    boost = is_boost_heating(snapshot)
    return [
        entity_id for entity_id, trv in snapshot.trvs.items() if trv.available or boost
    ]


def _with_mode(
    entity_ids: list[str], hvac_mode: HvacMode, suppression: Suppression | None = None
) -> dict[str, TrvDesired]:
    """Build one intent per TRV carrying ``hvac_mode``."""
    return {
        entity_id: TrvDesired(
            entity_id=entity_id, hvac_mode=hvac_mode, suppression=suppression
        )
        for entity_id in entity_ids
    }


def _active_conditions(snapshot: WorldSnapshot, state: KernelState) -> list[Condition]:
    """Collect the conditions active in this cycle, window before door."""
    conditions: list[Condition] = []
    if state.lifecycle.startup_running:
        conditions.append(Condition(Meaning.STARTUP))
    if state.maintenance.is_blocking(snapshot.now_monotonic):
        # A maintenance run that exceeded its maximum runtime stops
        # blocking — the region's liveness invariant.
        conditions.append(Condition(Meaning.MAINTENANCE))
    if state.window.effective_open:
        conditions.append(Condition(Meaning.WINDOW_OPEN, Suppression.WINDOW))
    if state.door.effective_open:
        conditions.append(Condition(Meaning.WINDOW_OPEN, Suppression.DOOR))
    if not snapshot.call_for_heat:
        conditions.append(
            Condition(Meaning.NO_CALL_FOR_HEAT, Suppression.NO_CALL_FOR_HEAT)
        )
    return conditions


def _apply(
    condition: Condition, addressed: list[str], snapshot: WorldSnapshot
) -> DesiredState:
    """Build the desired state the ruling condition's effect fixes."""
    effect = EFFECTS[condition.meaning]
    call_for_heat = snapshot.call_for_heat if effect.keeps_call_for_heat else False
    if effect.action == Action.RELEASE:
        return DesiredState(call_for_heat=call_for_heat)
    return DesiredState(
        call_for_heat=call_for_heat,
        trvs=_with_mode(addressed, HvacMode.OFF, condition.suppression),
    )


def decide(
    snapshot: WorldSnapshot, state: KernelState
) -> tuple[DesiredState, KernelState]:
    """Map one world snapshot onto the desired state of every TRV.

    The deterministic precedence cascade (top wins): the release
    conditions (startup, maintenance), mode OFF, an open window or door,
    no call for heat, and otherwise heating to the target.

    The input state is never mutated; a fresh successor state is returned.
    The flight recorder records the input as the pre-decide state of the
    cycle.

    Parameters
    ----------
    snapshot : WorldSnapshot
        Immutable observation of the world for this control cycle.
    state : KernelState
        Controller-side state threaded through the kernel.

    Returns
    -------
    tuple[DesiredState, KernelState]
        The desired state to apply to the devices and the updated kernel
        state.
    """
    # Advance the kernel-owned regions from this observation: lifecycle
    # promotes STARTING to RUNNING once the grace window has passed, and
    # the per-TRV reachability regions track availability.
    state = replace(
        state,
        lifecycle=lifecycle_tick(state.lifecycle, snapshot.now),
        reachability={
            entity_id: reachability_step(
                state.reachability.get(entity_id, ReachabilityState()),
                trv.available,
                snapshot.now_monotonic,
            )
            for entity_id, trv in snapshot.trvs.items()
        },
    )

    conditions = _active_conditions(snapshot, state)
    addressed = _addressed(snapshot)

    ruling = strongest(conditions, RANK_ABOVE_MODE_OFF)
    if ruling is not None:
        return _apply(ruling, addressed, snapshot), state

    if state.mode.hvac_mode == HvacMode.OFF:
        return (
            DesiredState(call_for_heat=False, trvs=_with_mode(addressed, HvacMode.OFF)),
            state,
        )

    ruling = strongest(conditions, RANK_BELOW_MODE_OFF)
    if ruling is not None:
        return _apply(ruling, addressed, snapshot), state

    # Heating rung. Under the ladder's HOLD rung no usable temperature
    # exists, so no calibration runs — but the intent still carries the
    # raw user target (passthrough): the device setpoint stays locked on
    # the last known target, re-sent if the device loses it, with the
    # safety hull enforcing the frost floor at the command boundary.
    heating = {
        entity_id: TrvDesired(
            entity_id=entity_id,
            hvac_mode=state.mode.hvac_mode,
            setpoint=snapshot.heat_target_temperature,
        )
        for entity_id in addressed
    }
    return DesiredState(call_for_heat=True, trvs=heating), state
