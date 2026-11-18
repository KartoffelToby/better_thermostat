"""Sweep of the temperature-based valve split over multi-zone rooms.

Better Thermostat can split a group's valve command by each TRV's own reading.
Whether that helps depends on what the readings say about the room: how far the
zones drift apart, how much of a reading is the TRV's own radiator, and where
the room sensor hangs. None of these is known for a real room, so this runner
compares the even command with the production split across a grid of them.

Zone 0 is the zone that loses the larger share of the room's heat.

Run with ``uv run python -m tests.benchmark.multi_zone_runner``.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, replace
from itertools import product
import json
from pathlib import Path
import statistics

from custom_components.better_thermostat.utils.calibration.mpc import (
    distribute_valve_percent,
)

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
# Deviation a zone may show from the setpoint before it counts as discomfort.
ZONE_COMFORT_BAND_K = 0.5

SCENARIOS: dict[str, ScenarioConfig] = {
    scenario.name: scenario
    for scenario in (S07_OUTDOOR_STEP_COLD, S14_NIGHTLY_SETBACK, S15_DAILY_CYCLE)
}
CONTROLLERS = ("mpc", "mpc_v2")
COLD_LOSS_SHARES = (0.5, 0.65, 0.8)
AIR_EXCHANGES = (1.0, 4.0, 16.0, 64.0)
RAD_FRACTIONS = (0.0, 0.1, 0.2, 0.3)
SENSOR_COLD_WEIGHTS = (0.0, 0.5, 1.0)
COLD_TRV_BIASES_K = (0.0, 1.0)


@dataclass(frozen=True)
class ZoneCase:
    """One room of the sweep.

    Attributes
    ----------
    cold_loss_share : float
        Share of the room's loss the cold zone carries.
    air_exchange : float
        Conductance between the zones, relative to the room's loss.
    rad_fraction : float
        Share of its radiator's excess a TRV's reading picks up.
    sensor_cold_weight : float
        Share of the cold zone in the room sensor's reading.
    cold_trv_bias_K : float
        What the cold zone's TRV reads too warm, as a TRV behind a curtain does.
    """

    cold_loss_share: float
    air_exchange: float
    rad_fraction: float
    sensor_cold_weight: float
    cold_trv_bias_K: float

    @property
    def plant(self) -> MultiZonePlantParams:
        """The two-zone plant of this room."""
        return MultiZonePlantParams(
            loss_shares=(self.cold_loss_share, 1.0 - self.cold_loss_share),
            air_exchange=self.air_exchange,
        )

    @property
    def sensor_weights(self) -> tuple[float, float]:
        """Share of each zone in the room sensor's reading."""
        return (self.sensor_cold_weight, 1.0 - self.sensor_cold_weight)


