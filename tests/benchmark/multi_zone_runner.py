"""Sweep of the temperature-based valve split over multi-zone rooms.

Better Thermostat can split a group's valve command by each TRV's own reading.
Whether that helps depends on what the readings say about the room: how far the
zones drift apart, how much of a reading is the TRV's own radiator, how wrong a
TRV reads, how often it reports, and where the room sensor hangs. None of these
is known for a real room, so this runner draws rooms across their plausible
ranges and compares the even command with the production split in each.

Zone 0 is the zone that loses the larger share of the room's heat.

Run with ``uv run python -m tests.benchmark.multi_zone_runner``.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from collections.abc import Callable, Iterable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, replace
from itertools import product
import json
import math
from pathlib import Path
import statistics

import numpy as np

from custom_components.better_thermostat.utils.calibration.mpc import (
    distribute_valve_percent,
)

from . import schedules
from .actuator import Actuator, ActuatorParams
from .multi_zone_plant import MultiZonePlant, MultiZonePlantParams, settled_state_at
from .runner import _drive_adapter, _make_adapter
from .scenarios import (
    S07_OUTDOOR_STEP_COLD,
    S14_NIGHTLY_SETBACK,
    S15_DAILY_CYCLE,
    ScenarioConfig,
)
from .sensor import trv_sensor_reading

# Long enough for the zones and the controllers' learned models to settle
# before anything is scored.
WARMUP_S = 72 * 3600.0
STEP_S = 30.0
# A setpoint change counts as a transition for this long afterwards.
TRANSITION_S = 6 * 3600.0
# Deviations a zone may show from the setpoint before it counts as discomfort.
COMFORT_BANDS_K = (0.2, 0.5, 1.0)
PRIMARY_BAND_INDEX = 1

WINTER_HOLD = replace(
    S07_OUTDOOR_STEP_COLD,
    name="winter_hold",
    description="Two days on one setpoint under a daily outdoor swing",
    duration_minutes=48 * 60,
    setpoint_schedule=schedules.constant(21.0),
    outdoor_schedule=schedules.sinus_diurnal(
        min_value=-2.0, max_value=6.0, phase_min_h=0.0
    ),
    transient_start_s=0.0,
)
SCENARIOS: dict[str, ScenarioConfig] = {
    scenario.name: scenario
    for scenario in (
        WINTER_HOLD,
        S07_OUTDOOR_STEP_COLD,
        S14_NIGHTLY_SETBACK,
        S15_DAILY_CYCLE,
    )
}
CONTROLLERS = ("mpc", "mpc_v2")

# Seconds a TRV may stay silent after its reading changed. Reporting differs
# by model and firmware, so the sweep spans it.
TRV_REPORT_INTERVALS_S = (30.0, 300.0, 900.0)
# A worn or sticky valve: small commands do not open it, small moves do not
# move it.
STICKY_VALVE = ActuatorParams(deadband_percent=5.0, hysteresis_percent=3.0)

# Even-command spread between the zones the results are grouped by, in K.
SPREAD_BINS_K = (
    (0.0, 0.2),
    (0.2, 0.4),
    (0.4, 0.6),
    (0.6, 0.8),
    (0.8, 1.0),
    (1.0, 1.5),
    (1.5, 2.5),
    (2.5, math.inf),
)
# A cold-zone TRV counts as reading correctly within this error, in K.
ACCURATE_BIAS_K = 0.25
_BOOTSTRAP_SAMPLES = 2000


@dataclass(frozen=True)
class ZoneCase:
    """One room of the sweep.

    Attributes
    ----------
    cold_loss_share : float
        Share of the room's loss the cold zone carries.
    air_exchange : float
        Conductance between the zones, relative to the room's loss.
    cold_rad_fraction : float
        Share of its radiator's excess the cold zone's TRV picks up.
    warm_rad_fraction : float
        Share of its radiator's excess the warm zone's TRV picks up.
    sensor_cold_weight : float
        Share of the cold zone in the room sensor's reading.
    cold_trv_bias_K : float
        What the cold zone's TRV reads too warm; negative reads too cold.
    cold_radiator_scale : float
        Size of the cold zone's radiator against the warm zone's.
    sticky_valves : bool
        Whether the valves carry :data:`STICKY_VALVE`.
    trv_report_interval_s : float
        Seconds a TRV stays silent after its reading changed.
    warm_trv_bias_K : float
        What the warm zone's TRV reads too warm. The split sees only the
        difference of the readings, so the sweep varies the cold TRV alone.
    """

    cold_loss_share: float
    air_exchange: float
    cold_rad_fraction: float
    warm_rad_fraction: float
    sensor_cold_weight: float
    cold_trv_bias_K: float
    cold_radiator_scale: float = 1.0
    sticky_valves: bool = False
    trv_report_interval_s: float = STEP_S
    warm_trv_bias_K: float = 0.0

    @property
    def plant(self) -> MultiZonePlantParams:
        """The two-zone plant of this room."""
        scale = self.cold_radiator_scale
        return MultiZonePlantParams(
            gain_heaters=(3.0 * scale, 3.0),
            coupling_rad_zone=(1.5 * scale, 1.5),
            loss_shares=(self.cold_loss_share, 1.0 - self.cold_loss_share),
            air_exchange=self.air_exchange,
        )

    @property
    def sensor_weights(self) -> tuple[float, float]:
        """Share of each zone in the room sensor's reading."""
        return (self.sensor_cold_weight, 1.0 - self.sensor_cold_weight)


