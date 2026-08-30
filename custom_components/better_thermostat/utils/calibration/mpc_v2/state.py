"""MPC v2 per-room runtime state and its persistence round-trip."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
import logging
import math
from typing import Any

from .controller import ControllerSnapshot, MpcV2Controller
from .params import MpcV2Params

_LOGGER = logging.getLogger(__name__)


@dataclass
class MpcV2State:
    """Per-room runtime state — owned by the caller, persisted via the StateManager store."""

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


# Relative per-component drift below this fraction is absorbed without a
# controller rebuild. The AUTO prior re-derives ``tau_room_min`` from the
# learned ``heat_loss_rate``, which moves a little after every completed
# idle-cooling cycle; rebuilding on each tick would discard the observer
# state (Kalman, DOB, integral) several times a day. Preset switches move
# the signature far beyond this tolerance and still trigger a rebuild.
_SIGNATURE_REL_TOL = 0.1


def plant_signature_differs(old: tuple[float, ...], new: tuple[float, ...]) -> bool:
    """Return ``True`` when the plant prior moved enough to warrant a rebuild.

    Compares component-wise against :data:`_SIGNATURE_REL_TOL` relative to the
    build-time value ``old``. Because the stored signature stays anchored at
    the params the controller was built with, slow cumulative drift eventually
    crosses the tolerance and rebuilds exactly once.
    """
    if len(old) != len(new):
        return True
    return any(
        abs(n - o) > _SIGNATURE_REL_TOL * max(abs(o), 1e-9) for o, n in zip(old, new)
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
    payload: Mapping[str, Any],
    params: MpcV2Params | None = None,
    *,
    key: str | None = None,
) -> MpcV2State:
    """Rehydrate a single live v2 state from a previously exported payload.

    A fresh ``MpcV2Controller`` is constructed and seeded via
    :meth:`MpcV2Controller.restore_snapshot`. When ``params`` is ``None`` the
    controller boots with defaults — the caller is expected to recompute soon
    after with the correct params.

    Parameters
    ----------
    payload : Mapping[str, Any]
        the exported state to rehydrate
    params : MpcV2Params | None
        parameters for the rebuilt controller, defaults when None
    key : str | None
        names the state entry the payload belongs to, so a report about an
        unusable value can point at the room rather than at nothing

    Returns
    -------
    MpcV2State
        the rehydrated state, with any unusable field left at its default
    """
    state = MpcV2State()
    for attr in ("last_percent", "last_compute_ts", "created_ts"):
        value = payload.get(attr)
        if value is not None:
            try:
                number = float(value)
                # `float()` takes "NaN", "Infinity" and anything that
                # overflows to one, and the contract above says an unusable
                # field keeps its default. A non-finite command or timestamp
                # poisons every calculation that reads it afterwards, so it
                # goes down the same refusal path as an unreadable one.
                if not math.isfinite(number):
                    raise ValueError(f"{attr} is not finite: {value!r}")
                setattr(state, attr, number)
            except TypeError, ValueError, OverflowError:
                # The field keeps the default a first start leaves there, so
                # a value the store lost is indistinguishable from one it
                # never held unless this line says so.
                _LOGGER.warning(
                    "MPC v2 stored %s for %s is not a usable number, "
                    "continuing without it",
                    attr,
                    key or "an unnamed state entry",
                    exc_info=True,
                )
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
    except Exception:
        # A snapshot that cannot be rehydrated means the stored state is
        # corrupt. The fallback is a fresh controller, which is also what a
        # first start produces — so the two are told apart by this line rather
        # than by the resulting state. The room re-learns from the default
        # instead of running on half-restored state.
        _LOGGER.warning(
            "MPC v2 controller state could not be restored, starting fresh",
            exc_info=True,
        )
    return state
