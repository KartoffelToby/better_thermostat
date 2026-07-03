"""Adapter wrapping the existing TPI controller.

TPI emits a duty cycle. For the benchmark, the duty cycle is interpreted
as an equivalent steady-state valve fraction over the simulator step —
i.e. ``duty_cycle_pct`` is fed directly to the plant as ``valve_percent``.
This is the standard interpretation when the duty cycle's period is short
relative to the simulator step.
"""

from __future__ import annotations

from dataclasses import asdict
from itertools import count
from typing import Any

from custom_components.better_thermostat.utils.calibration import tpi as tpi_mod
from custom_components.better_thermostat.utils.calibration.tpi import (
    TpiInput,
    TpiParams,
    _TpiState,
    compute_tpi,
)
from custom_components.better_thermostat.utils.state_manager import (
    _make_json_safe,
    deserialize_tpi,
)

from .base import BenchmarkContext, BenchmarkOutput, ControllerFamily

# Controller state is caller-owned; the adapter threads its own ``_state``
# through compute_tpi, so instances never share learned state.
_KEY_COUNTER = count()


class TpiAdapter:
    """Benchmark adapter for the production TPI controller."""

    name: str = "tpi"
    family: ControllerFamily = "duty"

    def __init__(self, params: TpiParams | None = None, key: str | None = None) -> None:
        self._params = params if params is not None else TpiParams()
        self._state: _TpiState = _TpiState()
        self._key = key if key is not None else f"bench:trv:tpi{next(_KEY_COUNTER)}"
        self._sim_time_s: float = 0.0
        self._original_monotonic = tpi_mod.monotonic

    def _virtualise_time(self) -> None:
        tpi_mod.monotonic = lambda: self._sim_time_s

    def _restore_time(self) -> None:
        tpi_mod.monotonic = self._original_monotonic

    def reset(self, prior: dict[str, Any] | None = None) -> None:
        """Reset the adapter, optionally rehydrating persisted state.

        Production persists TPI state across Home Assistant restarts
        (``StateManager`` store); passing a prior ``export_state()``
        snapshot replays that behaviour through the production
        ``deserialize_tpi`` path. Without ``prior`` the adapter
        cold-starts.
        """
        self._state = deserialize_tpi(prior) if prior else _TpiState()
        self._sim_time_s = 0.0

    def step(self, ctx: BenchmarkContext) -> BenchmarkOutput:
        """Compute one TPI step for the given benchmark context."""
        self._sim_time_s = ctx.t
        self._virtualise_time()
        try:
            inp = TpiInput(
                key=self._key,
                target_temp_C=ctx.target_temp_C,
                current_temp_C=ctx.current_temp_C,
                outdoor_temp_C=ctx.outdoor_temp_C,
                window_open=ctx.window_open,
                heating_allowed=True,
                bt_name="benchmark",
                entity_id="bench_trv",
            )
            out, self._state = compute_tpi(inp, self._params, state=self._state)
        finally:
            self._restore_time()

        if out is None:
            # ``compute_tpi``'s contract allows None (no recommendation);
            # production then skips the TPI result for this cycle. The
            # benchmark has no fallback controller, so map it to a zero
            # duty cycle — the same floor the window-open path emits.
            return BenchmarkOutput(
                duty_cycle_pct=0.0, valve_percent=0.0, diagnostics={"early_exit": True}
            )
        return BenchmarkOutput(
            duty_cycle_pct=float(out.duty_cycle_pct),
            valve_percent=float(out.duty_cycle_pct),
            diagnostics=dict(out.debug) if out.debug else {},
        )

    def export_state(self) -> dict[str, Any]:
        """Return a serializable snapshot of the wrapped TPI state."""
        return _make_json_safe(asdict(self._state))
