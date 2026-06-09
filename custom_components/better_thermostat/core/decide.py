"""Decision kernel of Better Thermostat.

``decide(snapshot, state)`` maps one immutable observation onto the
desired state of every TRV — the precedence cascade that used to be
scattered across the shell, expressed as a single pure function. It
performs no IO and reads no clocks; time arrives inside the snapshot.

The cascade (top wins):

1. Lifecycle gate — while startup runs, nothing is commanded.
2. Mode — OFF turns every TRV off.
3. Window — an open window turns every TRV off without touching the mode.
4. Lower tiers (maintenance, reachability, calibrate/passthrough) move
   in here ticket by ticket.
"""

from __future__ import annotations

from dataclasses import dataclass

from .desired import DesiredState, TrvDesired
from .snapshot import HvacMode, WorldSnapshot


@dataclass
class KernelState:
    """Aggregate controller-side state threaded through ``decide()``.

    Grows region by region as the orthogonal FSMs (window, maintenance,
    lifecycle, mode, control mode, reachability) move into the core.
    """


def _all_off(snapshot: WorldSnapshot) -> dict[str, TrvDesired]:
    """Intent: every TRV off."""
    return {
        entity_id: TrvDesired(entity_id=entity_id, hvac_mode=HvacMode.OFF)
        for entity_id in snapshot.trvs
    }


def decide(
    snapshot: WorldSnapshot, state: KernelState
) -> tuple[DesiredState, KernelState]:
    """Map one world snapshot onto the desired state of every TRV."""
    if snapshot.startup_running:
        # Lifecycle gate: no intent while the entity is still starting up.
        return DesiredState(call_for_heat=snapshot.call_for_heat), state

    if snapshot.hvac_mode == HvacMode.OFF:
        return DesiredState(call_for_heat=False, trvs=_all_off(snapshot)), state

    if snapshot.window_open:
        return (
            DesiredState(call_for_heat=snapshot.call_for_heat, trvs=_all_off(snapshot)),
            state,
        )

    # Lower tiers (maintenance, reachability, calibrate/passthrough) are
    # pulled in by subsequent tickets; until then no intent is produced.
    return DesiredState(call_for_heat=snapshot.call_for_heat), state
