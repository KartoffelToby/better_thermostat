"""MPC v2 per-room runtime state and its persistence round-trip."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
import logging
from typing import Any

from .controller import ControllerSnapshot, MpcV2Controller
from .params import MpcV2Params

_LOGGER = logging.getLogger(__name__)


@dataclass
class MpcV2State:
    """Per-room runtime state — owned by the caller, persisted via RestoreEntity."""

    controller: MpcV2Controller | None = None
    last_percent: float | None = None
    last_compute_ts: float = 0.0
    created_ts: float = 0.0
    # Signature of the plant prior used to construct the cached controller.
    # When the caller passes new MpcV2Params whose prior differs from this
    # signature (e.g. user switched preset), the controller is rebuilt so
    # the new prior actually takes effect.
    plant_signature: tuple[float, ...] | None = None
    # Latched once the controller falls back to a hardcoded outdoor temp;
    # used to throttle the WARN to one line per controller instance.
    outdoor_fallback_logged: bool = False


def _plant_signature_of(params: MpcV2Params) -> tuple[float, ...]:
    p = params.plant
    return (
        round(p.tau_room_min, 3),
        round(p.tau_rad_min, 3),
        round(p.gain_heater, 4),
        round(p.coupling_rad_room, 4),
    )


def export_mpc_v2_state(state: MpcV2State) -> dict[str, Any] | None:
    """Return a JSON-serialisable snapshot of a single live v2 state.

    Returns ``None`` when the state has no controller yet — there is nothing
    worth persisting, so the caller skips the write.
    """
    if state.controller is None:
        return None
    return {
        "last_percent": state.last_percent,
        "last_compute_ts": state.last_compute_ts,
        "created_ts": state.created_ts,
        "outdoor_fallback_logged": state.outdoor_fallback_logged,
        "snapshot": asdict(state.controller.export_snapshot()),
    }


def import_mpc_v2_state(
    payload: Mapping[str, Any], params: MpcV2Params | None = None
) -> MpcV2State:
    """Rehydrate a single live v2 state from a previously exported payload.

    A fresh ``MpcV2Controller`` is constructed and seeded via
    :meth:`MpcV2Controller.restore_snapshot`. When ``params`` is ``None`` the
    controller boots with defaults — the caller is expected to recompute soon
    after with the correct params.
    """
    state = MpcV2State()
    for attr in ("last_percent", "last_compute_ts", "created_ts"):
        value = payload.get(attr)
        if value is not None:
            try:
                setattr(state, attr, float(value))
            except TypeError, ValueError, OverflowError:
                pass
    # The fallback-WARN latch is per controller instance; restoring it keeps
    # the throttle intact across the export/import round-trip the dispatcher
    # performs every cycle (otherwise the WARN fires on every compute).
    state.outdoor_fallback_logged = bool(payload.get("outdoor_fallback_logged", False))
    snapshot = payload.get("snapshot")
    if not isinstance(snapshot, Mapping):
        return state
    effective_params = params or MpcV2Params()
    controller = MpcV2Controller(effective_params)
    try:
        snap = ControllerSnapshot.from_mapping(snapshot)
        if snap is not None:
            controller.restore_snapshot(snap)
        state.controller = controller
        # Record the prior the controller was built with so a later
        # preset/plant-prior change trips the rebuild guard in compute_mpc_v2.
        state.plant_signature = _plant_signature_of(effective_params)
    except Exception as err:
        _LOGGER.debug("MPC v2 restore_snapshot failed: %s", err)
    return state
