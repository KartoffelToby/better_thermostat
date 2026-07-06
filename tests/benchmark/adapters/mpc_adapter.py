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
from custom_components.better_thermostat.utils.state_manager import (
    _make_json_safe,
    deserialize_mpc,
)

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
        self._all_states: dict[str, _MpcState] = {}
        # ``uid:entity`` prefix of the production state key. The full key
        # appends the per-target bucket in ``step`` (see ``_bucket_key``),
        # mirroring ``build_mpc_key``.
        self._key = key if key is not None else f"bench{next(_KEY_COUNTER)}:trv"
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
        """Reset the adapter, optionally rehydrating persisted state.

        Production persists per-bucket MPC state across Home Assistant
        restarts (``StateManager`` store); passing a prior
        ``export_state()`` snapshot replays that behaviour through the
        production ``deserialize_mpc`` path. Without ``prior`` the
        adapter cold-starts.
        """
        self._all_states.clear()
        if prior:
            for bucket_key, raw in prior.items():
                if isinstance(raw, dict):
                    self._all_states[bucket_key] = deserialize_mpc(raw)
        self._sim_time_s = 0.0
        self._rng.seed(_MPC_RNG_SEED)

    def _bucket_key(self, target_temp_C: float) -> str:
        """Return the production-shaped per-target-bucket state key.

        Mirrors ``build_mpc_key``: MPC state is partitioned by the target
        temperature rounded to 0.5 K, so a setpoint move across a bucket
        boundary allocates a fresh state that ``compute_mpc`` seeds from
        its nearest sibling via ``all_states``.
        """
        bucket = f"t{round(float(target_temp_C) * 2.0) / 2.0:.1f}"
        return f"{self._key}:{bucket}"

    def step(self, ctx: BenchmarkContext) -> BenchmarkOutput:
        """Compute one MPC step for the given benchmark context."""
        self._sim_time_s = ctx.t
        key = self._bucket_key(ctx.target_temp_C)
        state = self._all_states.setdefault(key, _MpcState())
        self._virtualise()
        try:
            inp = MpcInput(
                key=key,
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
            out, new_state = compute_mpc(
                inp, self._params, state=state, all_states=self._all_states
            )
            self._all_states[key] = new_state
        finally:
            self._restore()

        if out is None:
            # ``compute_mpc``'s contract allows None (no recommendation);
            # production then skips the MPC result for this cycle. The
            # benchmark has no fallback controller, so map it to a closed
            # valve — the same floor the window-open path emits.
            return BenchmarkOutput(valve_percent=0.0, diagnostics={"early_exit": True})
        return BenchmarkOutput(
            valve_percent=float(out.valve_percent),
            diagnostics=dict(out.debug) if out.debug else {},
        )

    def export_state(self) -> dict[str, Any]:
        """Return a serializable snapshot of all per-bucket MPC states."""
        return {
            bucket_key: _make_json_safe(asdict(state))
            for bucket_key, state in self._all_states.items()
        }
