"""Scenario library.

A scenario is an immutable description of one benchmark run: initial
conditions, plant parameters, input schedules, disturbance schedules
and acceptance criteria.

DESIGN.md §7.1 enumerates 16 scenarios; this module ships the
``setpoint``-, ``disturbance``- and ``cold-start`` families. The
multi-TRV variant (S14) is deferred until the simulator supports
multiple actuators.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from . import schedules
from .actuator import ActuatorParams, ActuatorProfile
from .plant import (
    PROFILE_BOILER_LIMITED,
    PROFILE_COOLING,
    PROFILE_STANDARD,
    PROFILE_UNDERFLOOR,
    PlantParams,
)
from .sensor import SensorParams


@dataclass(frozen=True)
class InitialConditions:
    """Plant state at scenario start.

    Attributes
    ----------
    T_room_C : float
        Room air temperature in °C.
    T_rad_C : float
        Radiator surface temperature in °C.
    """

    T_room_C: float
    T_rad_C: float


@dataclass(frozen=True)
class ScenarioConfig:
    """Immutable description of one benchmark scenario.

    Attributes
    ----------
    name : str
        Registry key and report label.
    description : str
        Human-readable one-line summary.
    duration_min : int
        Nominal scenario duration in minutes (scaled for slow plants).
    initial : InitialConditions
        Plant state at scenario start.
    plant : PlantParams
        Thermal plant the scenario runs against.
    setpoint_schedule : Callable[[float], float]
        ``t_s -> T_setpoint_C``.
    outdoor_schedule : Callable[[float], float]
        ``t_s -> T_outdoor_C``.
    transient_start_s : float
        Time from which transient metrics are evaluated.
    """

    name: str
    description: str
    duration_min: int
    initial: InitialConditions
    plant: PlantParams
    setpoint_schedule: Callable[[float], float]  # t_s → T_setpoint
    outdoor_schedule: Callable[[float], float]  # t_s → T_outdoor
    transient_start_s: float = 0.0

    # --- Optional disturbance and sensor schedules ---
    # If None, the runner treats the channel as inactive (no window event,
    # no solar gain, default sensor).
    window_open_schedule: Callable[[float], bool] | None = None
    solar_intensity_schedule: Callable[[float], float] | None = None
    # Independent solar signal *seen* by the controller. When ``None`` the
    # controller sees the same value as the plant; setting it to a
    # different schedule models a wrong weather forecast.
    controller_solar_schedule: Callable[[float], float] | None = None
    window_loss_K_per_min: float = 0.5  # heat lost per minute while window is open
    solar_max_K_per_min: float = 0.03  # plant-side heat gain at full solar (1.0)
    sensor_params: SensorParams | None = None
    actuator_params: ActuatorParams | None = None
    # Per-scenario override for the stabilisation phase. ``None`` defers
    # to the CLI default.
    stabilisation_min: float | None = None
    # If set, the runner calls ``adapter.reset()`` once when ``t`` first
    # reaches this value — models an HA restart that wipes integrator
    # state mid-flight.
    controller_restart_t_s: float | None = None


# --- Schedule helpers ---


# --- Scenarios ---


S01_SETPOINT_STEP_SMALL = ScenarioConfig(
    name="S01_setpoint_step_small",
    description="Setpoint step 20.0 → 21.0 °C after 30 min stabilization",
    duration_min=180,
    initial=InitialConditions(T_room_C=20.0, T_rad_C=20.0),
    plant=PROFILE_STANDARD,
    setpoint_schedule=schedules.step(30 * 60.0, 20.0, 21.0),
    outdoor_schedule=schedules.constant(5.0),
    transient_start_s=30 * 60.0,
)


S02_SETPOINT_STEP_LARGE = ScenarioConfig(
    name="S02_setpoint_step_large",
    description="Setpoint step 19.0 → 23.0 °C after 30 min stabilization",
    duration_min=300,
    initial=InitialConditions(T_room_C=19.0, T_rad_C=19.0),
    plant=PROFILE_STANDARD,
    setpoint_schedule=schedules.step(30 * 60.0, 19.0, 23.0),
    outdoor_schedule=schedules.constant(5.0),
    transient_start_s=30 * 60.0,
)


S03_FROST_TO_COMFORT = ScenarioConfig(
    name="S03_frost_to_comfort",
    description="Cold home: 12 → 21 °C with cold outdoor (-2 °C)",
    duration_min=720,  # 12 h: large jump on cold start
    initial=InitialConditions(T_room_C=12.0, T_rad_C=12.0),
    plant=PROFILE_STANDARD,
    setpoint_schedule=schedules.step(30 * 60.0, 12.0, 21.0),
    outdoor_schedule=schedules.constant(-2.0),
    transient_start_s=30 * 60.0,
    # Vacation-mode arrival: cold radiator, no prior controller state.
    stabilisation_min=10.0,
)


S04_SETPOINT_DROP = ScenarioConfig(
    name="S04_setpoint_drop",
    description="Downward setpoint step 22 → 19 °C — heat-off recovery",
    duration_min=300,
    initial=InitialConditions(T_room_C=22.0, T_rad_C=35.0),
    plant=PROFILE_STANDARD,
    setpoint_schedule=schedules.step(30 * 60.0, 22.0, 19.0),
    outdoor_schedule=schedules.constant(5.0),
    transient_start_s=30 * 60.0,
)


S06_SETPOINT_DURING_HEATING = ScenarioConfig(
    name="S06_setpoint_during_heating",
    description="Multi-step setpoint: 20 → 22 → 21 °C while controller is active",
    duration_min=360,
    initial=InitialConditions(T_room_C=20.0, T_rad_C=20.0),
    plant=PROFILE_STANDARD,
    setpoint_schedule=schedules.piecewise_step(
        [(60 * 60.0, 22.0), (180 * 60.0, 21.0)], initial=20.0
    ),
    outdoor_schedule=schedules.constant(5.0),
    transient_start_s=60 * 60.0,
)


S07_OUTDOOR_STEP_COLD = ScenarioConfig(
    name="S07_outdoor_step_cold",
    description="Outdoor temperature step 5 → -10 °C while holding setpoint 21",
    duration_min=300,
    initial=InitialConditions(T_room_C=21.0, T_rad_C=37.0),
    plant=PROFILE_STANDARD,
    setpoint_schedule=schedules.constant(21.0),
    outdoor_schedule=schedules.step(60 * 60.0, 5.0, -10.0),
    transient_start_s=60 * 60.0,
)


S08_OUTDOOR_RAMP_WARM = ScenarioConfig(
    name="S08_outdoor_ramp_warm",
    description="Outdoor warms slowly from 5 to 12 °C over 6 h, setpoint constant 21",
    duration_min=420,
    initial=InitialConditions(T_room_C=21.0, T_rad_C=37.0),
    plant=PROFILE_STANDARD,
    setpoint_schedule=schedules.constant(21.0),
    outdoor_schedule=schedules.ramp(60 * 60.0, 60 * 60.0 + 6 * 3600.0, 5.0, 12.0),
    transient_start_s=60 * 60.0,
)


S09_WINDOW_OPEN_SHORT = ScenarioConfig(
    name="S09_window_open_short",
    description="5-minute window-open event at t=60 min, setpoint 21",
    duration_min=240,
    initial=InitialConditions(T_room_C=21.0, T_rad_C=37.0),
    plant=PROFILE_STANDARD,
    setpoint_schedule=schedules.constant(21.0),
    outdoor_schedule=schedules.constant(5.0),
    window_open_schedule=schedules.pulse_bool(60 * 60.0, 65 * 60.0),
    transient_start_s=60 * 60.0,
)


S10_WINDOW_OPEN_LONG = ScenarioConfig(
    name="S10_window_open_long",
    description="20-minute window-open event at t=60 min, setpoint 21",
    duration_min=360,
    initial=InitialConditions(T_room_C=21.0, T_rad_C=37.0),
    plant=PROFILE_STANDARD,
    setpoint_schedule=schedules.constant(21.0),
    outdoor_schedule=schedules.constant(5.0),
    window_open_schedule=schedules.pulse_bool(60 * 60.0, 80 * 60.0),
    transient_start_s=60 * 60.0,
)


S11_SOLAR_GAIN_MORNING = ScenarioConfig(
    name="S11_solar_gain_morning",
    description="Solar trapezoid (rise 60 min, plateau 60 min, fall 60 min) at full intensity",
    duration_min=300,
    initial=InitialConditions(T_room_C=21.0, T_rad_C=37.0),
    plant=PROFILE_STANDARD,
    setpoint_schedule=schedules.constant(21.0),
    outdoor_schedule=schedules.constant(8.0),
    solar_intensity_schedule=schedules.solar_trapezoid(
        60 * 60.0, 120 * 60.0, 180 * 60.0, 240 * 60.0, peak=1.0
    ),
    transient_start_s=60 * 60.0,
)


S12_SENSOR_DROPOUT = ScenarioConfig(
    name="S12_sensor_dropout",
    description="10-minute sensor dropout at t=60-70 min, setpoint 21",
    duration_min=240,
    initial=InitialConditions(T_room_C=21.0, T_rad_C=37.0),
    plant=PROFILE_STANDARD,
    setpoint_schedule=schedules.constant(21.0),
    outdoor_schedule=schedules.constant(5.0),
    transient_start_s=60 * 60.0,
    sensor_params=SensorParams(
        sample_interval_s=60.0, dropout_from_t_s=60 * 60.0, dropout_until_t_s=70 * 60.0
    ),
)


S13_COLD_START = ScenarioConfig(
    name="S13_cold_start",
    description="Cold start: T_room=15, no prior controller state, setpoint 20",
    duration_min=360,
    initial=InitialConditions(T_room_C=15.0, T_rad_C=15.0),
    plant=PROFILE_STANDARD,
    setpoint_schedule=schedules.constant(20.0),
    outdoor_schedule=schedules.constant(5.0),
    transient_start_s=0.0,
    # Forced cold-start: no pre-warm via oracle.
    stabilisation_min=0.0,
)


S05_SLOW_RADIATOR = ScenarioConfig(
    name="S05_slow_radiator",
    description="Setpoint step 20 → 21 °C on an underfloor heating plant",
    duration_min=480,
    initial=InitialConditions(T_room_C=20.0, T_rad_C=20.0),
    plant=PROFILE_UNDERFLOOR,
    setpoint_schedule=schedules.step(30 * 60.0, 20.0, 21.0),
    outdoor_schedule=schedules.constant(5.0),
    transient_start_s=30 * 60.0,
)


S14_NIGHTLY_SETBACK = ScenarioConfig(
    name="S14_nightly_setback",
    description="Setback 18 °C → wake-up 21 °C after 30 min → setback 18 °C at 10 h",
    duration_min=12 * 60,
    initial=InitialConditions(T_room_C=18.0, T_rad_C=18.0),
    plant=PROFILE_STANDARD,
    setpoint_schedule=schedules.piecewise_step(
        [(30 * 60.0, 21.0), (10 * 3600.0, 18.0)], initial=18.0
    ),
    outdoor_schedule=schedules.constant(-2.0),
    transient_start_s=30 * 60.0,
)


S15_DAILY_CYCLE = ScenarioConfig(
    name="S15_daily_cycle",
    description="24 h day: morning warmup, daytime away, evening, night setback",
    duration_min=24 * 60,
    initial=InitialConditions(T_room_C=19.0, T_rad_C=22.0),
    plant=PROFILE_STANDARD,
    setpoint_schedule=schedules.piecewise_step(
        [
            (2 * 3600.0, 19.0),  # 08:00 daytime away
            (11 * 3600.0, 21.0),  # 17:00 evening occupied
            (16 * 3600.0, 18.0),  # 22:00 night setback
        ],
        initial=21.0,  # 06:00 morning, already on the wakeup setpoint
    ),
    outdoor_schedule=schedules.sinus_diurnal(
        min_value=1.0, max_value=8.0, phase_min_h=0.0
    ),
    transient_start_s=0.0,
)


S17_SENSOR_BIAS = ScenarioConfig(
    name="S17_sensor_bias",
    description="Steady-state with constant sensor bias +0.5 K (room ends 0.5 K below SP)",
    duration_min=8 * 60,
    initial=InitialConditions(T_room_C=21.0, T_rad_C=33.0),
    plant=PROFILE_STANDARD,
    setpoint_schedule=schedules.constant(21.0),
    outdoor_schedule=schedules.constant(5.0),
    sensor_params=SensorParams(sample_interval_s=60.0, bias_K=0.5),
    transient_start_s=30 * 60.0,
)


S18_SENSOR_DRIFT = ScenarioConfig(
    name="S18_sensor_drift",
    description="Slow sensor drift +0.05 K/h over 12 h (alters perceived ss_err)",
    duration_min=12 * 60,
    initial=InitialConditions(T_room_C=21.0, T_rad_C=33.0),
    plant=PROFILE_STANDARD,
    setpoint_schedule=schedules.constant(21.0),
    outdoor_schedule=schedules.constant(5.0),
    sensor_params=SensorParams(sample_interval_s=60.0, drift_K_per_h=0.05),
    transient_start_s=30 * 60.0,
)


S19_VALVE_STICTION = ScenarioConfig(
    name="S19_valve_stiction",
    description="Steady-state under 5 % valve stiction (stick-slip hysteresis)",
    duration_min=6 * 60,
    initial=InitialConditions(T_room_C=21.0, T_rad_C=33.0),
    plant=PROFILE_STANDARD,
    setpoint_schedule=schedules.constant(21.0),
    outdoor_schedule=schedules.constant(5.0),
    actuator_params=ActuatorParams(
        profile=ActuatorProfile.EQUAL_PERCENTAGE,
        equal_percentage_exponent=3.0,
        hysteresis_pct=5.0,
    ),
    transient_start_s=30 * 60.0,
)


S20_VALVE_DEADBAND = ScenarioConfig(
    name="S20_valve_deadband",
    description="Small setpoint step (20 → 21) on a valve with 3 % deadband",
    duration_min=180,
    initial=InitialConditions(T_room_C=20.0, T_rad_C=20.0),
    plant=PROFILE_STANDARD,
    setpoint_schedule=schedules.step(30 * 60.0, 20.0, 21.0),
    outdoor_schedule=schedules.constant(5.0),
    actuator_params=ActuatorParams(
        profile=ActuatorProfile.EQUAL_PERCENTAGE,
        equal_percentage_exponent=3.0,
        deadband_pct=3.0,
    ),
    transient_start_s=30 * 60.0,
)


S23_BOILER_LIMITED = ScenarioConfig(
    name="S23_boiler_limited",
    description="Cold start (15 → 21 °C) on a 42 °C heat-pump supply at +8 °C outdoor",
    duration_min=720,  # 12 h — heat pump warmup is slower
    initial=InitialConditions(T_room_C=15.0, T_rad_C=15.0),
    plant=PROFILE_BOILER_LIMITED,
    setpoint_schedule=schedules.step(30 * 60.0, 15.0, 21.0),
    outdoor_schedule=schedules.constant(8.0),
    transient_start_s=30 * 60.0,
    stabilisation_min=10.0,
)


S21_STOCHASTIC_WINDOWS = ScenarioConfig(
    name="S21_stochastic_windows",
    description="12 h with 3 randomised window-open events (Annex-79-style)",
    duration_min=12 * 60,
    initial=InitialConditions(T_room_C=21.0, T_rad_C=33.0),
    plant=PROFILE_STANDARD,
    setpoint_schedule=schedules.constant(21.0),
    outdoor_schedule=schedules.constant(0.0),
    window_open_schedule=schedules.stochastic_windows(
        seed=42,
        count=3,
        duration_s=12 * 3600.0,
        min_duration_s=3 * 60.0,
        max_duration_s=8 * 60.0,
    ),
    window_loss_K_per_min=0.3,
    transient_start_s=30 * 60.0,
)


S24_CONTROLLER_RESTART = ScenarioConfig(
    name="S24_controller_restart",
    description="Steady-state, then controller reset() at t=2h (HA restart sim)",
    duration_min=4 * 60,
    initial=InitialConditions(T_room_C=21.0, T_rad_C=35.0),
    plant=PROFILE_STANDARD,
    setpoint_schedule=schedules.constant(21.0),
    outdoor_schedule=schedules.constant(0.0),
    transient_start_s=120 * 60.0,
    controller_restart_t_s=120 * 60.0,
)


S25_DEMAND_RESPONSE = ScenarioConfig(
    name="S25_demand_response",
    description="Grid demand-response: pre-heat +2 K, then setback -1 K, back to normal",
    duration_min=8 * 60,
    initial=InitialConditions(T_room_C=20.0, T_rad_C=30.0),
    plant=PROFILE_STANDARD,
    setpoint_schedule=schedules.piecewise_step(
        [
            (2 * 3600.0, 22.0),  # DR pre-heat
            (4 * 3600.0, 19.0),  # DR peak setback
            (6 * 3600.0, 20.0),  # back to normal
        ],
        initial=20.0,
    ),
    outdoor_schedule=schedules.constant(0.0),
    transient_start_s=2 * 3600.0,
)


S16_VACATION = ScenarioConfig(
    name="S16_vacation",
    description="6 d vacation: 20 °C → 8 °C frost-protect (5 d) → 20 °C return",
    duration_min=6 * 24 * 60,  # 144 h
    initial=InitialConditions(T_room_C=20.0, T_rad_C=25.0),
    plant=PROFILE_STANDARD,
    setpoint_schedule=schedules.piecewise_step(
        [
            (2 * 3600.0, 8.0),  # vacation setback after 2 h
            (5 * 24 * 3600.0, 20.0),  # return after 5 d
        ],
        initial=20.0,
    ),
    outdoor_schedule=schedules.constant(-5.0),
    transient_start_s=5 * 24 * 3600.0,  # measure the return transient
)


def _build_synthetic_weather_scenario(
    name: str,
    climate: ClimateParams,
    seed: int,
    start_day_of_year: int = 14,
    duration_h: int = 168,
    setpoint_C: float = 21.0,
) -> ScenarioConfig:
    """Build a constant-setpoint, weather-driven scenario.

    The outdoor and solar schedules are generated by
    :mod:`weather.synthetic` from the supplied ``climate`` preset plus a
    seed — fully reproducible, no external data.

    Parameters
    ----------
    name : str
        Scenario registry key.
    climate : ClimateParams
        Climate preset for the synthetic weather generator.
    seed : int
        RNG seed for the weather generator.
    start_day_of_year : int
        Day of year at which the weather slice starts.
    duration_h : int
        Scenario length in hours.
    setpoint_C : float
        Constant setpoint in °C.

    Returns
    -------
    ScenarioConfig
        Fully-populated weather-driven scenario.
    """
    from .weather.synthetic import make_schedules

    outdoor, solar = make_schedules(
        climate, start_day_of_year=start_day_of_year, duration_h=duration_h, seed=seed
    )
    return ScenarioConfig(
        name=name,
        description=(
            f"{duration_h // 24} d {climate.name} winter slice "
            f"(seed={seed}), setpoint {setpoint_C} °C"
        ),
        duration_min=duration_h * 60,
        initial=InitialConditions(T_room_C=setpoint_C, T_rad_C=setpoint_C + 8.0),
        plant=PROFILE_STANDARD,
        setpoint_schedule=schedules.constant(setpoint_C),
        outdoor_schedule=outdoor,
        solar_intensity_schedule=solar,
        transient_start_s=0.0,
        # Permanent outdoor variability — settling is not a meaningful
        # metric and overshoot tolerates the diurnal swing.
    )


# Lazy import so the dataclass is reachable from the type annotation.
from .weather.synthetic import CHICAGO_LIKE, DENVER_LIKE, ClimateParams  # noqa: E402

S37_WINTER_WEEK_HUMID_CONT = _build_synthetic_weather_scenario(
    name="S37_winter_week_humid_continental",
    climate=CHICAGO_LIKE,
    seed=37,
    start_day_of_year=14,  # mid-January
)


S38_WINTER_WEEK_SEMI_ARID = _build_synthetic_weather_scenario(
    name="S38_winter_week_semi_arid", climate=DENVER_LIKE, seed=38, start_day_of_year=14
)


S28_INDIRECT_TRV_TADO = ScenarioConfig(
    name="S28_indirect_trv_tado",
    description="SP step 19 → 21 on a Tado-style indirect TRV (0.5 K steps, internal P-loop)",
    duration_min=4 * 60,
    initial=InitialConditions(T_room_C=19.0, T_rad_C=19.0),
    plant=PROFILE_STANDARD,
    setpoint_schedule=schedules.step(30 * 60.0, 19.0, 21.0),
    outdoor_schedule=schedules.constant(5.0),
    transient_start_s=30 * 60.0,
)


S30_FROST_DRIFT_RECOVERY = ScenarioConfig(
    name="S30_frost_drift_recovery",
    description="7 d frost-protection at 12 °C, then 8 h recovery to 21 °C — only recovery is scored",
    duration_min=7 * 24 * 60 + 8 * 60,
    initial=InitialConditions(T_room_C=12.0, T_rad_C=15.0),
    plant=PROFILE_STANDARD,
    setpoint_schedule=schedules.piecewise_step([(7 * 24 * 3600.0, 21.0)], initial=12.0),
    outdoor_schedule=schedules.constant(-5.0),
    transient_start_s=7 * 24 * 3600.0,
    stabilisation_min=0.0,
)


S32_FORECAST_MISMATCH_SOLAR = ScenarioConfig(
    name="S32_forecast_mismatch_solar",
    description="Controller sees solar=1.0 (wrong forecast) while plant gets solar=0.0",
    duration_min=8 * 60,
    initial=InitialConditions(T_room_C=21.0, T_rad_C=33.0),
    plant=PROFILE_STANDARD,
    setpoint_schedule=schedules.constant(21.0),
    outdoor_schedule=schedules.constant(-5.0),
    solar_intensity_schedule=schedules.constant(0.0),  # plant truth: no sun
    controller_solar_schedule=schedules.constant(1.0),  # controller lie: full sun
    transient_start_s=30 * 60.0,
)


S29_HEATPUMP_STEADY_STATE = ScenarioConfig(
    name="S29_heatpump_steady_state",
    description="24 h on PROFILE_BOILER_LIMITED with diurnal outdoor; evaluate valve sweet-spot residency",
    duration_min=24 * 60,
    initial=InitialConditions(T_room_C=21.0, T_rad_C=33.0),
    plant=PROFILE_BOILER_LIMITED,
    setpoint_schedule=schedules.constant(21.0),
    outdoor_schedule=schedules.sinus_diurnal(
        min_value=4.0, max_value=12.0, phase_min_h=6.0
    ),
    transient_start_s=0.0,
)


S33_UFH_ASYMMETRIC = ScenarioConfig(
    name="S33_ufh_asymmetric",
    description="Underfloor SP step 20 → 21; asymmetric overshoot/undershoot accounting",
    duration_min=16 * 60,
    initial=InitialConditions(T_room_C=20.0, T_rad_C=28.0),
    plant=PROFILE_UNDERFLOOR,
    setpoint_schedule=schedules.step(30 * 60.0, 20.0, 21.0),
    outdoor_schedule=schedules.constant(10.0),
    transient_start_s=30 * 60.0,
)


S31_BOILER_CYCLE_STRESS = ScenarioConfig(
    name="S31_boiler_cycle_stress",
    description="Multi-step SP wobble on a 20 % deadband valve — boiler cycle stress",
    duration_min=6 * 60,
    initial=InitialConditions(T_room_C=20.5, T_rad_C=30.0),
    plant=PROFILE_STANDARD,
    setpoint_schedule=schedules.piecewise_step(
        [(60 * 60.0, 21.0), (120 * 60.0, 20.8), (180 * 60.0, 21.2), (240 * 60.0, 21.0)],
        initial=21.0,
    ),
    outdoor_schedule=schedules.constant(5.0),
    actuator_params=ActuatorParams(
        profile=ActuatorProfile.EQUAL_PERCENTAGE,
        equal_percentage_exponent=3.0,
        deadband_pct=20.0,
        hysteresis_pct=3.0,
    ),
    transient_start_s=0.0,
)


S35_SAMPLE_JITTER = ScenarioConfig(
    name="S35_sample_jitter",
    description="SP step 20 → 21 with sensor sample interval jittering ±45 s around 120 s",
    duration_min=4 * 60,
    initial=InitialConditions(T_room_C=20.0, T_rad_C=20.0),
    plant=PROFILE_STANDARD,
    setpoint_schedule=schedules.step(30 * 60.0, 20.0, 21.0),
    outdoor_schedule=schedules.constant(5.0),
    sensor_params=SensorParams(sample_interval_s=120.0, jitter_std_s=45.0),
    transient_start_s=30 * 60.0,
)


S36_USER_OVERRIDE = ScenarioConfig(
    name="S36_user_override",
    description="SP whipped via API every 8 minutes — controller must not drop updates",
    duration_min=2 * 60,
    initial=InitialConditions(T_room_C=20.0, T_rad_C=30.0),
    plant=PROFILE_STANDARD,
    setpoint_schedule=schedules.piecewise_step(
        [
            (8 * 60.0, 22.0),
            (16 * 60.0, 19.5),
            (24 * 60.0, 21.0),
            (32 * 60.0, 20.0),
            (40 * 60.0, 22.5),
            (48 * 60.0, 19.0),
            (56 * 60.0, 21.0),
        ],
        initial=20.0,
    ),
    outdoor_schedule=schedules.constant(5.0),
    transient_start_s=0.0,
)


S27_PIPE_FILL_AFTER_IDLE = ScenarioConfig(
    name="S27_pipe_fill_after_idle",
    description="Long idle then SP step 20 → 22 with 120 s pipe-transport delay",
    duration_min=4 * 60,
    initial=InitialConditions(T_room_C=20.0, T_rad_C=20.0),
    plant=PlantParams(
        tau_room_min=480.0,
        tau_rad_min=15.0,
        gain_heater=2.0,
        coupling_rad_room=1.0,
        T_water_C=65.0,
        valve_command_delay_s=120.0,
    ),
    setpoint_schedule=schedules.step(120 * 60.0, 20.0, 22.0),
    outdoor_schedule=schedules.constant(2.0),
    transient_start_s=120 * 60.0,
    stabilisation_min=0.0,
)


S26_COOLING_MODE = ScenarioConfig(
    name="S26_cooling_mode",
    description="Hot day (outdoor 28 °C) on a reverse-acting chilled-water plant",
    duration_min=8 * 60,
    initial=InitialConditions(T_room_C=22.0, T_rad_C=22.0),
    plant=PROFILE_COOLING,
    setpoint_schedule=schedules.constant(22.0),
    outdoor_schedule=schedules.constant(28.0),
    transient_start_s=30 * 60.0,
)


S22_DIURNAL_OUTDOOR = ScenarioConfig(
    name="S22_diurnal_outdoor",
    description="Constant 21 °C setpoint under a 24 h diurnal outdoor cycle (-5..+3 °C)",
    duration_min=24 * 60,
    initial=InitialConditions(T_room_C=21.0, T_rad_C=31.0),
    plant=PROFILE_STANDARD,
    setpoint_schedule=schedules.constant(21.0),
    outdoor_schedule=schedules.sinus_diurnal(
        min_value=-5.0, max_value=3.0, phase_min_h=0.0
    ),
    transient_start_s=0.0,
)


ALL_SCENARIOS: dict[str, ScenarioConfig] = {
    S01_SETPOINT_STEP_SMALL.name: S01_SETPOINT_STEP_SMALL,
    S02_SETPOINT_STEP_LARGE.name: S02_SETPOINT_STEP_LARGE,
    S03_FROST_TO_COMFORT.name: S03_FROST_TO_COMFORT,
    S04_SETPOINT_DROP.name: S04_SETPOINT_DROP,
    S05_SLOW_RADIATOR.name: S05_SLOW_RADIATOR,
    S06_SETPOINT_DURING_HEATING.name: S06_SETPOINT_DURING_HEATING,
    S07_OUTDOOR_STEP_COLD.name: S07_OUTDOOR_STEP_COLD,
    S08_OUTDOOR_RAMP_WARM.name: S08_OUTDOOR_RAMP_WARM,
    S09_WINDOW_OPEN_SHORT.name: S09_WINDOW_OPEN_SHORT,
    S10_WINDOW_OPEN_LONG.name: S10_WINDOW_OPEN_LONG,
    S11_SOLAR_GAIN_MORNING.name: S11_SOLAR_GAIN_MORNING,
    S12_SENSOR_DROPOUT.name: S12_SENSOR_DROPOUT,
    S13_COLD_START.name: S13_COLD_START,
    S14_NIGHTLY_SETBACK.name: S14_NIGHTLY_SETBACK,
    S15_DAILY_CYCLE.name: S15_DAILY_CYCLE,
    S16_VACATION.name: S16_VACATION,
    S17_SENSOR_BIAS.name: S17_SENSOR_BIAS,
    S18_SENSOR_DRIFT.name: S18_SENSOR_DRIFT,
    S19_VALVE_STICTION.name: S19_VALVE_STICTION,
    S20_VALVE_DEADBAND.name: S20_VALVE_DEADBAND,
    S21_STOCHASTIC_WINDOWS.name: S21_STOCHASTIC_WINDOWS,
    S22_DIURNAL_OUTDOOR.name: S22_DIURNAL_OUTDOOR,
    S23_BOILER_LIMITED.name: S23_BOILER_LIMITED,
    S24_CONTROLLER_RESTART.name: S24_CONTROLLER_RESTART,
    S25_DEMAND_RESPONSE.name: S25_DEMAND_RESPONSE,
    S26_COOLING_MODE.name: S26_COOLING_MODE,
    S27_PIPE_FILL_AFTER_IDLE.name: S27_PIPE_FILL_AFTER_IDLE,
    S31_BOILER_CYCLE_STRESS.name: S31_BOILER_CYCLE_STRESS,
    S35_SAMPLE_JITTER.name: S35_SAMPLE_JITTER,
    S36_USER_OVERRIDE.name: S36_USER_OVERRIDE,
    S33_UFH_ASYMMETRIC.name: S33_UFH_ASYMMETRIC,
    S29_HEATPUMP_STEADY_STATE.name: S29_HEATPUMP_STEADY_STATE,
    S32_FORECAST_MISMATCH_SOLAR.name: S32_FORECAST_MISMATCH_SOLAR,
    S30_FROST_DRIFT_RECOVERY.name: S30_FROST_DRIFT_RECOVERY,
    S28_INDIRECT_TRV_TADO.name: S28_INDIRECT_TRV_TADO,
    S37_WINTER_WEEK_HUMID_CONT.name: S37_WINTER_WEEK_HUMID_CONT,
    S38_WINTER_WEEK_SEMI_ARID.name: S38_WINTER_WEEK_SEMI_ARID,
}
