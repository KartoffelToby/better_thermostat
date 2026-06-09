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
5. Lower tiers (calibrate/passthrough) move in here ticket by ticket.

The ``degraded`` flag deliberately does not branch anywhere: today it is
pure annunciation. Giving it an effect on the control law is the
fail-soft ladder's job (M8) and a product decision, not a refactoring.
"""

from __future__ import annotations

from dataclasses import dataclass

from .desired import DesiredState, TrvDesired
from .snapshot import HvacMode, WorldSnapshot

# Preset name kept in the core vocabulary; value matches HA's PRESET_BOOST.
PRESET_BOOST = "boost"


@dataclass
class KernelState:
    """Aggregate controller-side state threaded through ``decide()``.

    Grows region by region as the orthogonal FSMs (window, maintenance,
    lifecycle, mode, control mode, reachability) move into the core.
    """


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
    if snapshot.startup_running or snapshot.in_maintenance:
        # Lifecycle gate: no intent while starting up, and maintenance
        # pre-empts control entirely (it owns the valves).
        return DesiredState(call_for_heat=snapshot.call_for_heat), state

    addressed = _addressed(snapshot)

    if snapshot.hvac_mode == HvacMode.OFF:
        return (
            DesiredState(call_for_heat=False, trvs=_with_mode(addressed, HvacMode.OFF)),
            state,
        )

    if snapshot.window_open:
        return (
            DesiredState(
                call_for_heat=snapshot.call_for_heat,
                trvs=_with_mode(addressed, HvacMode.OFF),
            ),
            state,
        )

    # Lower tiers (calibrate/passthrough) are pulled in by subsequent
    # tickets; until then no intent is produced for the heating branch.
    return DesiredState(call_for_heat=snapshot.call_for_heat), state
