"""Adapter wrapping the existing PID controller.

PID's signature differs from the others (positional args, no Input dataclass).
This adapter normalises it to the common protocol shape. Like the production
call site it passes the committed room temperature, its EMA and the slope the
EMA tick derives.
"""

from __future__ import annotations

from itertools import count
from typing import Any, override

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

from .base import BenchmarkContext, BenchmarkOutput, ControlCycleGate, ControllerFamily

# Controller state is caller-owned; the adapter threads its own ``_state``
# through compute_pid, so instances never share learned state.
_KEY_COUNTER = count()


class PidAdapter(ControlCycleGate):
    """Benchmark adapter for the production PID controller."""

    name: str = "pid"
    family: ControllerFamily = "valve"

    def __init__(self, params: PIDParams | None = None, key: str | None = None) -> None:
        self._params = params if params is not None else PIDParams()
        self._state: PIDState = PIDState()
        self._key = key if key is not None else f"bench:trv:pid{next(_KEY_COUNTER)}"
        self._sim_time_s: float = 0.0
        self._original_monotonic = pid_mod.monotonic

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
        self._held = None
        self._state = deserialize_pid(prior) if prior else PIDState()
        self._sim_time_s = 0.0

    @override
    def _compute(self, ctx: BenchmarkContext) -> BenchmarkOutput:
        """Compute one PID step for the given benchmark context."""
        self._sim_time_s = ctx.t
        self._virtualise_time()
        try:
            percent, debug, self._state = compute_pid(
                params=self._params,
                inp_target_temperature=ctx.target_temperature,
                inp_room_temperature=ctx.room_temperature,
                inp_trv_temperature=ctx.trv_temperature,
                inp_temperature_slope_K_per_min=ctx.temperature_slope_K_per_min,
                key=self._key,
                inp_room_temperature_filtered=ctx.room_temperature_filtered,
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
