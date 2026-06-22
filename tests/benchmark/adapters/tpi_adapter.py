"""Adapter wrapping the existing TPI controller.

TPI emits a duty cycle. For the benchmark, the duty cycle is interpreted
as an equivalent steady-state valve fraction over the simulator step —
i.e. ``duty_cycle_pct`` is fed directly to the plant as ``valve_percent``.
This is the standard interpretation when the duty cycle's period is short
relative to the simulator step.
"""

from __future__ import annotations

from itertools import count
from typing import Any

from custom_components.better_thermostat.utils.calibration import tpi as tpi_mod
from custom_components.better_thermostat.utils.calibration.tpi import (
    TpiInput,
    TpiParams,
    _TpiState,
    compute_tpi,
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
        """Drop learned state. ``prior`` is unused."""
        _ = prior
        self._state = _TpiState()
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
            return BenchmarkOutput(
                duty_cycle_pct=ctx.last_valve_percent,
                valve_percent=ctx.last_valve_percent,
                diagnostics={"early_exit": True},
            )
        return BenchmarkOutput(
            duty_cycle_pct=float(out.duty_cycle_pct),
            valve_percent=float(out.duty_cycle_pct),
            diagnostics=dict(out.debug) if out.debug else {},
        )

    def export_state(self) -> dict[str, Any]:
        """Return the (small) TPI state as a serialisable dict."""
        return {"last_percent": self._state.last_percent}
