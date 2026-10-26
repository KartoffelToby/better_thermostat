"""Benchmark runner — entry point.

Usage::

    python -m tests.benchmark.runner                              # all x all
    python -m tests.benchmark.runner --controller mpc             # one controller
    python -m tests.benchmark.runner --scenario S01_setpoint_step_small
    python -m tests.benchmark.runner --controller mpc --scenario S01_setpoint_step_small
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import replace
import math
import sys
from typing import Any, Protocol
import zlib

from custom_components.better_thermostat.core.watchdog import CONTROL_TICK_S

from .actuator import Actuator, ActuatorParams, ActuatorProfile
from .adapters.base import BenchmarkContext, ControlCycleGate, ControllerAdapter
from .adapters.baselines import BangBangAdapter, IdealOracleAdapter, LinearPAdapter
from .adapters.heating_power_adapter import HeatingPowerAdapter
from .adapters.indirect_trv import (
    BOSCH_PARAMS,
    SONOFF_TRVZB_PARAMS,
    TADO_PARAMS,
    TUYA_PARAMS,
    IndirectTrvAdapter,
)
from .adapters.mpc_adapter import MpcAdapter
from .adapters.mpc_v2_adapter import MpcV2Adapter
from .adapters.passive_modes import (
    AggressiveCalibrationAdapter,
    DefaultCalibrationAdapter,
    NoCalibrationAdapter,
)
from .adapters.pid_adapter import PidAdapter
from .adapters.tpi_adapter import TpiAdapter
from .input_stage import RoomTemperatureInput
from .metrics import MetricValues, TimeSeries, compute_metrics
from .plant import (
    PROFILE_DOE_MIDRISE_APT,
    PROFILE_DOE_SFD_2004,
    PROFILE_DOE_SFD_2010,
    PROFILE_DOE_SFD_PRE1980,
    PROFILE_FAST_SMALL,
    PROFILE_LARGE_SLOW,
    PROFILE_REAL_KITCHEN,
    PROFILE_REAL_LIVING_ROOM,
    PROFILE_REAL_LIVING_ROOM_RC3,
    PROFILE_REALISTIC,
    PROFILE_STANDARD,
    PROFILE_STANDARD_RC3,
    PROFILE_UNDERFLOOR,
    PlantParams,
    PlantState,
    TwoStatePlant,
)
from .reporter import (
    ScenarioResult,
    ScoredResult,
    average_over_seeds,
    render_per_scenario,
    render_plant_sweep,
    render_score_matrix,
    score_results,
)
from .scenarios import ALL_SCENARIOS, ScenarioConfig
from .scoring import PROFILES, UserProfile
from .sensor import REALISTIC_SENSOR_PARAMS, Sensor, trv_sensor_reading

ADAPTER_FACTORIES: dict[str, Callable[[], ControllerAdapter]] = {
    "mpc": MpcAdapter,
    "mpc_v2": MpcV2Adapter,
    "mpc_v2+flow_temperature": lambda: MpcV2Adapter(configure_flow_temperature=True),
    "tpi": TpiAdapter,
    "pid": PidAdapter,
    "heating_power": HeatingPowerAdapter,
    # BT's offset-family modes: no smart controller, TRV-internal P-loop only.
    "default": DefaultCalibrationAdapter,
    "aggressive": AggressiveCalibrationAdapter,
    "no_calibration": NoCalibrationAdapter,
    "bangbang": BangBangAdapter,
    "linear_p": LinearPAdapter,
    "ideal_oracle": IdealOracleAdapter,
    # Indirect-TRV variants wrap an inner controller behind the TRV's
    # quantised setpoint / internal P-loop. The vendor-specific param
    # presets (TADO_PARAMS, BOSCH_PARAMS, TUYA_PARAMS, SONOFF_TRVZB_PARAMS)
    # live in adapters/indirect_trv.py.
    "pid+indirect_tado": lambda: IndirectTrvAdapter(PidAdapter(), TADO_PARAMS),
    "mpc+indirect_tado": lambda: IndirectTrvAdapter(MpcAdapter(), TADO_PARAMS),
    "mpc_v2+indirect_tado": lambda: IndirectTrvAdapter(MpcV2Adapter(), TADO_PARAMS),
    "tpi+indirect_tado": lambda: IndirectTrvAdapter(TpiAdapter(), TADO_PARAMS),
    "heating_power+indirect_tado": lambda: IndirectTrvAdapter(
        HeatingPowerAdapter(), TADO_PARAMS
    ),
    # Bosch BTH-RA — wider hysteresis + command latency.
    "pid+indirect_bosch": lambda: IndirectTrvAdapter(PidAdapter(), BOSCH_PARAMS),
    "mpc+indirect_bosch": lambda: IndirectTrvAdapter(MpcAdapter(), BOSCH_PARAMS),
    "mpc_v2+indirect_bosch": lambda: IndirectTrvAdapter(MpcV2Adapter(), BOSCH_PARAMS),
    "tpi+indirect_bosch": lambda: IndirectTrvAdapter(TpiAdapter(), BOSCH_PARAMS),
    "heating_power+indirect_bosch": lambda: IndirectTrvAdapter(
        HeatingPowerAdapter(), BOSCH_PARAMS
    ),
    # Tuya TS0601 family — 1 K setpoint quantisation.
    "pid+indirect_tuya": lambda: IndirectTrvAdapter(PidAdapter(), TUYA_PARAMS),
    "mpc+indirect_tuya": lambda: IndirectTrvAdapter(MpcAdapter(), TUYA_PARAMS),
    "mpc_v2+indirect_tuya": lambda: IndirectTrvAdapter(MpcV2Adapter(), TUYA_PARAMS),
    "tpi+indirect_tuya": lambda: IndirectTrvAdapter(TpiAdapter(), TUYA_PARAMS),
    "heating_power+indirect_tuya": lambda: IndirectTrvAdapter(
        HeatingPowerAdapter(), TUYA_PARAMS
    ),
    # Sonoff TRVZB offset-mode (post-FW 1.3).
    "pid+indirect_sonoff": lambda: IndirectTrvAdapter(
        PidAdapter(), SONOFF_TRVZB_PARAMS
    ),
    "mpc+indirect_sonoff": lambda: IndirectTrvAdapter(
        MpcAdapter(), SONOFF_TRVZB_PARAMS
    ),
    "mpc_v2+indirect_sonoff": lambda: IndirectTrvAdapter(
        MpcV2Adapter(), SONOFF_TRVZB_PARAMS
    ),
    "tpi+indirect_sonoff": lambda: IndirectTrvAdapter(
        TpiAdapter(), SONOFF_TRVZB_PARAMS
    ),
    "heating_power+indirect_sonoff": lambda: IndirectTrvAdapter(
        HeatingPowerAdapter(), SONOFF_TRVZB_PARAMS
    ),
}

PLANT_PROFILES: dict[str, PlantParams] = {
    "fast_small": PROFILE_FAST_SMALL,
    "standard": PROFILE_STANDARD,
    "large_slow": PROFILE_LARGE_SLOW,
    "underfloor": PROFILE_UNDERFLOOR,
    "real_living_room": PROFILE_REAL_LIVING_ROOM,
    "real_kitchen": PROFILE_REAL_KITCHEN,
    "standard_rc3": PROFILE_STANDARD_RC3,
    "real_living_room_rc3": PROFILE_REAL_LIVING_ROOM_RC3,
    "realistic": PROFILE_REALISTIC,
    "doe_sfd_pre1980": PROFILE_DOE_SFD_PRE1980,
    "doe_sfd_2004": PROFILE_DOE_SFD_2004,
    "doe_sfd_2010": PROFILE_DOE_SFD_2010,
    "doe_midrise_apt": PROFILE_DOE_MIDRISE_APT,
}


def _plant_time_scale(
    plant: PlantParams, reference: PlantParams = PROFILE_STANDARD
) -> float:
    """Adaptive time-scale factor for one plant relative to STANDARD.

    Combines the room, radiator and (in RC3 mode) wall time constants —
    radiator dominates the initial response, room+wall dominates the bulk
    of the settling — to produce a single dimensionless multiplier applied
    to time-based thresholds and to the scenario duration.
    """

    def _scale(p: PlantParams) -> float:
        # RC3: room and wall add up (heat must traverse both); RC2: just room.
        room_plus_wall = p.tau_room_min + max(p.tau_wall_min, 0.0)
        return room_plus_wall + 3.0 * p.tau_rad_min

    ref_scale = _scale(reference)
    if ref_scale <= 0.0:
        return 1.0
    return _scale(plant) / ref_scale


def flow_temperature_at(scenario: ScenarioConfig, t: float) -> float | None:
    """Return the flow temperature reaching the radiators at ``t``, in °C.

    ``None`` when the scenario keeps the plant's own ``T_water``.
    """
    if scenario.flow_temperature_schedule is None:
        return None
    return scenario.flow_temperature_schedule(t)


def _stabilise_plant(
    plant: TwoStatePlant,
    scenario: ScenarioConfig,
    stabilisation_min: float,
    step_s: float,
) -> None:
    """Pre-warm the plant to steady-state for the pre-step setpoint.

    Runs :class:`IdealOracleAdapter` for ``stabilisation_min`` minutes before
    the test controller takes over.
    """
    if stabilisation_min <= 0.0:
        return
    pre_setpoint = scenario.setpoint_schedule(0.0)
    pre_outdoor = scenario.outdoor_schedule(0.0)
    pre_flow = flow_temperature_at(scenario, 0.0)
    oracle = IdealOracleAdapter(plant_params=scenario.plant)
    steps = round(stabilisation_min * 60.0 / step_s)
    for _ in range(steps):
        ctx = BenchmarkContext(
            t=0.0,  # logical time; not exposed to test controller
            dt=step_s,
            target_temperature=pre_setpoint,
            room_temperature=plant.state.T_room,
            raw_room_temperature=plant.state.T_room,
            trv_temperature=plant.state.T_rad,
            outdoor_temperature=pre_outdoor,
            flow_temperature=pre_flow,
        )
        out = oracle.step(ctx)
        u = (out.valve_percent or 0.0) / 100.0
        plant.step(step_s, u, pre_outdoor, flow_temperature=pre_flow)


class PlantFacade(Protocol):
    """Minimal plant interface ``_drive_adapter`` works against.

    The benchmark drives both single-TRV (``TwoStatePlant``) and
    multi-TRV (``MultiTrvPlant``) simulators through the same loop. The
    facade adapts each plant's native shape (scalar valve vs. vector
    valves) to the loop's uniform "apply one valve_percent, see one room and
    one radiator temperature" view.
    """

    @property
    def T_room(self) -> float:
        """Current room air temperature, in °C."""
        ...

    @property
    def T_rad(self) -> float:
        """Single-radiator view of the plant (mean over radiators if multi-TRV), in °C."""
        ...

    @property
    def heat_to_room_K_per_min(self) -> float:
        """Heat all radiators currently deliver to the room, in room-K/min."""
        ...

    def apply(
        self,
        dt_s: float,
        valve_percent: float,
        T_outdoor: float,
        Q_K_per_min: float,
        flow_temperature: float | None,
        heat_delivered: bool,
    ) -> None:
        """Step the plant forward by ``dt_s`` seconds under the given valve, outdoor and heat-source inputs."""
        ...


class _SingleTrvFacade:
    """Wrap :class:`TwoStatePlant` for the shared drive loop."""

    def __init__(self, plant: TwoStatePlant, actuator: Actuator) -> None:
        self._plant = plant
        self._actuator = actuator

    @property
    def T_room(self) -> float:
        return self._plant.state.T_room

    @property
    def T_rad(self) -> float:
        return self._plant.state.T_rad

    @property
    def heat_to_room_K_per_min(self) -> float:
        return self._plant.heat_to_room_K_per_min

    def apply(
        self,
        dt_s: float,
        valve_percent: float,
        T_outdoor: float,
        Q_K_per_min: float,
        flow_temperature: float | None,
        heat_delivered: bool,
    ) -> None:
        u = self._actuator.apply(valve_percent)
        self._plant.step(
            dt_s,
            u,
            T_outdoor,
            Q_K_per_min=Q_K_per_min,
            flow_temperature=flow_temperature,
            heat_delivered=heat_delivered,
        )


def _drive_adapter(
    adapter: ControllerAdapter,
    facade: PlantFacade,
    scenario: ScenarioConfig,
    step_s: float,
    duration_s: float,
    handle_controller_restart: bool,
    learned_sensor: bool = False,
    nominal_flow_temperature: float | None = None,
) -> TimeSeries:
    """Drive an adapter through one scenario, returning the recorded series.

    Shared between :func:`run_scenario` and the multi-TRV runner — the
    only difference between the two is *how* the valve command reaches
    the plant, which the facade encapsulates. With ``learned_sensor`` an
    adapter that offers ``adopt_sensor`` is handed the room sensor's
    parameters before the first step. ``nominal_flow_temperature`` is the
    simulated plant's ``T_water``, which the context reports as the nominal
    flow temperature.
    """
    sensor_params = scenario.sensor_params or REALISTIC_SENSOR_PARAMS
    # Derive a stable per-scenario seed so sensor noise/jitter is
    # decorrelated across scenarios while staying fully reproducible.
    # ``zlib.crc32`` is used over the built-in ``hash`` because the latter
    # is salted per process (PYTHONHASHSEED) and would break determinism.
    seed_key = (
        scenario.name
        if scenario.noise_seed == 0
        else f"{scenario.name}#{scenario.noise_seed}"
    )
    sensor = Sensor(sensor_params, seed=zlib.crc32(seed_key.encode()))
    room_input = RoomTemperatureInput()
    adapter.reset()
    if learned_sensor:
        # A controller that learns the sensor then starts as one that has
        # watched it for days; the others have nothing to adopt.
        adopt = getattr(adapter, "adopt_sensor", None)
        if callable(adopt):
            adopt(sensor_params)

    t_s_list: list[float] = []
    T_room_list: list[float] = []
    T_setpoint_list: list[float] = []
    valve_percent_list: list[float] = []
    heat_to_room_list: list[float] = []

    t = 0.0
    last_valve_percent = 0.0
    restart_fired = False
    # What the previous step showed; a change in any of them is an event
    # that requests a control cycle.
    seen: tuple[int, float | None, float, bool] | None = None
    next_tick_t = 0.0

    while t <= duration_s + 1e-6:
        if (
            handle_controller_restart
            and scenario.controller_restart_t_s is not None
            and not restart_fired
            and t >= scenario.controller_restart_t_s
        ):
            # Simulate an HA restart: snapshot persistent state, drop
            # the in-RAM controller, rehydrate from storage. Adapters
            # without export_state get a true cold-start reset.
            stored: dict[str, Any] | None = None
            export = getattr(adapter, "export_state", None)
            if callable(export):
                # Let a genuinely broken export_state surface rather than
                # silently cold-starting; a non-dict return is still
                # tolerated (treated as no persisted state).
                candidate = export()
                if isinstance(candidate, dict):
                    stored = candidate
            adapter.reset(prior=stored)
            room_input.reset()
            seen = None
            restart_fired = True

        target = scenario.setpoint_schedule(t)
        T_outdoor = scenario.outdoor_schedule(t)
        window_open = (
            scenario.window_open_schedule(t)
            if scenario.window_open_schedule is not None
            else False
        )
        solar_intensity = (
            scenario.solar_intensity_schedule(t)
            if scenario.solar_intensity_schedule is not None
            else 0.0
        )
        controller_solar = (
            scenario.controller_solar_schedule(t)
            if scenario.controller_solar_schedule is not None
            else solar_intensity
        )
        flow_temperature = flow_temperature_at(scenario, t)
        heat_delivered = (
            scenario.heat_delivery_schedule(t)
            if scenario.heat_delivery_schedule is not None
            else True
        )
        # On dropout the sensor returns None and the input stage holds its
        # last committed value rather than handing over the plant truth.
        room_input.update(t, sensor.read(t, facade.T_room))
        trv_report = trv_sensor_reading(
            facade.T_room, facade.T_rad, scenario.trv_sensor_rad_fraction
        )
        # Before the first committed reading BT controls on the TRV's own
        # temperature, as its sensor-fallback rung does.
        T_measured = room_input.room_temperature
        if T_measured is None:
            T_measured = trv_report
        now_seen = (room_input.commits, trv_report, target, window_open)
        control_event = now_seen != seen
        seen = now_seen
        control_tick = t >= next_tick_t
        if control_tick:
            next_tick_t += CONTROL_TICK_S

        ctx = BenchmarkContext(
            t=t,
            dt=step_s,
            target_temperature=target,
            room_temperature=T_measured,
            raw_room_temperature=facade.T_room,
            trv_temperature=trv_report,
            outdoor_temperature=T_outdoor,
            window_open=window_open,
            solar_intensity=controller_solar,
            last_valve_percent=last_valve_percent,
            sensor_calibration_error_K=sensor.observed_calibration_error_K,
            room_temperature_filtered=room_input.room_temperature_filtered,
            temperature_slope_K_per_min=room_input.temperature_slope_K_per_min,
            trv_internal_temperature=trv_sensor_reading(
                facade.T_room,
                facade.T_rad,
                scenario.trv_sensor_rad_fraction,
                resolution_K=0.0,
            ),
            control_event=control_event,
            control_tick=control_tick,
            flow_temperature=(
                nominal_flow_temperature
                if flow_temperature is None
                else flow_temperature
            ),
            nominal_flow_temperature=nominal_flow_temperature,
            heat_delivered=heat_delivered,
        )

        out = adapter.step(ctx)
        # Better Thermostat turns every TRV off while a window is open,
        # whatever the calibration mode: the kernel's window region decides
        # that above the controller. The controller still runs and sees the
        # open window, but the plant gets a closed valve.
        valve_percent = (
            0.0 if window_open or out.valve_percent is None else out.valve_percent
        )
        if isinstance(adapter, ControlCycleGate):
            # BT writes a direct-valve command as a whole percent.
            valve_percent = float(round(valve_percent))
        last_valve_percent = valve_percent

        t_s_list.append(t)
        T_room_list.append(facade.T_room)
        T_setpoint_list.append(target)
        valve_percent_list.append(valve_percent)
        heat_to_room_list.append(facade.heat_to_room_K_per_min)

        Q_solar = solar_intensity * scenario.solar_max_K_per_min
        Q_window = -scenario.window_loss_K_per_min if window_open else 0.0
        facade.apply(
            step_s,
            valve_percent,
            T_outdoor,
            Q_K_per_min=Q_solar + Q_window,
            flow_temperature=flow_temperature,
            heat_delivered=heat_delivered,
        )
        t += step_s

    return TimeSeries(
        t_s=t_s_list,
        T_room=T_room_list,
        T_setpoint=T_setpoint_list,
        valve_percent=valve_percent_list,
        heat_to_room_K_per_min=heat_to_room_list,
    )


def run_scenario(
    adapter: ControllerAdapter,
    scenario: ScenarioConfig,
    step_s: float = 30.0,
    plant_params: PlantParams | None = None,
    stabilisation_min: float = 60.0,
    learned_sensor: bool = False,
) -> ScenarioResult:
    """Drive a controller through one scenario and return measured metrics.

    Parameters
    ----------
    adapter:
        The controller-under-test wrapped in its benchmark adapter.
    scenario:
        Scenario configuration.
    step_s:
        Simulator step size in seconds.
    plant_params:
        Optional override for the scenario's plant — reuses one scenario
        across multiple plant profiles. When ``None`` the scenario's
        default plant is used.
    stabilisation_min:
        Minutes to pre-warm the plant via :class:`IdealOracleAdapter` before
        the test adapter takes over. ``0`` disables stabilisation and gives
        the cold-start behaviour of earlier versions.
    learned_sensor:
        Start a controller that learns the room sensor's reporting
        threshold with that threshold already learned, as it stands after
        days in service. ``False`` starts it cold, as on the day it is
        installed.

    Raises
    ------
    ValueError
        If ``step_s`` is not positive.
    """
    if step_s <= 0.0:
        raise ValueError("step_s must be > 0")
    actual_plant = plant_params if plant_params is not None else scenario.plant
    plant = TwoStatePlant(
        actual_plant,
        PlantState(T_room=scenario.initial.T_room, T_rad=scenario.initial.T_rad),
    )
    # Run stabilisation with a scenario whose plant matches the override so
    # the IdealOracle inside the warm-up loop is parametrised correctly.
    effective_scenario = (
        scenario if plant_params is None else _replace_plant(scenario, actual_plant)
    )
    effective_stabilisation = (
        scenario.stabilisation_min
        if scenario.stabilisation_min is not None
        else stabilisation_min
    )
    _stabilise_plant(plant, effective_scenario, effective_stabilisation, step_s)

    actuator = Actuator(scenario.actuator_params or ActuatorParams())
    facade = _SingleTrvFacade(plant, actuator)

    # Scale the scenario duration so a slower plant gets proportionally
    # more time to settle. Never shrink below the scenario default.
    time_scale = _plant_time_scale(actual_plant)
    duration_s = max(
        scenario.duration_minutes * 60.0, scenario.duration_minutes * 60.0 * time_scale
    )
    series = _drive_adapter(
        adapter,
        facade,
        scenario,
        step_s,
        duration_s,
        handle_controller_restart=True,
        learned_sensor=learned_sensor,
        nominal_flow_temperature=actual_plant.T_water,
    )
    return ScenarioResult(
        controller=adapter.name,
        scenario=scenario.name,
        metrics=compute_metrics(series, scenario.transient_start_s),
    )


def _replace_plant(scenario: ScenarioConfig, plant: PlantParams) -> ScenarioConfig:
    """Return a copy of *scenario* with the plant attribute replaced."""
    from dataclasses import replace

    return replace(scenario, plant=plant)


#: Factories for adapters that accept ``plant_params=`` and should
#: receive the override. Other registered factories either ignore the
#: override (e.g. the RLS-learning variants are meant to discover the
#: plant from data) or take no constructor arguments.
PLANT_AWARE_FACTORIES: dict[str, Callable[[PlantParams], ControllerAdapter]] = {
    "ideal_oracle": lambda plant: IdealOracleAdapter(plant_params=plant)
}


def _make_adapter(name: str, plant_override: PlantParams | None) -> ControllerAdapter:
    """Instantiate an adapter, threading plant params into model-aware adapters."""
    plant_aware_factory = PLANT_AWARE_FACTORIES.get(name)
    if plant_override is not None and plant_aware_factory is not None:
        return plant_aware_factory(plant_override)
    return ADAPTER_FACTORIES[name]()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — parse args, run benchmarks, print report."""
    parser = argparse.ArgumentParser(description="Run the calibration benchmark.")
    parser.add_argument(
        "--controller",
        action="append",
        default=[],
        help=(
            "Controller(s) to run. Choices: "
            f"{sorted(ADAPTER_FACTORIES.keys())}. Repeat for multiple. "
            "Omit for all."
        ),
    )
    parser.add_argument(
        "--scenario",
        action="append",
        default=[],
        help="Scenario name(s). Repeat for multiple. Omit for all.",
    )
    parser.add_argument(
        "--step-s",
        type=float,
        default=30.0,
        help="Simulator step in seconds (default 30).",
    )
    parser.add_argument(
        "--plant",
        action="append",
        default=[],
        help=(
            "Plant profile(s) to override scenario default. Choices: "
            f"{sorted(PLANT_PROFILES.keys())}. Use 'all' for the full sweep."
        ),
    )
    parser.add_argument(
        "--stabilisation-min",
        type=float,
        default=60.0,
        help=(
            "Minutes to pre-warm the plant via IdealOracle before the test "
            "controller takes over (default 60). Set to 0 to disable."
        ),
    )
    parser.add_argument(
        "--profile",
        choices=list(PROFILES.keys()),
        default="balanced",
        help=(
            "User profile that weighs the overall score: balanced, "
            "comfort_first, longevity_first, energy_first. Default: balanced."
        ),
    )
    parser.add_argument(
        "--per-scenario",
        action="store_true",
        help="Also print a controller × scenario detail table under the matrix.",
    )
    parser.add_argument(
        "--plant-sweep",
        action="store_true",
        help=(
            "Cross-plant comparison preset: run on the realistic anchor plant "
            "plus the four DOE Reference Building envelope classes, with the "
            "realistic sensor and equal-percentage actuator overrides applied. "
            "Scenarios that pin a non-standard plant (UFH, boiler-limited, "
            "cooling, pipe-fill) are excluded — they carry plant-specific "
            "intent. Output is the per-plant score matrix plus a cross-plant "
            "overall summary."
        ),
    )
    parser.add_argument(
        "--seeds",
        type=int,
        default=1,
        help=(
            "Average every controller × scenario cell over this many sensor "
            "noise realisations (default 1). With --per-scenario, a second "
            "table shows each cell's spread (max − min) across them."
        ),
    )
    parser.add_argument(
        "--sensor-noise",
        type=float,
        default=None,
        metavar="K",
        help="Room sensor noise σ in K for every scenario (default: per scenario).",
    )
    parser.add_argument(
        "--report-threshold",
        type=float,
        default=None,
        metavar="K",
        help=(
            "Change in K that makes the room sensor report, for every scenario "
            "(default: per scenario)."
        ),
    )
    parser.add_argument(
        "--heartbeat-s",
        type=float,
        default=None,
        metavar="S",
        help=(
            "Seconds after which the room sensor reports anyway, for every "
            "scenario; 0 disables it (default: per scenario)."
        ),
    )
    parser.add_argument(
        "--learned-sensor",
        action="store_true",
        help=(
            "Start controllers that learn the room sensor's reporting threshold "
            "(mpc_v2) with it already learned, as after days in service. "
            "Without it every scenario is their first day."
        ),
    )
    parser.add_argument(
        "--multi-trv",
        action="store_true",
        help=(
            "In addition to the single-TRV matrix, run every controller "
            "through the three multi-TRV plant profiles (symmetric, "
            "asymmetric, heterogeneous) — three parallel radiators sharing "
            "one room state, valve %% distributed via BT's "
            "``distribute_valve_percent``. Scenarios that pin a non-standard "
            "plant are excluded."
        ),
    )
    args = parser.parse_args(argv)
    profile = PROFILES[args.profile]
    if args.step_s <= 0.0:
        print("--step-s must be > 0", file=sys.stderr)
        return 2
    if args.seeds < 1:
        print("--seeds must be >= 1", file=sys.stderr)
        return 2
    sensor_overrides = {
        field: value
        for field, value in (
            ("noise_std_K", args.sensor_noise),
            ("report_threshold_K", args.report_threshold),
            ("heartbeat_s", args.heartbeat_s),
        )
        if value is not None
    }
    if any(
        not math.isfinite(value) or value < 0.0 for value in sensor_overrides.values()
    ):
        print(
            "--sensor-noise, --report-threshold and --heartbeat-s must be finite "
            "and >= 0",
            file=sys.stderr,
        )
        return 2
    if (sensor_overrides or args.learned_sensor) and (
        args.plant_sweep or args.multi_trv
    ):
        print(
            "--sensor-noise, --report-threshold, --heartbeat-s and "
            "--learned-sensor cannot be combined with --plant-sweep or --multi-trv",
            file=sys.stderr,
        )
        return 2
    args.sensor_overrides = sensor_overrides
    if args.seeds > 1 and (args.plant or args.plant_sweep or args.multi_trv):
        print(
            "--seeds cannot be combined with --plant, --plant-sweep or --multi-trv",
            file=sys.stderr,
        )
        return 2

    controllers = args.controller or list(ADAPTER_FACTORIES.keys())
    scenarios = args.scenario or list(ALL_SCENARIOS.keys())

    # Validate names before any filtering indexes into the registries.
    for c in controllers:
        if c not in ADAPTER_FACTORIES:
            print(f"Unknown controller: {c}", file=sys.stderr)
            return 2
    for s in scenarios:
        if s not in ALL_SCENARIOS:
            print(f"Unknown scenario: {s}", file=sys.stderr)
            return 2

    # Resolve plant selection.
    plant_args: list[str] = args.plant or []
    if args.plant_sweep:
        # Realistic anchor + four DOE envelope classes. Restrict to scenarios
        # that don't pin their own plant — those carry plant-specific intent.
        plant_names = [
            "realistic",
            "doe_sfd_pre1980",
            "doe_sfd_2004",
            "doe_sfd_2010",
            "doe_midrise_apt",
        ]
        scenarios = [s for s in scenarios if ALL_SCENARIOS[s].plant is PROFILE_STANDARD]
        if not scenarios:
            print(
                "--plant-sweep: all selected scenarios pin their own plant; "
                "nothing to sweep",
                file=sys.stderr,
            )
    elif "all" in plant_args:
        plant_names = list(PLANT_PROFILES.keys())
    elif plant_args:
        plant_names = plant_args
    else:
        plant_names = ["__default__"]  # use each scenario's own plant

    for p in plant_names:
        if p != "__default__" and p not in PLANT_PROFILES:
            print(f"Unknown plant profile: {p}", file=sys.stderr)
            return 2

    # Under --plant-sweep, every scenario gets the noisy realistic sensor
    # and the equal-percentage actuator overlay so the comparison is at the
    # "realistic dwelling" operating point rather than the simulator's clean
    # defaults.
    def _scenario_for(name: str) -> ScenarioConfig:
        sc = ALL_SCENARIOS[name]
        if not args.plant_sweep:
            return _with_sensor_overrides(sc, sensor_overrides)
        from dataclasses import replace as _dc_replace

        return _dc_replace(
            sc,
            sensor_params=REALISTIC_SENSOR_PARAMS,
            actuator_params=ActuatorParams(
                profile=ActuatorProfile.EQUAL_PERCENTAGE, equal_percentage_exponent=3.0
            ),
        )

    # Pre-compute Oracle metrics per (scenario, plant) — the scorer
    # normalises everything against them.
    oracle_cache: dict[tuple[str, str], MetricValues] = {}
    for plant_name in plant_names:
        plant_override = (
            None if plant_name == "__default__" else PLANT_PROFILES[plant_name]
        )
        for s in scenarios:
            scen = _scenario_for(s)
            oracle_run = run_scenario(
                IdealOracleAdapter(plant_params=plant_override or scen.plant),
                scen,
                step_s=args.step_s,
                plant_params=plant_override,
                stabilisation_min=args.stabilisation_min,
            )
            oracle_cache[(plant_name, s)] = oracle_run.metrics

    grouped: dict[str, list[ScenarioResult]] = {}
    for plant_name in plant_names:
        plant_override = (
            None if plant_name == "__default__" else PLANT_PROFILES[plant_name]
        )
        label = (
            "scenario default" if plant_name == "__default__" else f"plant={plant_name}"
        )
        section: list[ScenarioResult] = []
        for c in controllers:
            for s in scenarios:
                scen = _scenario_for(s)
                # Plant-aware adapters (the oracle) must be built with the
                # scenario's own plant in default mode, not the standard
                # default — otherwise the reference row is suboptimal on
                # non-standard-plant scenarios. A fresh instance per
                # scenario is equivalent to the reset run_scenario performs.
                adapter_plant = (
                    plant_override if plant_override is not None else scen.plant
                )
                adapter = _make_adapter(c, adapter_plant)
                section.append(
                    run_scenario(
                        adapter,
                        scen,
                        step_s=args.step_s,
                        plant_params=plant_override,
                        stabilisation_min=args.stabilisation_min,
                        learned_sensor=args.learned_sensor,
                    )
                )
        grouped[label] = section

    # Key the oracle cache by (block_label, scenario) for the scorer.
    oracle_by_label: dict[tuple[str, str], MetricValues] = {}
    for plant_name in plant_names:
        label = (
            "scenario default" if plant_name == "__default__" else f"plant={plant_name}"
        )
        for s in scenarios:
            m = oracle_cache.get((plant_name, s))
            if m is not None:
                oracle_by_label[(label, s)] = m

    if args.plant_sweep:
        # Per-plant matrices plus a cross-plant summary table.
        scored_per_plant: dict[str, list[ScoredResult]] = {}
        for label, block in grouped.items():
            scored_per_plant[label] = score_results(
                {label: block}, oracle_by_label, profile
            )
        for label, items in scored_per_plant.items():
            print(f"\n[{label}]")
            print(render_score_matrix(items, profile))
        print(render_plant_sweep(scored_per_plant, profile))
    elif len(grouped) > 1:
        # Several plant blocks (e.g. --plant all or multiple --plant names):
        # render one labelled matrix per plant. Collapsing them into a single
        # matrix would average scores across unrelated plants into one
        # meaningless row, hiding which plant a result came from.
        for label, block in grouped.items():
            scored = score_results({label: block}, oracle_by_label, profile)
            print(f"\n[{label}]")
            print(render_score_matrix(scored, profile))
            if args.per_scenario:
                print(render_per_scenario(scored, profile))
    elif args.seeds > 1:
        scored, spread = _score_over_seeds(
            controllers, scenarios, args, profile, oracle_by_label, grouped
        )
        print(f"\n[single-TRV, mean of {args.seeds} noise seeds]")
        print(render_score_matrix(scored, profile))
        if args.per_scenario:
            print(render_per_scenario(scored, profile))
            print(
                render_per_scenario(
                    spread,
                    profile,
                    title=f"Per-scenario spread (max − min) over {args.seeds} seeds",
                )
            )
    else:
        scored = score_results(grouped, oracle_by_label, profile)
        print("\n[single-TRV]")
        print(render_score_matrix(scored, profile))
        if args.per_scenario:
            print(render_per_scenario(scored, profile))

    if args.multi_trv:
        _run_multi_trv_block(controllers, scenarios, args, profile)

    return 0