def sample_rooms(count: int, seed: int = 0) -> list[ZoneCase]:
    """Draw rooms by Latin hypercube sampling.

    Every parameter's range is cut into ``count`` strata and each stratum is
    used once, so a small sample still covers each range evenly.

    Parameters
    ----------
    count : int
        Number of rooms.
    seed : int
        Seed of the draw.

    Returns
    -------
    list[ZoneCase]
        The rooms, the same for the same arguments.
    """
    rng = np.random.default_rng(seed)
    strata = [(rng.permutation(count) + rng.random(count)) / count for _ in range(9)]

    def _within(index: int, room: int, low: float, high: float) -> float:
        return float(low + strata[index][room] * (high - low))

    rooms: list[ZoneCase] = []
    for room in range(count):
        intervals = TRV_REPORT_INTERVALS_S
        rooms.append(
            ZoneCase(
                cold_loss_share=_within(0, room, 0.5, 0.85),
                air_exchange=float(10.0 ** _within(1, room, 0.0, math.log10(64.0))),
                cold_rad_fraction=_within(2, room, 0.0, 0.3),
                warm_rad_fraction=_within(3, room, 0.0, 0.3),
                sensor_cold_weight=_within(4, room, 0.0, 1.0),
                cold_trv_bias_K=_within(5, room, -1.0, 1.0),
                cold_radiator_scale=_within(6, room, 0.6, 1.4),
                sticky_valves=bool(strata[7][room] >= 0.5),
                trv_report_interval_s=intervals[int(strata[8][room] * len(intervals))],
            )
        )
    return rooms


@dataclass(frozen=True)
class ZoneMetrics:
    """What one run did to the zones over the scored phase.

    Attributes
    ----------
    spread_K : float
        Mean absolute temperature difference between the zones.
    steady_excess_K : tuple[float, ...]
        Mean excess beyond each of :data:`COMFORT_BANDS_K` in the zone that
        suffered most, away from setpoint changes.
    transition_excess_K : tuple[float, ...]
        The same within :data:`TRANSITION_S` after a setpoint change.
    energy_K_h : float
        Heat the radiators delivered to the room.
    valve_travel_percent : float
        Summed movement of all valves.
    """

    spread_K: float
    steady_excess_K: tuple[float, ...]
    transition_excess_K: tuple[float, ...]
    energy_K_h: float
    valve_travel_percent: float


