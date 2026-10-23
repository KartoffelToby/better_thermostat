"""Adapter wrapping the MPC v2 controller in custom_components.better_thermostat.

``compute_mpc_v2`` takes an explicit ``now`` argument, so — unlike the v1
adapter — no module-level time virtualisation is needed: the simulation clock
is passed straight through. Controller state is caller-owned; each adapter
keeps its own ``MpcV2State`` so concurrent instances never share learned state.

The plant prior is built the way production builds it for a room on the AUTO
preset before any heat-loss learning or offline re-identification exists:
``make_plant_prior()`` with no inputs. With ``configure_flow_temperature`` the
prior carries the flow temperature an installer enters for the room, the
context's ``nominal_flow_temperature``; without it the prior keeps the default.

This is benchmark-only code: never imported by production.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from itertools import count
import math
from typing import Any, override

from custom_components.better_thermostat.utils.calibration.mpc_v2 import (
    MpcV2Input,
    MpcV2Params,
    MpcV2State,
    compute_mpc_v2,
    export_mpc_v2_state,
    import_mpc_v2_state,
    make_plant_prior,
)
from custom_components.better_thermostat.utils.calibration.mpc_v2.controller import (
    MpcV2Controller,
)
from custom_components.better_thermostat.utils.calibration.mpc_v2_internals.report_band import (
    REPORT_STEP_WINDOW,
)
from custom_components.better_thermostat.utils.state_manager import (
    MpcV2StateData,
    write_mpc_v2_state,
)

from ..sensor import SensorParams
from .base import BenchmarkContext, BenchmarkOutput, ControlCycleGate, ControllerFamily

# Controller state is caller-owned; each adapter keeps its own key so
# concurrent instances never share learned state.
_KEY_COUNTER = count()


class MpcV2Adapter(ControlCycleGate):
    """Benchmark adapter for the MPC v2 (QP + Kalman) controller."""

    name: str = "mpc_v2"
    family: ControllerFamily = "valve"

    def __init__(
        self,
        params: MpcV2Params | None = None,
        key: str | None = None,
        configure_flow_temperature: bool = False,
    ) -> None:
        params = params if params is not None else MpcV2Params(plant=make_plant_prior())
        # The controller reads the plant prior by reference, so a private copy
        # lets the configured flow temperature reach it without touching the
        # caller's object.
        self._params = replace(params, plant=replace(params.plant))
        self._configure_flow_temperature = configure_flow_temperature
        if configure_flow_temperature:
            self.name = "mpc_v2+flow_temperature"
        self._state = MpcV2State()
        self._key = key if key is not None else f"bench:trv:mpc_v2{next(_KEY_COUNTER)}"

    def reset(self, prior: dict[str, Any] | None = None) -> None:
        """Reset to a cold start, or rehydrate from a prior export.

        The runner's restart path passes a previously exported snapshot via
        ``prior`` so restart scenarios resume statefully instead of
        cold-starting (which would bias the benchmark).
        """
        self._held = None
        if prior:
            self._state = import_mpc_v2_state(prior, self._params, key=self._key)
        else:
            self._state = MpcV2State()

    @override
    def _compute(self, ctx: BenchmarkContext) -> BenchmarkOutput:
        """Compute one MPC v2 step for the given benchmark context.

        The valve the plant received on the previous step goes in as
        ``applied_valve_percent``, as production passes the last confirmed write
        of a single direct-valve TRV.
        """
        if (
            self._configure_flow_temperature
            and ctx.nominal_flow_temperature is not None
        ):
            self._params.plant.T_water = ctx.nominal_flow_temperature
        inp = MpcV2Input(
            key=self._key,
            target_temperature=ctx.target_temperature,
            room_temperature=ctx.room_temperature,
            trv_temperature=ctx.trv_temperature,
            outdoor_temperature=ctx.outdoor_temperature,
            window_open=ctx.window_open,
            heating_allowed=True,
            applied_valve_percent=ctx.last_valve_percent,
            bt_name="benchmark",
            entity_id="bench_trv",
        )
        out, self._state = compute_mpc_v2(
            inp, self._params, state=self._state, now=ctx.t
        )
        if out is None:
            # ``compute_mpc_v2`` returns None for an open window or missing
            # input; production then skips the MPC result for this cycle. The
            # benchmark has no fallback controller, so map it to a closed
            # valve, as the v1 adapter does.
            return BenchmarkOutput(valve_percent=0.0, diagnostics={"early_exit": True})
        return BenchmarkOutput(
            valve_percent=float(out.valve_percent), diagnostics=asdict(out.diagnostics)
        )

    def adopt_sensor(self, sensor: SensorParams) -> None:
        """Start with the room sensor's reporting threshold already learned.

        The report band is filled with the smallest step the sensor reports,
        which is where its estimate settles after days of readings. The
        sensor rounds to its resolution before it compares against the
        threshold, so that step is the first multiple of the resolution that
        reaches the threshold.
        """
        threshold, resolution = sensor.report_threshold_K, sensor.resolution_K
        if resolution > 0.0:
            multiples = max(1, math.ceil(threshold / resolution - 1e-9))
            step = round(multiples * resolution, 6)
        else:
            step = threshold
        if self._state.controller is None:
            self._state.controller = MpcV2Controller(self._params)
        self._state.controller.report_band.restore(None, [step] * REPORT_STEP_WINDOW)

    def export_state(self) -> dict[str, Any]:
        """Return a serializable snapshot of the wrapped MPC v2 state."""
        exported = export_mpc_v2_state(self._state)
        if exported is None:
            return {}
        return dict(write_mpc_v2_state(MpcV2StateData(**exported)))
