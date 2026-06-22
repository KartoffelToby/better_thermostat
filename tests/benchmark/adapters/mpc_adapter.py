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
from itertools import count
import random
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

# Controller state is caller-owned; each adapter keeps its own ``all_states``
# map so concurrent instances never share learned state.
_KEY_COUNTER = count()

# Fixed seed for the MPC's hybrid-learning RNG (``random.random()`` in
# mpc.py). Re-applied on every reset so each scenario sees the same
# forced-calibration realisation regardless of run order — the benchmark's
# determinism guarantee extends to MPC's stochastic learning path.
_MPC_RNG_SEED = 1_234_567


class MpcAdapter:
    """Benchmark adapter for the production MPC controller."""

    name: str = "mpc"
    family: ControllerFamily = "valve"

    def __init__(self, params: MpcParams | None = None, key: str | None = None) -> None:
        self._params = params if params is not None else MpcParams()
        self._state: _MpcState = _MpcState()
        self._all_states: dict[str, _MpcState] = {}
        self._key = key if key is not None else f"bench:trv:mpc{next(_KEY_COUNTER)}"
        self._sim_time_s: float = 0.0
        self._original_time = mpc_mod.time
        # Deterministic stand-in for the module-global ``random`` that
        # mpc.py uses for its hybrid-learning forced calibration.
        self._rng = random.Random(_MPC_RNG_SEED)
        self._original_random = mpc_mod.random

    def _virtualise(self) -> None:
        """Swap the mpc module's time + random for deterministic stand-ins."""
        mpc_mod.time = lambda: self._sim_time_s
        mpc_mod.random = self._rng

    def _restore(self) -> None:
        """Restore the mpc module's real time + random symbols."""
        mpc_mod.time = self._original_time
        mpc_mod.random = self._original_random

    def reset(self, prior: dict[str, Any] | None = None) -> None:
        """Drop learned state. ``prior`` is unused."""
        _ = prior
        self._state = _MpcState()
        self._all_states.clear()
        self._sim_time_s = 0.0
        self._rng.seed(_MPC_RNG_SEED)

    def step(self, ctx: BenchmarkContext) -> BenchmarkOutput:
        """Compute one MPC step for the given benchmark context."""
        self._sim_time_s = ctx.t
        self._virtualise()
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
                inp, self._params, state=self._state, all_states=self._all_states
            )
        finally:
            self._restore()

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
