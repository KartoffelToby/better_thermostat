"""Adapter wrapping the existing TPI controller.

TPI emits a duty cycle. For the benchmark, the duty cycle is interpreted
as an equivalent steady-state valve fraction over the simulator step —
i.e. ``duty_cycle_percent`` is fed directly to the plant as ``valve_percent``.
This is the standard interpretation when the duty cycle's period is short
relative to the simulator step.
"""

from __future__ import annotations

from itertools import count
from typing import Any, override

from custom_components.better_thermostat.utils.calibration import tpi as tpi_mod
from custom_components.better_thermostat.utils.calibration.tpi import (
    TpiInput,
    TpiParams,
    _TpiState,
    compute_tpi,
)
from custom_components.better_thermostat.utils.state_manager import (
    deserialize_tpi,
    write_tpi_state,
)

from .base import BenchmarkContext, BenchmarkOutput, ControlCycleGate, ControllerFamily

# Controller state is caller-owned; the adapter threads its own ``_state``
# through compute_tpi, so instances never share learned state.
_KEY_COUNTER = count()


class TpiAdapter(ControlCycleGate):
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
        self._held = None
        self._state = deserialize_tpi(prior) if prior else _TpiState()
        self._sim_time_s = 0.0

    @override
    def _compute(self, ctx: BenchmarkContext) -> BenchmarkOutput:
        """Compute one TPI step for the given benchmark context."""
        self._sim_time_s = ctx.t
        self._virtualise_time()
        try:
            inp = TpiInput(
                key=self._key,
                target_temperature=ctx.target_temperature,
                room_temperature=ctx.room_temperature,
                outdoor_temperature=ctx.outdoor_temperature,
                window_open=ctx.window_open,
                heating_allowed=True,
                bt_name="benchmark",
                entity_id="bench_trv",
            )
            out, self._state = compute_tpi(inp, self._params, state=self._state)
        finally:
            self._restore_time()

        return BenchmarkOutput(
            duty_cycle_percent=float(out.duty_cycle_percent),
            valve_percent=float(out.duty_cycle_percent),
            diagnostics=dict(out.debug) if out.debug else {},
        )

    def export_state(self) -> dict[str, Any]:
        """Return a serializable snapshot of the wrapped TPI state."""
        return dict(write_tpi_state(self._state))
