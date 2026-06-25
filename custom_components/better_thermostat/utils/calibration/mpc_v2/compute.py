"""MPC v2 entry point: one control cycle from input to percent recommendation."""

from __future__ import annotations

import logging
import math
from time import time

from .controller import MpcV2Controller
from .io import MpcV2Input, MpcV2Output
from .params import MpcV2Params
from .state import MpcV2State, _plant_signature_of

_LOGGER = logging.getLogger(__name__)

# When the user has no outdoor sensor we fall back to this value (°C) so the
# QP and DOB still have a defined operating point. A warm-ish German winter
# day average — close enough that the steady-state input is still in the
# valid range; well off-target temps make ``u_ss`` saturate, which the
# reference governor catches.
OUTDOOR_TEMP_FALLBACK_C = 10.0


def _all_finite(*values: float | None) -> bool:
    """Return ``True`` when every non-None value passes ``math.isfinite``."""
    for v in values:
        if v is None:
            continue
        if not math.isfinite(v):
            return False
    return True


def compute_mpc_v2(
    inp: MpcV2Input,
    params: MpcV2Params,
    state: MpcV2State | None = None,
    *,
    now: float | None = None,
) -> tuple[MpcV2Output | None, MpcV2State]:
    """Run one v2 cycle and return a percent recommendation + updated state.

    Early-exits to ``(None, state)`` when essential inputs are missing — the
    caller treats this as "hold last value".

    ``now`` overrides the wall-clock used as the controller's ``t_s``.
    Production callers leave it ``None`` (real ``time.time()``); tests
    pass a synthetic value so realistic dt-driven behaviour (DOB)
    can be exercised without sleeping.
    """
    if now is None:
        now = time()
    if state is None:
        state = MpcV2State()
    if state.created_ts == 0.0:
        state.created_ts = now

    if (
        inp.current_temp_C is None
        or inp.target_temp_C is None
        or not inp.heating_allowed
        or inp.window_open
    ):
        return None, state

    # Reject non-finite sensor inputs. Without this guard a NaN propagates
    # through Kalman/QP and poisons the cached state — a single bad reading
    # would require restarting the integration to recover.
    if not _all_finite(
        inp.current_temp_C, inp.target_temp_C, inp.outdoor_temp_C, inp.trv_temp_C
    ):
        _LOGGER.warning(
            "better_thermostat %s: MPC v2 (%s) non-finite input "
            "(current=%s target=%s outdoor=%s trv=%s) — holding last command",
            inp.bt_name or "BT",
            inp.entity_id or inp.key,
            inp.current_temp_C,
            inp.target_temp_C,
            inp.outdoor_temp_C,
            inp.trv_temp_C,
        )
        return None, state

    new_signature = _plant_signature_of(params)
    if (
        state.controller is not None
        and state.plant_signature is not None
        and state.plant_signature != new_signature
    ):
        _LOGGER.info(
            "MPC v2 plant prior changed for %s (%s → %s); rebuilding controller",
            inp.key,
            state.plant_signature,
            new_signature,
        )
        state.controller = None

    if state.controller is None:
        state.controller = MpcV2Controller(params)
        state.plant_signature = new_signature

    if inp.outdoor_temp_C is None:
        T_outdoor = OUTDOOR_TEMP_FALLBACK_C
        if not state.outdoor_fallback_logged:
            _LOGGER.warning(
                "better_thermostat %s: MPC v2 (%s) no outdoor_temp_C — falling "
                "back to %.1f °C. Configure an outdoor sensor for accurate "
                "feed-forward (u_ss).",
                inp.bt_name or "BT",
                inp.entity_id or inp.key,
                T_outdoor,
            )
            state.outdoor_fallback_logged = True
    else:
        T_outdoor = inp.outdoor_temp_C

    u, diag = state.controller.step(
        t_s=now,
        T_room_C=inp.current_temp_C,
        T_target_C=inp.target_temp_C,
        T_outdoor_C=T_outdoor,
        T_rad_C=inp.trv_temp_C,
    )

    percent_int = round(max(0.0, min(1.0, u)) * 100.0)
    if inp.max_opening_pct is not None:
        percent_int = min(percent_int, int(inp.max_opening_pct))

    # Feed the actually-applied (possibly capped) fraction back so the observer
    # and rate limiter track the real valve input, not the uncapped request.
    if state.controller is not None:
        state.controller.set_applied_u(percent_int / 100.0)

    state.last_percent = float(percent_int)
    state.last_compute_ts = now

    if _LOGGER.isEnabledFor(logging.DEBUG):
        _LOGGER.debug(
            "better_thermostat %s: MPC v2 (%s) target=%.2f current=%.2f trv=%s "
            "outdoor=%s -> valve=%d%% (T_rad_hat=%.2f D_hat=%.4f tau_room=%.0f) key=%s",
            inp.bt_name or "BT",
            inp.entity_id or inp.key,
            inp.target_temp_C,
            inp.current_temp_C,
            inp.trv_temp_C,
            inp.outdoor_temp_C,
            percent_int,
            diag.T_rad_hat,
            diag.D_hat_K_per_min,
            diag.tau_room_min,
            inp.key,
        )

    return MpcV2Output(valve_percent=percent_int, diagnostics=diag), state
