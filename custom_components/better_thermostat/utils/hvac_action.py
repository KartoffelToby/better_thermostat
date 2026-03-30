"""HVAC action computation with tolerance-based hysteresis."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from homeassistant.components.climate.const import HVACAction, HVACMode

_LOGGER = logging.getLogger(__name__)


@dataclass
class ToleranceHysteresis:
    """Mutable hysteresis state for the tolerance state-machine."""

    last_action: HVACAction = field(default=HVACAction.IDLE)
    hold_active: bool = False


@dataclass(frozen=True)
class HvacActionResult:
    """Immutable result of a single HVAC-action computation."""

    action: HVACAction
    tolerance_decision: HVACAction
    new_last_action: HVACAction
    new_hold_active: bool


@dataclass(frozen=True)
class TrvSnapshot:
    """Pre-resolved, immutable view of a single TRV's relevant state."""

    trv_id: str
    ignore_trv_states: bool = False
    hvac_action: str | None = None
    valve_position: float | None = None
    last_valve_percent: float | None = None


def to_pct(val: float | str | None) -> float | None:
    """Normalise a valve value to percent (0-100).

    Values in [0, 1) are treated as fractions and multiplied by 100.
    Values >= 1 are returned as-is (already percent).
    Returns *None* for non-numeric / unparseable input.
    """
    if val is None:
        return None
    try:
        v = float(val)
    except (TypeError, ValueError):
        return None
    return v * 100.0 if 0.0 <= v < 1.0 else v


def should_heat_with_tolerance(
    cur_temp: float,
    target_temp: float,
    tolerance: float,
    previous_action: HVACAction | None,
) -> bool:
    """Hysteresis decision: should we heat right now?

    Band: ``[target - tolerance, target)``
    * Start heating when ``cur_temp < target - tolerance``.
    * Continue heating (if already heating) until ``cur_temp >= target``.
    * Stop at ``target`` – never heat *above* target.
    """
    tolerance = max(0.0, tolerance)
    heat_off_threshold = target_temp
    heat_on_threshold = target_temp - tolerance
    if previous_action == HVACAction.HEATING:
        return cur_temp < heat_off_threshold
    return cur_temp < heat_on_threshold


_VALVE_THRESH = 0.0


def compute_hvac_action(
    hysteresis: ToleranceHysteresis,
    cur_temp: float | None,
    target_temp: float | None,
    cool_target: float | None,
    hvac_mode: HVACMode,
    bt_hvac_mode: HVACMode,
    window_open: bool,
    tolerance: float,
    ignore_states: bool,
    trv_snapshots: list[TrvSnapshot],
    device_name: str = "",
) -> HvacActionResult:
    """Compute the current HVAC action without mutating *hysteresis*.

    Rules
    -----
    - OFF mode → OFF regardless of temperatures.
    - Open window → IDLE (suppresses active heating/cooling).
    - Heating uses a hysteresis band ``[target - tolerance, target]``.
    - Cooling when ``heat_cool`` and ``cur_temp > cool_target + tolerance``.
    - Otherwise IDLE, unless a TRV explicitly reports heating.
    """
    prev_action = hysteresis.last_action

    if target_temp is None or cur_temp is None:
        return HvacActionResult(
            action=HVACAction.IDLE,
            tolerance_decision=HVACAction.IDLE,
            new_last_action=HVACAction.IDLE,
            new_hold_active=False,
        )

    if HVACMode.OFF in (hvac_mode, bt_hvac_mode):
        return HvacActionResult(
            action=HVACAction.OFF,
            tolerance_decision=HVACAction.OFF,
            new_last_action=HVACAction.IDLE,
            new_hold_active=False,
        )

    if window_open:
        return HvacActionResult(
            action=HVACAction.IDLE,
            tolerance_decision=HVACAction.IDLE,
            new_last_action=HVACAction.IDLE,
            new_hold_active=False,
        )

    # Tolerance-based heating decision
    heating_allowed = hvac_mode in (HVACMode.HEAT, HVACMode.HEAT_COOL)
    action = HVACAction.IDLE
    tolerance_hold = False

    if heating_allowed:
        if should_heat_with_tolerance(cur_temp, target_temp, tolerance, prev_action):
            action = HVACAction.HEATING
        else:
            tolerance_hold = True

    tolerance_decision = action

    # Cooling decision
    if (
        hvac_mode == HVACMode.HEAT_COOL
        and cool_target is not None
        and cur_temp > (cool_target + tolerance)
    ):
        action = HVACAction.COOLING
        tolerance_hold = False

    # TRV override: if base decision is IDLE but any TRV is active, show HEATING
    if action == HVACAction.IDLE:
        if ignore_states or window_open:
            return HvacActionResult(
                action=HVACAction.IDLE,
                tolerance_decision=tolerance_decision,
                new_last_action=HVACAction.IDLE,
                new_hold_active=tolerance_hold,
            )

        for snap in trv_snapshots:
            if snap.ignore_trv_states:
                continue

            if snap.hvac_action is not None:
                action_str = str(snap.hvac_action).lower()
                if action_str == "heating":
                    _LOGGER.debug(
                        "better_thermostat %s: overriding hvac_action to HEATING "
                        "(TRV %s reports heating)",
                        device_name,
                        snap.trv_id,
                    )
                    action = HVACAction.HEATING
                    break

            vp_pct = to_pct(snap.valve_position)
            if vp_pct is not None and vp_pct > _VALVE_THRESH:
                _LOGGER.debug(
                    "better_thermostat %s: overriding hvac_action to HEATING "
                    "(valve_position %.1f%%, TRV %s)",
                    device_name,
                    vp_pct,
                    snap.trv_id,
                )
                action = HVACAction.HEATING
                break

            last_pct = to_pct(snap.last_valve_percent)
            if last_pct is not None and last_pct > _VALVE_THRESH:
                _LOGGER.debug(
                    "better_thermostat %s: overriding hvac_action to HEATING "
                    "(last_valve_percent %.1f%%, TRV %s)",
                    device_name,
                    last_pct,
                    snap.trv_id,
                )
                action = HVACAction.HEATING
                break

    # Hysteresis state follows tolerance_decision, not the TRV-overridden action.
    # A TRV still physically heating must not keep the state machine in the
    # lenient "was-heating" mode, which would cause heating past target + tolerance.
    new_last_action = (
        HVACAction.HEATING
        if tolerance_decision == HVACAction.HEATING
        else HVACAction.IDLE
    )
    new_hold_active = bool(tolerance_hold and action != HVACAction.COOLING)

    return HvacActionResult(
        action=action,
        tolerance_decision=tolerance_decision,
        new_last_action=new_last_action,
        new_hold_active=new_hold_active,
    )
