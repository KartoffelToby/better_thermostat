"""Decision kernel of Better Thermostat.

``decide(snapshot, state)`` maps one immutable observation onto the
desired state of every TRV — the precedence cascade as a single pure
function. It performs no IO and reads no clocks; time arrives inside
the snapshot.

The cascade (top wins):

1. Lifecycle gate — while startup or valve maintenance runs, nothing is
   commanded.
2. Mode — OFF turns every TRV off.
3. Window — an open window turns every TRV off without touching the mode.
4. Call for heat — without heat demand every TRV is turned off.
5. Heating — every addressed TRV is asked to heat towards the room
   target. Under the ladder's HOLD rung no calibration runs; the intent
   carries the raw user target (passthrough) so the device stays locked
   on the last known target with the safety hull's frost floor. The
   calibrated numbers (setpoint corrections, offsets, valve
   percentages) are computed in the shell by the calibration
   strategies.

Reachability is an address filter applied across the cascade rather than
a tier of it: an unreachable TRV is dropped from the addressed set and
receives no intent, except while boost heating is active (boost keeps
commanding so the TRV catches up the moment it returns).

Degraded annunciation (unavailable optional sensors) deliberately does
not branch anywhere; only the control-mode region's rung affects the
decision.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from .desired import DesiredState, Suppression, TrvDesired
from .fsm.control_mode import ControlModeState
from .fsm.lifecycle import LifecyclePhase, LifecycleState, tick as lifecycle_tick
from .fsm.maintenance import MaintenanceState
from .fsm.mode import ModeState
from .fsm.reachability import ReachabilityState, step as reachability_step
from .fsm.window import WindowState
from .snapshot import HvacMode, WorldSnapshot

# Preset name kept in the core vocabulary; value matches HA's PRESET_BOOST.
PRESET_BOOST = "boost"


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


def decide(
    snapshot: WorldSnapshot, state: KernelState
) -> tuple[DesiredState, KernelState]:
    """Map one world snapshot onto the desired state of every TRV.

    The deterministic precedence cascade (top wins): the lifecycle and
    maintenance gate, mode OFF, an open window, no call for heat, and
    otherwise heating to the target.

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
                trvs=_with_mode(addressed, HvacMode.OFF, Suppression.WINDOW),
            ),
            state,
        )

    if not snapshot.call_for_heat:
        return (
            DesiredState(
                call_for_heat=False,
                trvs=_with_mode(addressed, HvacMode.OFF, Suppression.NO_CALL_FOR_HEAT),
            ),
            state,
        )

    # Heating rung. Under the ladder's HOLD rung no usable temperature
    # exists, so no calibration runs — but the intent still carries the
    # raw user target (passthrough): the device setpoint stays locked on
    # the last known target, re-sent if the device loses it, with the
    # safety hull enforcing the frost floor at the command boundary.
    heating = {
        entity_id: TrvDesired(
            entity_id=entity_id,
            hvac_mode=state.mode.hvac_mode,
            setpoint=snapshot.target_temp,
        )
        for entity_id in addressed
    }
    return DesiredState(call_for_heat=True, trvs=heating), state