def _with_sensor_overrides(
    scenario: ScenarioConfig, overrides: dict[str, float]
) -> ScenarioConfig:
    """Return ``scenario`` with the given room sensor fields replaced."""
    if not overrides:
        return scenario
    sensor = scenario.sensor_params or REALISTIC_SENSOR_PARAMS
    return replace(scenario, sensor_params=replace(sensor, **overrides))


def _score_over_seeds(
    controllers: list[str],
    scenarios: list[str],
    args: argparse.Namespace,
    profile: UserProfile,
    oracle_by_label: dict[tuple[str, str], MetricValues],
    grouped: dict[str, list[ScenarioResult]],
) -> tuple[list[ScoredResult], list[ScoredResult]]:
    """Score every cell over ``args.seeds`` noise seeds.

    Seed 0 is the run already in ``grouped``; each further seed reruns the
    oracle and every controller on an independent noise realisation.

    Returns
    -------
    tuple of list of ScoredResult
        The per-cell mean scores, and per-cell spreads (max − min) carried
        in the same shape.
    """
    per_seed = [score_results(grouped, oracle_by_label, profile)]
    for seed in range(1, args.seeds):
        oracles: dict[tuple[str, str], MetricValues] = {}
        block: list[ScenarioResult] = []
        for s in scenarios:
            scen = replace(
                _with_sensor_overrides(ALL_SCENARIOS[s], args.sensor_overrides),
                noise_seed=seed,
            )
            oracles[("scenario default", s)] = run_scenario(
                IdealOracleAdapter(plant_params=scen.plant),
                scen,
                step_s=args.step_s,
                stabilisation_min=args.stabilisation_min,
            ).metrics
            block.extend(
                run_scenario(
                    _make_adapter(c, scen.plant),
                    scen,
                    step_s=args.step_s,
                    stabilisation_min=args.stabilisation_min,
                    learned_sensor=args.learned_sensor,
                )
                for c in controllers
            )
        per_seed.append(score_results({"scenario default": block}, oracles, profile))
    return average_over_seeds(per_seed)