class _MultiZoneFacade:
    """Wrap :class:`MultiZonePlant` for the shared ``_drive_adapter`` loop.

    Records every zone at the moment the loop records the room, so the samples
    line up with the time series the loop returns.
    """

    def __init__(self, plant: MultiZonePlant, case: ZoneCase, split: bool) -> None:
        self._plant = plant
        self._case = case
        self._split = split
        valve = STICKY_VALVE if case.sticky_valves else ActuatorParams()
        self._actuators = [Actuator(valve) for _ in range(plant.params.n_zones)]
        self._t_s = 0.0
        self._reports: list[float | None] = [None] * plant.params.n_zones
        self._reported_at_s = [0.0] * plant.params.n_zones
        self.zone_samples: list[list[float]] = []
        self.valve_samples: list[list[float]] = []
        self.heat_samples: list[float] = []

    @property
    def T_room(self) -> float:
        return sum(
            weight * zone
            for weight, zone in zip(
                self._case.sensor_weights, self._plant.state.T_zones, strict=True
            )
        )

    @property
    def T_rad(self) -> float:
        rads = self._plant.state.T_rads
        return sum(rads) / len(rads)

    @property
    def heat_to_room_K_per_min(self) -> float:
        return self._plant.heat_to_room_K_per_min

    def trv_readings(self) -> dict[str, float]:
        """Return what each TRV last reported, keyed by zone index.

        A TRV reports a changed reading once its report interval has passed.
        """
        case = self._case
        state = self._plant.state
        fractions = (case.cold_rad_fraction, case.warm_rad_fraction)
        biases = (case.cold_trv_bias_K, case.warm_trv_bias_K)
        readings: dict[str, float] = {}
        for i, (zone, rad) in enumerate(zip(state.T_zones, state.T_rads, strict=True)):
            fresh = trv_sensor_reading(zone, rad, fractions[i])
            report = self._reports[i]
            due = self._t_s - self._reported_at_s[i] >= case.trv_report_interval_s
            if report is None or (due and fresh != report):
                report = fresh
                self._reports[i] = fresh
                self._reported_at_s[i] = self._t_s
            readings[str(i)] = report + biases[i]
        return readings

    def valve_percents(self, valve_percent: float) -> list[float]:
        """Return each TRV's command for the group's ``valve_percent``."""
        n_zones = self._plant.params.n_zones
        if not self._split:
            return [valve_percent] * n_zones
        shares = distribute_valve_percent(valve_percent, self.trv_readings())
        # BT writes a direct-valve command as a whole percent.
        return [float(round(shares[str(i)])) for i in range(n_zones)]

    def apply(
        self,
        dt_s: float,
        valve_percent: float,
        T_outdoor: float,
        Q_K_per_min: float,
        flow_temperature: float | None,
        heat_delivered: bool,
    ) -> None:
        openings = [
            actuator.apply(percent)
            for actuator, percent in zip(
                self._actuators, self.valve_percents(valve_percent), strict=True
            )
        ]
        self.zone_samples.append(list(self._plant.state.T_zones))
        self.valve_samples.append([opening * 100.0 for opening in openings])
        self.heat_samples.append(self._plant.heat_to_room_K_per_min)
        self._plant.step(
            dt_s,
            openings,
            T_outdoor,
            Q_K_per_min=Q_K_per_min,
            flow_temperature=flow_temperature,
            heat_delivered=heat_delivered,
        )
        self._t_s += dt_s


def with_warmup(scenario: ScenarioConfig, warmup_s: float) -> ScenarioConfig:
    """Hold a scenario's first conditions for ``warmup_s`` before it starts.

    Parameters
    ----------
    scenario : ScenarioConfig
        The scenario to extend. Its setpoint and outdoor schedules are shifted;
        it must carry no other schedule.
    warmup_s : float
        Seconds to hold the conditions at ``t = 0``.

    Returns
    -------
    ScenarioConfig
        The scenario with the warm-up in front, scored after it.
    """
    setpoint = scenario.setpoint_schedule
    outdoor = scenario.outdoor_schedule
    return replace(
        scenario,
        duration_minutes=scenario.duration_minutes + round(warmup_s / 60.0),
        setpoint_schedule=lambda t: setpoint(max(0.0, t - warmup_s)),
        outdoor_schedule=lambda t: outdoor(max(0.0, t - warmup_s)),
        transient_start_s=scenario.transient_start_s + warmup_s,
    )