@dataclass(frozen=True)
class ZoneMetrics:
    """What one run did to the zones over the scored phase.

    Attributes
    ----------
    spread_K : float
        Mean absolute temperature difference between the zones.
    worst_zone_discomfort_K_h : float
        Deviation beyond the comfort band, in the zone that suffered most.
    energy_K_h : float
        Heat the radiators delivered to the room.
    valve_travel_percent : float
        Summed valve movement of all TRVs.
    """

    spread_K: float
    worst_zone_discomfort_K_h: float
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
        """Return what each TRV reports, keyed by zone index."""
        state = self._plant.state
        readings = {
            str(i): trv_sensor_reading(zone, rad, self._case.rad_fraction)
            for i, (zone, rad) in enumerate(
                zip(state.T_zones, state.T_rads, strict=True)
            )
        }
        readings["0"] += self._case.cold_trv_bias_K
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
        percents = self.valve_percents(valve_percent)
        self.zone_samples.append(list(self._plant.state.T_zones))
        self.valve_samples.append(percents)
        self.heat_samples.append(self._plant.heat_to_room_K_per_min)
        self._plant.step(
            dt_s,
            [percent / 100.0 for percent in percents],
            T_outdoor,
            Q_K_per_min=Q_K_per_min,
            flow_temperature=flow_temperature,
            heat_delivered=heat_delivered,
        )


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
    discomfort = [0.0] * n_zones
    spreads: list[float] = []
    energy = 0.0
    travel = 0.0
    previous: list[float] | None = None
    for index, t in enumerate(t_s):
        if t < scored_from_s:
            continue
        zones = facade.zone_samples[index]
        spreads.append(max(zones) - min(zones))
        for i, zone in enumerate(zones):
            excess = abs(zone - setpoints[index]) - ZONE_COMFORT_BAND_K
            discomfort[i] += max(0.0, excess) * STEP_S / 3600.0
        energy += facade.heat_samples[index] * STEP_S / 60.0
        valves = facade.valve_samples[index]
        if previous is not None:
            travel += sum(abs(a - b) for a, b in zip(valves, previous, strict=True))
        previous = valves
    return ZoneMetrics(
        spread_K=statistics.fmean(spreads),
        worst_zone_discomfort_K_h=max(discomfort),
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
    case: ZoneCase
    noise_seed: int
    even: ZoneMetrics
    split: ZoneMetrics


def _run_pair(unit: tuple[str, str, ZoneCase, int]) -> PairedRun:
    """Run one room once with the even command and once with the split."""
    controller, scenario_name, case, noise_seed = unit
    return PairedRun(
        controller=controller,
        scenario=scenario_name,
        case=case,
        noise_seed=noise_seed,
        even=run_case(controller, scenario_name, case, False, noise_seed),
        split=run_case(controller, scenario_name, case, True, noise_seed),
    )


def sweep_units(seeds: int) -> list[tuple[str, str, ZoneCase, int]]:
    """Return every paired run of the sweep."""
    cases = [
        ZoneCase(*values)
        for values in product(
            COLD_LOSS_SHARES,
            AIR_EXCHANGES,
            RAD_FRACTIONS,
            SENSOR_COLD_WEIGHTS,
            COLD_TRV_BIASES_K,
        )
    ]
    return list(product(CONTROLLERS, SCENARIOS, cases, range(seeds)))


def _summarise(runs: list[PairedRun]) -> dict[str, float]:
    """Return the mean effect of the split over ``runs``."""
    even_energy = sum(run.even.energy_K_h for run in runs)
    split_energy = sum(run.split.energy_K_h for run in runs)
    return {
        "runs": len(runs),
        "spread_even_K": statistics.fmean(run.even.spread_K for run in runs),
        "spread_split_K": statistics.fmean(run.split.spread_K for run in runs),
        "discomfort_even_K_h": statistics.fmean(
            run.even.worst_zone_discomfort_K_h for run in runs
        ),
        "discomfort_split_K_h": statistics.fmean(
            run.split.worst_zone_discomfort_K_h for run in runs
        ),
        "energy_change_percent": 100.0 * (split_energy / even_energy - 1.0),
        "travel_change_percent": 100.0
        * (
            sum(run.split.valve_travel_percent for run in runs)
            / max(1e-9, sum(run.even.valve_travel_percent for run in runs))
            - 1.0
        ),
        "share_of_runs_improved": statistics.fmean(
            float(
                run.split.worst_zone_discomfort_K_h
                < run.even.worst_zone_discomfort_K_h - 1e-9
            )
            for run in runs
        ),
        "share_of_runs_worsened": statistics.fmean(
            float(
                run.split.worst_zone_discomfort_K_h
                > run.even.worst_zone_discomfort_K_h + 1e-9
            )
            for run in runs
        ),
    }


def summarise_by(runs: list[PairedRun], axis: str) -> dict[str, dict[str, float]]:
    """Group ``runs`` by one attribute of their room or by controller."""
    groups: dict[str, list[PairedRun]] = defaultdict(list)
    for run in runs:
        value = (
            getattr(run.case, axis) if hasattr(run.case, axis) else getattr(run, axis)
        )
        groups[str(value)].append(run)
    return {value: _summarise(group) for value, group in sorted(groups.items())}


def _print_table(title: str, rows: dict[str, dict[str, float]]) -> None:
    """Print one summary table."""
    print(f"\n{title}")
    print(
        f"{'value':>14} {'runs':>6} {'spread even':>11} {'split':>7} "
        f"{'discomfort even':>15} {'split':>7} {'energy %':>9} "
        f"{'better':>7} {'worse':>7}"
    )
    for value, row in rows.items():
        print(
            f"{value:>14} {row['runs']:>6.0f} {row['spread_even_K']:>11.2f} "
            f"{row['spread_split_K']:>7.2f} {row['discomfort_even_K_h']:>15.2f} "
            f"{row['discomfort_split_K_h']:>7.2f} "
            f"{row['energy_change_percent']:>+9.2f} "
            f"{row['share_of_runs_improved']:>7.0%} "
            f"{row['share_of_runs_worsened']:>7.0%}"
        )


def main(argv: list[str] | None = None) -> int:
    """Run the sweep, print its summary and optionally store every run."""
    parser = argparse.ArgumentParser(
        description="Sweep the temperature-based valve split over multi-zone rooms."
    )
    parser.add_argument("--seeds", type=int, default=3, help="Noise realisations.")
    parser.add_argument("--workers", type=int, default=None, help="Worker processes.")
    parser.add_argument("--json", type=Path, default=None, help="File for every run.")
    args = parser.parse_args(argv)

    units = sweep_units(args.seeds)
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        runs = list(pool.map(_run_pair, units, chunksize=16))

    _print_table("All rooms", {"all": _summarise(runs)})
    for axis in (
        "cold_loss_share",
        "air_exchange",
        "rad_fraction",
        "sensor_cold_weight",
        "cold_trv_bias_K",
        "controller",
        "scenario",
    ):
        _print_table(axis, summarise_by(runs, axis))
    if args.json is not None:
        args.json.write_text(json.dumps([asdict(run) for run in runs]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
