"""Adapter wrapping the MPC v2 controller in custom_components.better_thermostat.

``compute_mpc_v2`` takes an explicit ``now`` argument, so — unlike the v1
adapter — no module-level time virtualisation is needed: the simulation clock
is passed straight through. Controller state is caller-owned; each adapter
keeps its own ``MpcV2State`` so concurrent instances never share learned state.

Requires the ``daqp`` QP solver (a production requirement of MPC v2). The
runner only registers this adapter when ``daqp`` is importable, so the
benchmark stays runnable without it.

This is benchmark-only code: never imported by production.
"""

from __future__ import annotations

from dataclasses import asdict
from itertools import count
from typing import Any

from custom_components.better_thermostat.utils.calibration.mpc_v2 import (
    MpcV2Input,
    MpcV2Params,
    MpcV2State,
    compute_mpc_v2,
    export_mpc_v2_state,
    import_mpc_v2_state,
)
from custom_components.better_thermostat.utils.state_manager import _make_json_safe

from .base import BenchmarkContext, BenchmarkOutput, ControllerFamily

_KEY_COUNTER = count()


class MpcV2Adapter:
    """Benchmark adapter for the MPC v2 (QP + Kalman) controller."""

    name: str = "mpc_v2"
    family: ControllerFamily = "valve"

    def __init__(
        self, params: MpcV2Params | None = None, key: str | None = None
    ) -> None:
        self._params = params if params is not None else MpcV2Params()
        self._state: MpcV2State = MpcV2State()
        self._key = key if key is not None else f"bench:trv:mpc_v2{next(_KEY_COUNTER)}"

    def reset(self, prior: dict[str, Any] | None = None) -> None:
        """Reset to a cold start, or rehydrate from a prior export.

        The runner's restart path passes a previously exported snapshot via
        ``prior`` so restart scenarios resume statefully instead of
        cold-starting (which would bias the benchmark).
        """
        if prior:
            self._state = import_mpc_v2_state(prior, self._params)
        else:
            self._state = MpcV2State()

    def step(self, ctx: BenchmarkContext) -> BenchmarkOutput:
        """Compute one MPC v2 step for the given benchmark context."""
        inp = MpcV2Input(
            key=self._key,
            target_temp_C=ctx.target_temp_C,
            current_temp_C=ctx.current_temp_C,
            trv_temp_C=ctx.trv_temp_C,
            outdoor_temp_C=ctx.outdoor_temp_C,
            window_open=ctx.window_open,
            heating_allowed=True,
            bt_name="benchmark",
            entity_id="bench_trv",
        )
        out, self._state = compute_mpc_v2(
            inp, self._params, state=self._state, now=ctx.t
        )
        if out is None:
            # Early exit (e.g. window-open, missing temp). Hold previous output.
            return BenchmarkOutput(
                valve_percent=ctx.last_valve_percent, diagnostics={"early_exit": True}
            )
        return BenchmarkOutput(
            valve_percent=float(out.valve_percent), diagnostics=asdict(out.diagnostics)
        )

    def export_state(self) -> dict[str, Any]:
        """Return a serializable snapshot of the wrapped MPC v2 state."""
        exported = export_mpc_v2_state(self._state)
        return _make_json_safe(exported) if exported is not None else {}