def zone_metrics(
    t_s: list[float],
    setpoints: list[float],
    facade: _MultiZoneFacade,
    scored_from_s: float,
) -> ZoneMetrics:
    """Compute what a run did to the zones from ``scored_from_s`` on.

    Parameters
    ----------
    t_s : list[float]
        Sample times in seconds.
    setpoints : list[float]
        The setpoint at each sample in °C.
    facade : _MultiZoneFacade
        The facade that recorded the run.
    scored_from_s : float
        First second that counts.

    Returns
    -------
    ZoneMetrics
        The run's zone metrics.
    """
    n_zones = len(facade.zone_samples[0])
    n_bands = len(COMFORT_BANDS_K)
    excess = {
        phase: [[0.0] * n_zones for _ in range(n_bands)]
        for phase in ("steady", "transition")
    }
    samples = {"steady": 0, "transition": 0}
    spreads: list[float] = []
    energy = 0.0
    travel = 0.0
    previous: list[float] | None = None
    changed_at_s = -math.inf
    for index, t in enumerate(t_s):
        if index > 0 and setpoints[index] != setpoints[index - 1]:
            changed_at_s = t
        if t < scored_from_s:
            continue
        zones = facade.zone_samples[index]
        spreads.append(max(zones) - min(zones))
        phase = "transition" if t - changed_at_s < TRANSITION_S else "steady"
        samples[phase] += 1
        for band_index, band in enumerate(COMFORT_BANDS_K):
            for i, zone in enumerate(zones):
                deviation = abs(zone - setpoints[index]) - band
                excess[phase][band_index][i] += max(0.0, deviation)
        energy += facade.heat_samples[index] * STEP_S / 60.0
        valves = facade.valve_samples[index]
        if previous is not None:
            travel += sum(abs(a - b) for a, b in zip(valves, previous, strict=True))
        previous = valves

    def _worst(phase: str) -> tuple[float, ...]:
        count = max(1, samples[phase])
        return tuple(max(zones) / count for zones in excess[phase])

    return ZoneMetrics(
        spread_K=statistics.fmean(spreads),
        steady_excess_K=_worst("steady"),
        transition_excess_K=_worst("transition"),
        energy_K_h=energy,
        valve_travel_percent=travel,
    )


def run_case(
    controller: str, scenario_name: str, case: ZoneCase, split: bool, noise_seed: int
) -> ZoneMetrics:
    """Run one controller through one room, with or without the split.

    Parameters
    ----------
    controller : str
        Name of the controller's benchmark adapter.
    scenario_name : str
        Key into :data:`SCENARIOS`.
    case : ZoneCase
        The room.
    split : bool
        Whether the production split divides the group's command.
    noise_seed : int
        Realisation of the room sensor's noise.

    Returns
    -------
    ZoneMetrics
        What the run did to the zones after the warm-up.
    """
    scenario = replace(
        with_warmup(SCENARIOS[scenario_name], WARMUP_S), noise_seed=noise_seed
    )
    plant = MultiZonePlant(
        case.plant,
        settled_state_at(
            case.plant,
            case.sensor_weights,
            scenario.setpoint_schedule(0.0),
            scenario.outdoor_schedule(0.0),
        ),
    )
    facade = _MultiZoneFacade(plant, case, split)
    series = _drive_adapter(
        _make_adapter(controller, None),
        facade,
        scenario,
        STEP_S,
        scenario.duration_minutes * 60.0,
        handle_controller_restart=False,
        nominal_flow_temperature=case.plant.T_water,
    )
    return zone_metrics(list(series.t_s), list(series.T_setpoint), facade, WARMUP_S)


@dataclass(frozen=True)
class PairedRun:
    """The even command and the split in the same room, under the same noise."""

    controller: str
    scenario: str
    room: int
    case: ZoneCase
    even: ZoneMetrics
    split: ZoneMetrics


def _run_pair(unit: tuple[str, str, int, ZoneCase]) -> PairedRun:
    """Run one room once with the even command and once with the split."""
    controller, scenario_name, room, case = unit
    return PairedRun(
        controller=controller,
        scenario=scenario_name,
        room=room,
        case=case,
        even=run_case(controller, scenario_name, case, False, room),
        split=run_case(controller, scenario_name, case, True, room),
    )


def sweep_units(rooms: int, seed: int = 0) -> list[tuple[str, str, int, ZoneCase]]:
    """Return every paired run of the sweep; each room has its own noise."""
    return [
        (controller, scenario, index, case)
        for (index, case), controller, scenario in product(
            enumerate(sample_rooms(rooms, seed)), CONTROLLERS, SCENARIOS
        )
    ]


@dataclass(frozen=True)
class RoomEffect:
    """The split's mean effect in one room, over controllers and scenarios."""

    case: ZoneCase
    even_spread_K: float
    split_spread_K: float
    steady_change_K: tuple[float, ...]
    transition_change_K: tuple[float, ...]
    even_steady_K: tuple[float, ...]
    even_energy_K_h: float
    split_energy_K_h: float
    even_travel_percent: float
    split_travel_percent: float


def _mean_of(pairs: list[PairedRun], read: Callable[[PairedRun], float]) -> float:
    """Return the mean of ``read`` over ``pairs``."""
    return statistics.fmean(read(pair) for pair in pairs)


