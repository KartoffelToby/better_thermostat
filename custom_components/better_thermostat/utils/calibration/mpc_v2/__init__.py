"""MPC v2 — receding-horizon QP controller for direct-valve TRVs.

Entry point:

    compute_mpc_v2(inp, params, state) -> (MpcV2Output | None, MpcV2State)

Internals live in :mod:`mpc_v2_internals` (plant model, Kalman observer,
Smith predictor, disturbance observer, RLS identifier, reference governor,
QP optimiser). Scope is direct-valve TRVs only; the indirect (offset-based)
TRV family is out of scope.
"""

from __future__ import annotations

from .compute import compute_mpc_v2
from .controller import SNAPSHOT_VERSION, ControllerSnapshot, MpcV2Controller
from .io import MpcV2Diagnostics, MpcV2Input, MpcV2Output
from .params import PLANT_PRESETS, MpcV2Params, make_plant_prior
from .state import MpcV2State, export_mpc_v2_state, import_mpc_v2_state

__all__ = [
    "MpcV2Input",
    "MpcV2Output",
    "MpcV2Diagnostics",
    "MpcV2Params",
    "MpcV2State",
    "MpcV2Controller",
    "ControllerSnapshot",
    "compute_mpc_v2",
    "export_mpc_v2_state",
    "import_mpc_v2_state",
    "make_plant_prior",
    "PLANT_PRESETS",
    "SNAPSHOT_VERSION",
]