def _run_multi_trv_block(
    controllers: list[str],
    scenario_names: list[str],
    args: argparse.Namespace,
    profile: UserProfile,
) -> None:
    """Score each controller against the three multi-TRV plant profiles.

    Scenarios that pin a non-standard plant are skipped — they carry
    plant-specific intent that the multi-TRV aggregation can't preserve.
    Each multi-TRV profile gets its own score matrix.
    """
    from .multi_trv_plant import (
        PROFILE_MULTI_ASYMMETRIC,
        PROFILE_MULTI_HETEROGENEOUS,
        PROFILE_MULTI_SYMMETRIC,
        MultiTrvPlantParams,
        MultiTrvPlantState,
    )
    from .multi_trv_runner import make_multi_trv_adapter, run_multi_trv_scenario

    multi_profiles: dict[str, MultiTrvPlantParams] = {
        "symmetric": PROFILE_MULTI_SYMMETRIC,
        "asymmetric": PROFILE_MULTI_ASYMMETRIC,
        "heterogeneous": PROFILE_MULTI_HETEROGENEOUS,
    }
    # Standard-plant scenarios only (others carry plant-specific intent),
    # and no restart scenarios: the multi-TRV driver does not fire the
    # restart protocol, so they would silently run without their defining
    # event.
    eligible = [
        s
        for s in scenario_names
        if ALL_SCENARIOS[s].plant is PROFILE_STANDARD
        and ALL_SCENARIOS[s].controller_restart_t_s is None
    ]
    if not eligible:
        print(
            "--multi-trv: no eligible scenarios in the selection "
            "(standard-plant, non-restart scenarios only)",
            file=sys.stderr,
        )
        return

    def _initial_for(
        plant: MultiTrvPlantParams, scen: ScenarioConfig
    ) -> MultiTrvPlantState:
        return MultiTrvPlantState(
            T_room=scen.initial.T_room, T_rads=[scen.initial.T_rad] * plant.n_trvs
        )

    for profile_name, plant_params in multi_profiles.items():
        label = f"multi-{profile_name}"
        oracle_cache: dict[tuple[str, str], MetricValues] = {}
        for s in eligible:
            scen = ALL_SCENARIOS[s]
            oracle_run = run_multi_trv_scenario(
                make_multi_trv_adapter("ideal_oracle", plant_params),
                scen,
                plant_params=plant_params,
                initial_state=_initial_for(plant_params, scen),
                step_s=args.step_s,
                stabilisation_min=args.stabilisation_min,
            )
            oracle_cache[(label, oracle_run.scenario)] = oracle_run.metrics

        section: list[ScenarioResult] = []
        for c in controllers:
            for s in eligible:
                scen = ALL_SCENARIOS[s]
                adapter = make_multi_trv_adapter(c, plant_params)
                section.append(
                    run_multi_trv_scenario(
                        adapter,
                        scen,
                        plant_params=plant_params,
                        initial_state=_initial_for(plant_params, scen),
                        step_s=args.step_s,
                        stabilisation_min=args.stabilisation_min,
                    )
                )

        scored = score_results({label: section}, oracle_cache, profile)
        print(f"\n[{label}]")
        print(render_score_matrix(scored, profile))


if __name__ == "__main__":
    sys.exit(main())