def _room_effect(pairs: list[PairedRun]) -> RoomEffect:
    """Average the paired runs of one room."""

    def _per_band(read: Callable[[PairedRun, int], float]) -> tuple[float, ...]:
        return tuple(
            statistics.fmean(read(pair, band) for pair in pairs)
            for band in range(len(COMFORT_BANDS_K))
        )

    return RoomEffect(
        case=pairs[0].case,
        even_spread_K=_mean_of(pairs, lambda p: p.even.spread_K),
        split_spread_K=_mean_of(pairs, lambda p: p.split.spread_K),
        steady_change_K=_per_band(
            lambda p, b: p.split.steady_excess_K[b] - p.even.steady_excess_K[b]
        ),
        transition_change_K=_per_band(
            lambda p, b: p.split.transition_excess_K[b] - p.even.transition_excess_K[b]
        ),
        even_steady_K=_per_band(lambda p, b: p.even.steady_excess_K[b]),
        even_energy_K_h=_mean_of(pairs, lambda p: p.even.energy_K_h),
        split_energy_K_h=_mean_of(pairs, lambda p: p.split.energy_K_h),
        even_travel_percent=_mean_of(pairs, lambda p: p.even.valve_travel_percent),
        split_travel_percent=_mean_of(pairs, lambda p: p.split.valve_travel_percent),
    )


def room_effects(runs: Iterable[PairedRun]) -> list[RoomEffect]:
    """Average the paired runs of each room into one effect per room.

    The rooms, not the runs, are the independent samples of the sweep.
    """
    grouped: dict[int, list[PairedRun]] = defaultdict(list)
    for run in runs:
        grouped[run.room].append(run)
    return [_room_effect(pairs) for _, pairs in sorted(grouped.items())]


def bootstrap_interval(values: list[float], seed: int = 0) -> tuple[float, float]:
    """Return the 95 % bootstrap interval of the mean of ``values``."""
    rng = np.random.default_rng(seed)
    data = np.asarray(values, dtype=float)
    picks = rng.integers(0, len(data), size=(_BOOTSTRAP_SAMPLES, len(data)))
    means = data[picks].mean(axis=1)
    low, high = np.percentile(means, [2.5, 97.5])
    return float(low), float(high)


def _steady_change(effect: RoomEffect) -> float:
    """Return the steady change at the primary comfort band."""
    return effect.steady_change_K[PRIMARY_BAND_INDEX]


def summarise(
    effects: list[RoomEffect], read: Callable[[RoomEffect], float] = _steady_change
) -> dict[str, float]:
    """Summarise the split's effect over ``effects``.

    Parameters
    ----------
    effects : list[RoomEffect]
        The rooms to summarise, at least one.
    read : Callable[[RoomEffect], float]
        The change to summarise; a negative change is an improvement.

    Returns
    -------
    dict[str, float]
        Room count, mean change with its 95 % interval, the 10th to 90th
        percentile over the rooms, the shares of rooms that improved and
        worsened, and the change in energy and valve travel in percent.
    """
    changes = [read(effect) for effect in effects]
    low, high = bootstrap_interval(changes)
    p10, p90 = np.percentile(changes, [10, 90])
    even_energy = sum(e.even_energy_K_h for e in effects)
    even_travel = sum(e.even_travel_percent for e in effects)
    return {
        "rooms": float(len(effects)),
        "even_spread_K": statistics.fmean(e.even_spread_K for e in effects),
        "split_spread_K": statistics.fmean(e.split_spread_K for e in effects),
        "mean_change_K": statistics.fmean(changes),
        "interval_low_K": low,
        "interval_high_K": high,
        "p10_K": float(p10),
        "p90_K": float(p90),
        "improved": statistics.fmean(float(c < -1e-3) for c in changes),
        "worsened": statistics.fmean(float(c > 1e-3) for c in changes),
        "energy_change_percent": 100.0
        * (sum(e.split_energy_K_h for e in effects) / even_energy - 1.0),
        "travel_change_percent": 100.0
        * (sum(e.split_travel_percent for e in effects) / max(1e-9, even_travel) - 1.0),
    }


def by_spread(effects: list[RoomEffect]) -> dict[str, list[RoomEffect]]:
    """Group rooms by how far the zones lie apart under the even command."""
    groups: dict[str, list[RoomEffect]] = {}
    for low, high in SPREAD_BINS_K:
        members = [e for e in effects if low <= e.even_spread_K < high]
        if members:
            label = f"{low:.1f}+ K" if math.isinf(high) else f"{low:.1f}-{high:.1f} K"
            groups[label] = members
    return groups


