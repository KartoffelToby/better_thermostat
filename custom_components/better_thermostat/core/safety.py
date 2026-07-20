"""Safety hull at the command boundary.

``clamp(desired, snapshot)`` is a pure pass that enforces absolute
limits on every intent right before it is written to a device — no
matter what the controller upstream computed:

* non-finite setpoint and offset intents (NaN/inf) are withheld
  entirely — the hull expresses "no command" as ``None``,
* setpoints stay inside the TRV's reported min/max temperature range
  (the min bound doubles as the frost-protection floor); when the TRV
  reports no usable bound, conservative fallback bounds apply,
* calibration offsets stay inside the device's local calibration range;
  when the device reports no usable range, conservative fallback bounds
  apply,
* valve percentages stay inside 0..valve_max_opening,
* optionally, valve changes are rate-limited against the previous
  intent (``max_valve_jump``); this mechanism ships disabled so today's
  write behavior is unchanged until the reconciler activates it.
"""

from __future__ import annotations

from dataclasses import replace
import math

from .desired import DesiredState, TrvDesired
from .snapshot import TrvReported, WorldSnapshot

# Fallback setpoint bounds for TRVs that report no usable min/max: the
# floor keeps the frost-protection semantics of the min bound, the cap
# stays inside what radiator hardware commonly accepts.
FALLBACK_MIN_SETPOINT = 4.5
FALLBACK_MAX_SETPOINT = 35.0

# Fallback calibration-offset bounds for TRVs that report no usable
# local calibration range, matching the widest span common TRV firmware
# accepts (typically ±12.7 K).
FALLBACK_MIN_OFFSET = -12.0
FALLBACK_MAX_OFFSET = 12.0


def _finite_bound(value: float | None) -> float | None:
    """Return ``value`` only when it is a finite bound, else ``None``.

    A NaN/inf bound would compare False against every value and silently
    disable the corresponding clamp; dropping it falls back to the
    caller's default instead.
    """
    return value if value is not None and math.isfinite(value) else None


def _clamp_value(value: float, lower: float | None, upper: float | None) -> float:
    lower = _finite_bound(lower)
    upper = _finite_bound(upper)
    if lower is not None and upper is not None and lower > upper:
        # A misreporting device can invert its bounds; swapping restores
        # a well-formed interval instead of clamping against lower > upper.
        lower, upper = upper, lower
    if not math.isfinite(value):
        # NaN/inf compare False against every bound, so they would slip through
        # the inequality checks below and reach a device as an invalid payload.
        if lower is not None:
            return lower
        if upper is not None:
            return upper
        return 0.0
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
    if setpoint is not None:
        if not math.isfinite(setpoint):
            # A non-finite setpoint carries no target at all; the hull
            # withholds the write instead of inventing one.
            setpoint = None
        else:
            lower = _finite_bound(reported.min_temp if reported is not None else None)
            upper = _finite_bound(reported.max_temp if reported is not None else None)
            setpoint = _clamp_value(
                setpoint,
                lower if lower is not None else FALLBACK_MIN_SETPOINT,
                upper if upper is not None else FALLBACK_MAX_SETPOINT,
            )

    offset = intent.offset
    if offset is not None:
        if not math.isfinite(offset):
            # A non-finite offset carries no correction at all; the hull
            # withholds the write instead of inventing one.
            offset = None
        else:
            lower = _finite_bound(
                reported.local_calibration_min if reported is not None else None
            )
            upper = _finite_bound(
                reported.local_calibration_max if reported is not None else None
            )
            offset = _clamp_value(
                offset,
                lower if lower is not None else FALLBACK_MIN_OFFSET,
                upper if upper is not None else FALLBACK_MAX_OFFSET,
            )

    valve = intent.valve_percent
    if valve is not None:
        upper = _finite_bound(
            reported.valve_max_opening if reported is not None else None
        )
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
                valve = _clamp_value(valve, 0.0, upper if upper is not None else 100.0)

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
