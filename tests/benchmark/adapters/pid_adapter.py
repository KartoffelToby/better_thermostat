"""Adapter wrapping the existing PID controller.

PID's signature differs from the others (positional args, no Input dataclass).
This adapter normalises it to the common protocol shape.

Deliberate simplifications relative to the production call site: the raw
sensor reading is passed as ``inp_room_temperature_filtered`` (production feeds
its maintained EMA ``room_temperature_filtered``), and the temperature slope is a
two-point finite difference (production passes its own ``temperature_slope``).
Both stand-ins converge on the production values for the noise-free,
fixed-step scenarios the benchmark runs.
"""

from __future__ import annotations

from itertools import count
from typing import Any

from custom_components.better_thermostat.utils.calibration import pid as pid_mod
from custom_components.better_thermostat.utils.calibration.pid import (
    PIDParams,
    PIDState,
    compute_pid,
)
from custom_components.better_thermostat.utils.state_manager import (
    deserialize_pid,
    write_pid_state,
)

from .base import BenchmarkContext, BenchmarkOutput, ControllerFamily

# Controller state is caller-owned; the adapter threads its own ``_state``
# through compute_pid, so instances never share learned state.
_KEY_COUNTER = count()


class PidAdapter:
    """Benchmark adapter for the production PID controller."""

    name: str = "pid"
    family: ControllerFamily = "valve"

    def __init__(self, params: PIDParams | None = None, key: str | None = None) -> None:
        self._params = params if params is not None else PIDParams()
        self._state: PIDState = PIDState()
        self._key = key if key is not None else f"bench:trv:pid{next(_KEY_COUNTER)}"
        self._sim_time_s: float = 0.0
        self._original_monotonic = pid_mod.monotonic
        self._prev_temp: float | None = None
        self._prev_t: float | None = None

    def _virtualise_time(self) -> None:
        pid_mod.monotonic = lambda: self._sim_time_s

    def _restore_time(self) -> None:
        pid_mod.monotonic = self._original_monotonic

    def reset(self, prior: dict[str, Any] | None = None) -> None:
        """Reset the adapter, optionally rehydrating persisted state.

        Production persists PID state across Home Assistant restarts
        (``StateManager`` store); passing a prior ``export_state()``
        snapshot replays that behaviour through the production
        ``deserialize_pid`` path. Without ``prior`` the adapter
        cold-starts.
        """
        self._state = deserialize_pid(prior) if prior else PIDState()
        self._sim_time_s = 0.0
        self._prev_temp = None
        self._prev_t = None

    def _estimate_slope(self, ctx: BenchmarkContext) -> float | None:
        if self._prev_temp is None or self._prev_t is None:
            self._prev_temp = ctx.room_temperature
            self._prev_t = ctx.t
            return None
        dt_min = (ctx.t - self._prev_t) / 60.0
        slope: float | None = None
        if dt_min > 0.0:
            slope = (ctx.room_temperature - self._prev_temp) / dt_min
        self._prev_temp = ctx.room_temperature
        self._prev_t = ctx.t
        return slope

    def step(self, ctx: BenchmarkContext) -> BenchmarkOutput:
        """Compute one PID step for the given benchmark context."""
        self._sim_time_s = ctx.t
        self._virtualise_time()
        slope = self._estimate_slope(ctx)
        try:
            percent, debug, self._state = compute_pid(
                params=self._params,
                inp_target_temperature=ctx.target_temperature,
                inp_room_temperature=ctx.room_temperature,
                inp_trv_temperature=ctx.trv_temperature,
                inp_temperature_slope_K_per_min=slope,
                key=self._key,
                inp_room_temperature_filtered=ctx.room_temperature,
                state=self._state,
            )
        finally:
            self._restore_time()

        return BenchmarkOutput(
            valve_percent=float(percent), diagnostics=dict(debug) if debug else {}
        )

    def export_state(self) -> dict[str, Any]:
        """Return a serializable snapshot of the wrapped PID state."""
        return dict(write_pid_state(self._state))
