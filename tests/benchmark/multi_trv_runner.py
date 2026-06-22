"""Multi-TRV scenario runner.

Wraps :class:`MultiTrvPlant` with the same simulation loop that
:func:`runner.run_scenario` uses for single-TRV plants, but with one
extra step: the controller's u_total is distributed across the parallel
radiators via BT's production ``distribute_valve_percent`` heuristic.

This way the simulator measures how a single-output controller plus
BT's distribution logic combine — which is the actual production
behaviour, not an idealised perfect-allocation scenario.
"""

from __future__ import annotations

from custom_components.better_thermostat.utils.calibration.mpc import (
    distribute_valve_percent,
)

from .adapters.base import BenchmarkContext, ControllerAdapter
from .adapters.baselines import IdealOracleAdapter
from .metrics import compute_metrics
from .multi_trv_plant import MultiTrvPlant, MultiTrvPlantParams, MultiTrvPlantState
from .plant import PlantParams
from .reporter import ScenarioResult
from .runner import _drive_adapter, _plant_time_scale
from .scenarios import ScenarioConfig


def _equivalent_single_plant(params: MultiTrvPlantParams) -> PlantParams:
    """Build an equivalent single-TRV plant for time-scale + oracle parametrisation.

    Aggregates gains as a sum (parallel radiators contribute additively)
    and uses the per-TRV time constants directly.
    """
    return PlantParams(
        tau_room_min=params.tau_room_min,
        tau_rad_min=params.tau_rad_min,
        gain_heater=sum(params.gain_heaters),
        coupling_rad_room=sum(params.coupling_rad_room),
        T_water_C=params.T_water_C,
    )


def make_multi_trv_adapter(
    name: str, plant_params: MultiTrvPlantParams
) -> ControllerAdapter:
    """Build a benchmark adapter pre-configured for a multi-TRV plant.

    Plant-aware adapters (currently only ``ideal_oracle``) receive an
    aggregated equivalent plant — sum of gain_heater, sum of
    coupling_rad_room — so their steady-state math is well-posed.
    Other controllers get their default constructor.
    """
    from .runner import _make_adapter

    return _make_adapter(name, _equivalent_single_plant(plant_params))


def _stabilise_multi_trv(
    plant: MultiTrvPlant,
    scenario: ScenarioConfig,
    stabilisation_min: float,
    step_s: float,
) -> None:
    """Pre-warm the multi-TRV plant via the IdealOracle.

    The oracle is parametrised with the *equivalent* single-TRV plant
    (sum of gains, coupling) so its steady-state inversion is reasonable.
    The distribution heuristic is applied so all radiators warm together.
    """
    if stabilisation_min <= 0.0:
        return
    pre_setpoint = scenario.setpoint_schedule(0.0)
    pre_outdoor = scenario.outdoor_schedule(0.0)
    equiv = _equivalent_single_plant(plant.params)
    oracle = IdealOracleAdapter(plant_params=equiv)
    steps = int(round(stabilisation_min * 60.0 / step_s))
    for _ in range(steps):
        ctx = BenchmarkContext(
            t=0.0,
            dt=step_s,
            target_temp_C=pre_setpoint,
            current_temp_C=plant.state.T_room_C,
            raw_room_temp_C=plant.state.T_room_C,
            trv_temp_C=sum(plant.state.T_rads_C) / plant.params.n_trvs,
            outdoor_temp_C=pre_outdoor,
        )
        out = oracle.step(ctx)
        u_total = (out.valve_percent or 0.0) / 100.0
        # Even pre-warm uses distribute_valve_percent so the radiator
        # state diverges naturally with the sensor offsets.
        u_per_trv = _distribute(u_total * 100.0, plant)
        plant.step(step_s, u_per_trv, pre_outdoor)


def _distribute(u_total_pct: float, plant: MultiTrvPlant) -> list[float]:
    """Call BT's distribute_valve_percent and return per-radiator u in [0,1]."""
    trv_temps: dict[str, float | None] = {
        f"trv_{i}": t for i, t in enumerate(plant.reported_trv_temps())
    }
    distribution = distribute_valve_percent(u_total_pct, trv_temps)
    return [distribution[f"trv_{i}"] / 100.0 for i in range(plant.params.n_trvs)]


class _MultiTrvFacade:
    """Wrap :class:`MultiTrvPlant` for the shared ``_drive_adapter`` loop."""

    def __init__(self, plant: MultiTrvPlant) -> None:
        self._plant = plant

    @property
    def T_room_C(self) -> float:
        return self._plant.state.T_room_C

    @property
    def T_rad_C(self) -> float:
        # Single-radiator view = mean over the parallel radiators.
        return sum(self._plant.state.T_rads_C) / self._plant.params.n_trvs

    def apply(
        self, dt_s: float, valve_pct: float, T_outdoor_C: float, Q_K_per_min: float
    ) -> None:
        u_per_trv = _distribute(valve_pct, self._plant)
        self._plant.step(dt_s, u_per_trv, T_outdoor_C, Q_K_per_min=Q_K_per_min)


def run_multi_trv_scenario(
    adapter: ControllerAdapter,
    scenario: ScenarioConfig,
    plant_params: MultiTrvPlantParams,
    initial_state: MultiTrvPlantState,
    step_s: float = 30.0,
    stabilisation_min: float = 60.0,
) -> ScenarioResult:
    """Run a scenario against a multi-TRV plant with BT's distribute_valve_percent."""
    if step_s <= 0.0:
        raise ValueError("step_s must be > 0")
    plant = MultiTrvPlant(plant_params, initial_state)
    effective_stabilisation_min = (
        scenario.stabilisation_min
        if scenario.stabilisation_min is not None
        else stabilisation_min
    )
    _stabilise_multi_trv(plant, scenario, effective_stabilisation_min, step_s)

    equiv = _equivalent_single_plant(plant_params)
    time_scale = _plant_time_scale(equiv)
    duration_s = max(
        scenario.duration_min * 60.0, scenario.duration_min * 60.0 * time_scale
    )
    facade = _MultiTrvFacade(plant)
    series = _drive_adapter(
        adapter, facade, scenario, step_s, duration_s, handle_controller_restart=False
    )
    return ScenarioResult(
        controller=adapter.name,
        scenario=scenario.name + " [multi-TRV]",
        metrics=compute_metrics(series, scenario.transient_start_s),
    )