def reading_class(case: ZoneCase) -> str:
    """Name what the cold zone's TRV tells the split about its zone."""
    if abs(case.cold_trv_bias_K) <= ACCURATE_BIAS_K:
        return "accurate"
    return "reads too warm" if case.cold_trv_bias_K > 0.0 else "reads too cold"


def _radiator_class(case: ZoneCase) -> str:
    """Name the cold zone's radiator size against the warm zone's."""
    if case.cold_radiator_scale < 0.87:
        return "small"
    return "large" if case.cold_radiator_scale > 1.13 else "equal"


def _print_table(
    title: str,
    groups: dict[str, list[RoomEffect]],
    read: Callable[[RoomEffect], float] = _steady_change,
) -> None:
    """Print one summary table; a negative change is an improvement."""
    print(f"\n{title}")
    print(
        f"{'group':>16} {'rooms':>5} {'spread':>12} {'change K':>9} "
        f"{'95 % interval':>18} {'p10..p90':>16} {'better':>7} {'worse':>6} "
        f"{'energy':>7} {'travel':>7}"
    )
    for label, members in groups.items():
        row = summarise(members, read)
        print(
            f"{label:>16} {row['rooms']:>5.0f} "
            f"{row['even_spread_K']:>5.2f}->{row['split_spread_K']:<5.2f} "
            f"{row['mean_change_K']:>+9.3f} "
            f"[{row['interval_low_K']:>+7.3f},{row['interval_high_K']:>+7.3f}] "
            f"[{row['p10_K']:>+6.3f},{row['p90_K']:>+6.3f}] "
            f"{row['improved']:>7.0%} {row['worsened']:>6.0%} "
            f"{row['energy_change_percent']:>+6.1f}% "
            f"{row['travel_change_percent']:>+6.1f}%"
        )


def _grouped(
    effects: list[RoomEffect], key: Callable[[ZoneCase], str]
) -> dict[str, list[RoomEffect]]:
    """Group rooms by a label of their case."""
    groups: dict[str, list[RoomEffect]] = defaultdict(list)
    for effect in effects:
        groups[key(effect.case)].append(effect)
    return dict(sorted(groups.items()))


def print_report(effects: list[RoomEffect]) -> None:
    """Print how the split's effect depends on the room."""
    accurate = [e for e in effects if reading_class(e.case) == "accurate"]
    _print_table("All rooms, steady, 0.5 K band", {"all": effects})
    _print_table("By zone spread, accurate readings", by_spread(accurate))
    _print_table("By zone spread, all readings", by_spread(effects))
    for band_index, band in enumerate(COMFORT_BANDS_K):
        _print_table(
            f"By zone spread, accurate readings, steady, {band} K band",
            by_spread(accurate),
            lambda e, b=band_index: e.steady_change_K[b],
        )
    _print_table(
        "By zone spread, accurate readings, setpoint transitions, 0.5 K band",
        by_spread(accurate),
        lambda e: e.transition_change_K[PRIMARY_BAND_INDEX],
    )
    _print_table("By cold TRV reading", _grouped(effects, reading_class))
    _print_table(
        "By valve",
        _grouped(effects, lambda c: "sticky" if c.sticky_valves else "ideal"),
    )
    _print_table(
        "By TRV report interval",
        _grouped(effects, lambda c: f"{c.trv_report_interval_s:.0f} s"),
    )
    _print_table("By cold radiator size", _grouped(effects, _radiator_class))


def main(argv: list[str] | None = None) -> int:
    """Run the sweep, print its summary and optionally store every run."""
    parser = argparse.ArgumentParser(
        description="Sweep the temperature-based valve split over multi-zone rooms."
    )
    parser.add_argument("--rooms", type=int, default=1500, help="Rooms to draw.")
    parser.add_argument("--seed", type=int, default=0, help="Seed of the draw.")
    parser.add_argument("--workers", type=int, default=None, help="Worker processes.")
    parser.add_argument("--json", type=Path, default=None, help="File for every run.")
    args = parser.parse_args(argv)

    units = sweep_units(args.rooms, args.seed)
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        runs = list(pool.map(_run_pair, units, chunksize=16))

    print_report(room_effects(runs))
    if args.json is not None:
        args.json.write_text(json.dumps([asdict(run) for run in runs]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
