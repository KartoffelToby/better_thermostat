"""Safety hull at the command boundary.

``clamp(desired, snapshot)`` is a pure pass that enforces absolute
limits on every intent right before it is written to a device — no
matter what the controller upstream computed:

* setpoints stay inside the TRV's reported min/max temperature range
  (the min bound doubles as the frost-protection floor),
* calibration offsets stay inside the device's local calibration range,
* valve percentages stay inside 0..valve_max_opening,
* optionally, valve changes are rate-limited against the previous
  intent (``max_valve_jump``); this mechanism ships disabled so today's
  write behavior is unchanged until the reconciler activates it.
"""

from __future__ import annotations

from dataclasses import replace

from .desired import DesiredState, TrvDesired
from .snapshot import TrvReported, WorldSnapshot


def _clamp_value(value: float, lower: float | None, upper: float | None) -> float:
    if lower is not None and value < lower:
        return lower
    if upper is not None and value > upper:
        return upper
    return value


def _clamp_trv(
    intent: TrvDesired,
    reported: TrvReported | None,
    previous: TrvDesired | None,
    max_valve_jump: float | None,
) -> TrvDesired:
    setpoint = intent.setpoint
    if setpoint is not None and reported is not None:
        setpoint = _clamp_value(setpoint, reported.min_temp, reported.max_temp)

    offset = intent.offset
    if offset is not None and reported is not None:
        offset = _clamp_value(
            offset, reported.local_calibration_min, reported.local_calibration_max
        )

    valve = intent.valve_percent
    if valve is not None:
        upper = reported.valve_max_opening if reported is not None else 100.0
        valve = _clamp_value(valve, 0.0, upper if upper is not None else 100.0)
        if (
            max_valve_jump is not None
            and previous is not None
            and previous.valve_percent is not None
        ):
            delta = valve - previous.valve_percent
            if abs(delta) > max_valve_jump:
                valve = previous.valve_percent + (
                    max_valve_jump if delta > 0 else -max_valve_jump
                )

    if (
        setpoint == intent.setpoint
        and valve == intent.valve_percent
        and offset == intent.offset
    ):
        return intent
    return replace(intent, setpoint=setpoint, valve_percent=valve, offset=offset)


def clamp(
    desired: DesiredState,
    snapshot: WorldSnapshot,
    *,
    previous: DesiredState | None = None,
    max_valve_jump: float | None = None,
) -> DesiredState:
    """Enforce absolute limits on every TRV intent.

    The frost-protection floor (the TRV's min temperature) applies to
    every intent that carries a setpoint — including future HOLD and
    PASSTHROUGH rungs, which express their targets the same way.
    """
    clamped = {
        entity_id: _clamp_trv(
            intent,
            snapshot.trvs.get(entity_id),
            previous.trvs.get(entity_id) if previous is not None else None,
            max_valve_jump,
        )
        for entity_id, intent in desired.trvs.items()
    }
    return DesiredState(call_for_heat=desired.call_for_heat, trvs=clamped)
