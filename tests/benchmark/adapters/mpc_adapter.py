"""Adapter wrapping the existing MPC controller in custom_components.better_thermostat.

The MPC's ``compute_mpc`` function uses module-level ``time()`` calls to
compute deltas. For deterministic, fast simulation we virtualise time by
monkey-patching the module's ``time`` symbol with our own counter for the
duration of each step. This affects only this process and is restored on
adapter destruction.

This is benchmark-only code: never imported by production.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from custom_components.better_thermostat.utils.calibration import mpc as mpc_mod
from custom_components.better_thermostat.utils.calibration.mpc import (
    MpcInput,
    MpcParams,
    _MpcState,
    compute_mpc,
)
from custom_components.better_thermostat.utils.state_manager import _make_json_safe

from .base import BenchmarkContext, BenchmarkOutput, ControllerFamily


class MpcAdapter:
    """Benchmark adapter for the production MPC controller."""

    name: str = "mpc"
    family: ControllerFamily = "valve"

    def __init__(
        self, params: MpcParams | None = None, key: str = "bench:trv:t0"
    ) -> None:
        self._params = params if params is not None else MpcParams()
        self._state: _MpcState = _MpcState()
        self._key = key
        self._sim_time_s: float = 0.0
        self._original_time = mpc_mod.time

    def _virtualise_time(self) -> None:
        mpc_mod.time = lambda: self._sim_time_s

    def _restore_time(self) -> None:
        mpc_mod.time = self._original_time

    def reset(self, prior: dict[str, Any] | None = None) -> None:
        """Drop learned state. ``prior`` is unused."""
        _ = prior
        self._state = _MpcState()
        self._sim_time_s = 0.0

    def step(self, ctx: BenchmarkContext) -> BenchmarkOutput:
        """Compute one MPC step for the given benchmark context."""
        self._sim_time_s = ctx.t
        self._virtualise_time()
        try:
            inp = MpcInput(
                key=self._key,
                target_temp_C=ctx.target_temp_C,
                current_temp_C=ctx.current_temp_C,
                trv_temp_C=ctx.trv_temp_C,
                outdoor_temp_C=ctx.outdoor_temp_C,
                window_open=ctx.window_open,
                solar_intensity=ctx.solar_intensity,
                heating_allowed=True,
                bt_name="benchmark",
                entity_id="bench_trv",
            )
            out, self._state = compute_mpc(
                inp, self._params, state=self._state, all_states={}
            )
        finally:
            self._restore_time()

        if out is None:
            # Early exit (e.g. window-open, missing temp). Hold previous output.
            return BenchmarkOutput(
                valve_percent=ctx.last_valve_percent, diagnostics={"early_exit": True}
            )
        return BenchmarkOutput(
            valve_percent=float(out.valve_percent),
            diagnostics=dict(out.debug) if out.debug else {},
        )

    def export_state(self) -> dict[str, Any]:
        """Return a serializable snapshot of the wrapped MPC state."""
        return _make_json_safe(asdict(self._state))
