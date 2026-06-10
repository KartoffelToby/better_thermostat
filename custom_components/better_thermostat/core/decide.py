"""Decision kernel of Better Thermostat.

``decide(snapshot, state)`` maps one immutable observation onto the
desired state of every TRV — the precedence cascade that used to be
scattered across the shell, expressed as a single pure function. It
performs no IO and reads no clocks; time arrives inside the snapshot.

The cascade (top wins):

1. Lifecycle gate — while startup or valve maintenance runs, nothing is
   commanded.
2. Mode — OFF turns every TRV off.
3. Window — an open window turns every TRV off without touching the mode.
4. Reachability — unreachable TRVs receive no intent, except while boost
   heating is active (boost keeps commanding so the TRV catches up the
   moment it returns).
5. Call for heat — without heat demand every TRV is turned off.
6. Heating — every addressed TRV is asked to heat towards the room
   target. Under the ladder's HOLD rung the intent keeps the mode but
   carries no setpoint: with no usable temperature the controller stops
   adjusting and the device keeps its last commanded state. The
   calibrated numbers (setpoint corrections, offsets, valve
   percentages) stay in the shell until the calibrator strategies move
   into the core (M7).

The ``degraded`` flag deliberately does not branch anywhere: today it is
pure annunciation. Giving it an effect on the control law is the
fail-soft ladder's job (M8) and a product decision, not a refactoring.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .desired import DesiredState, TrvDesired
from .fsm.control_mode import ControlMode, ControlModeState
from .fsm.lifecycle import LifecyclePhase, LifecycleState
from .fsm.maintenance import MaintenanceState
from .fsm.mode import ModeState
from .fsm.reachability import ReachabilityState, step as reachability_step
from .fsm.window import WindowState
from .snapshot import HvacMode, WorldSnapshot

# Preset name kept in the core vocabulary; value matches HA's PRESET_BOOST.
PRESET_BOOST = "boost"


@dataclass
class KernelState:
    """Aggregate controller-side state threaded through ``decide()``.

    The regions are authoritative: ``decide()`` branches on them, not on
    the mirrored snapshot flags (those remain pure observations for the
    flight recorder and annunciation).

    None of the regions is persisted across restarts. They are
    re-derived from live observations: lifecycle through the startup
    sequence, window/maintenance/mode from the first events, and the
    ladder and reachability within one debounce window. Only controller
    state with learning value (PID/TPI/MPC, thermal stats, filters)
    persists — via the StateManager, never through entity attributes.
    """

    window: WindowState = field(default_factory=WindowState)
    maintenance: MaintenanceState = field(default_factory=MaintenanceState)
    lifecycle: LifecycleState = field(default_factory=LifecycleState)
    mode: ModeState = field(default_factory=ModeState)
    control_mode: ControlModeState = field(default_factory=ControlModeState)
    reachability: dict[str, ReachabilityState] = field(default_factory=dict)
    # Watchdog heartbeat: monotonic time of the last completed control pass.
    last_control_monotonic: float | None = None


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
    """Return True while the boost preset is active and the room is too cold."""
    return (
        snapshot.preset_mode == PRESET_BOOST
        and snapshot.room_temp is not None
        and snapshot.target_temp is not None
        and snapshot.room_temp < snapshot.target_temp
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


def _with_mode(entity_ids: list[str], hvac_mode: HvacMode) -> dict[str, TrvDesired]:
    """Build one intent per TRV carrying ``hvac_mode``."""
    return {
        entity_id: TrvDesired(entity_id=entity_id, hvac_mode=hvac_mode)
        for entity_id in entity_ids
    }


def decide(
    snapshot: WorldSnapshot, state: KernelState
) -> tuple[DesiredState, KernelState]:
    """Map one world snapshot onto the desired state of every TRV."""
    # Advance the per-TRV reachability regions from this observation.
    state.reachability = {
        entity_id: reachability_step(
            state.reachability.get(entity_id, ReachabilityState()),
            trv.available,
            snapshot.now_monotonic,
        )
        for entity_id, trv in snapshot.trvs.items()
    }

    if state.lifecycle.startup_running or state.maintenance.is_blocking(
        snapshot.now_monotonic
    ):
        # Lifecycle gate: no intent while starting up, and maintenance
        # pre-empts control entirely (it owns the valves). A maintenance
        # run that exceeded its maximum runtime stops blocking — the
        # region's liveness invariant.
        return DesiredState(call_for_heat=snapshot.call_for_heat), state

    addressed = _addressed(snapshot)

    if state.mode.hvac_mode == HvacMode.OFF:
        return (
            DesiredState(call_for_heat=False, trvs=_with_mode(addressed, HvacMode.OFF)),
            state,
        )

    if state.window.effective_open:
        return (
            DesiredState(
                call_for_heat=snapshot.call_for_heat,
                trvs=_with_mode(addressed, HvacMode.OFF),
            ),
            state,
        )

    if not snapshot.call_for_heat:
        return (
            DesiredState(call_for_heat=False, trvs=_with_mode(addressed, HvacMode.OFF)),
            state,
        )

    # HOLD rung of the fail-soft ladder: no usable temperature exists,
    # so the intent keeps the mode and adjusts nothing.
    hold = state.control_mode.mode == ControlMode.HOLD
    heating = {
        entity_id: TrvDesired(
            entity_id=entity_id,
            hvac_mode=state.mode.hvac_mode,
            setpoint=None if hold else snapshot.target_temp,
        )
        for entity_id in addressed
    }
    return DesiredState(call_for_heat=True, trvs=heating), state
