"""Multi-zone plant and valve split sweep tests."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tests.benchmark import multi_zone_runner
from tests.benchmark.multi_trv_plant import (
    MultiTrvPlant,
    MultiTrvPlantParams,
    MultiTrvPlantState,
)
from tests.benchmark.multi_zone_plant import (
    MultiZonePlant,
    MultiZonePlantParams,
    MultiZonePlantState,
    settled_state_at,
    steady_state,
)
from tests.benchmark.multi_zone_runner import (
    COMFORT_BANDS_K,
    SCENARIOS,
    STEP_S,
    TRANSITION_S,
    TRV_REPORT_INTERVALS_S,
    WARMUP_S,
    PairedRun,
    RoomEffect,
    ZoneCase,
    ZoneMetrics,
    _MultiZoneFacade,
    _run_pair,
    bootstrap_interval,
    by_spread,
    reading_class,
    room_effects,
    run_case,
    sample_rooms,
    summarise,
    sweep_units,
    with_warmup,
    zone_metrics,
)
from tests.benchmark.scenarios import S07_OUTDOOR_STEP_COLD

COLD_CORNER = MultiZonePlantParams(loss_shares=(0.8, 0.2), air_exchange=1.0)


def _case(**overrides) -> ZoneCase:
    room = ZoneCase(
        cold_loss_share=0.8,
        air_exchange=1.0,
        cold_rad_fraction=0.0,
        warm_rad_fraction=0.0,
        sensor_cold_weight=0.5,
        cold_trv_bias_K=0.0,
    )
    return replace(room, **overrides)


def _facade(case: ZoneCase, T_zones, T_rads, split=True) -> _MultiZoneFacade:
    plant = MultiZonePlant(case.plant, MultiZonePlantState(T_zones, T_rads))
    return _MultiZoneFacade(plant, case, split)


def _metrics(change: float = 0.0, spread: float = 1.0) -> ZoneMetrics:
    return ZoneMetrics(
        spread_K=spread,
        steady_excess_K=(change, change, change),
        transition_excess_K=(2.0 * change, 2.0 * change, 2.0 * change),
        energy_K_h=10.0,
        valve_travel_percent=100.0,
    )


def _effect(change: float, spread: float = 1.0, bias: float = 0.0) -> RoomEffect:
    return RoomEffect(
        case=_case(cold_trv_bias_K=bias),
        even_spread_K=spread,
        split_spread_K=spread,
        steady_change_K=(change, change, change),
        transition_change_K=(change, change, change),
        even_steady_K=(1.0, 1.0, 1.0),
        even_energy_K_h=10.0,
        split_energy_K_h=11.0,
        even_travel_percent=100.0,
        split_travel_percent=150.0,
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"gain_heaters": (3.0,)}, "two zones"),
        ({"coupling_rad_zone": (1.5,)}, "one entry per zone"),
        ({"tau_room_min": 0.0}, "must be > 0"),
        ({"loss_shares": (0.5, 0.6)}, "sum to 1"),
        ({"air_exchange": -1.0}, "must be >= 0"),
    ],
)
def test_parameters_the_plant_cannot_integrate_are_refused(overrides, message):
    """A wrong room fails where it is built."""
    with pytest.raises(ValueError, match=message):
        MultiZonePlantParams(**overrides)


def test_the_state_must_match_the_zones():
    """Every zone needs a temperature and a radiator."""
    with pytest.raises(ValueError, match="2 zones"):
        MultiZonePlant(COLD_CORNER, MultiZonePlantState([20.0], [20.0]))


def test_the_integrator_settles_where_the_solver_says():
    """Simulation and closed form describe the same plant."""
    expected = steady_state(COLD_CORNER, [0.2, 0.4], 0.0)
    plant = MultiZonePlant(COLD_CORNER, MultiZonePlantState([20.0, 20.0], [20.0, 20.0]))

    for _ in range(40_000):
        plant.step(30.0, [0.2, 0.4], 0.0)

    assert plant.state.T_zones == pytest.approx(expected.T_zones, abs=1e-3)
    assert plant.state.T_rads == pytest.approx(expected.T_rads, abs=1e-3)


def test_the_zone_with_the_larger_loss_is_colder():
    """The same opening leaves the cold corner behind."""
    state = steady_state(COLD_CORNER, [0.3, 0.3], 0.0)

    assert state.T_zones[0] < state.T_zones[1]


def test_more_air_exchange_narrows_the_spread():
    """Well-mixed air evens the zones out."""
    weak = steady_state(COLD_CORNER, [0.3, 0.3], 0.0)
    strong = steady_state(
        MultiZonePlantParams(loss_shares=(0.8, 0.2), air_exchange=64.0), [0.3, 0.3], 0.0
    )

    assert strong.T_zones[1] - strong.T_zones[0] < weak.T_zones[1] - weak.T_zones[0]


def test_unlimited_exchange_is_the_single_room_node():
    """Without a spread the plant is the multi-TRV plant it generalises."""
    zones = MultiZonePlant(
        MultiZonePlantParams(air_exchange=500.0),
        MultiZonePlantState([20.0, 20.0], [20.0, 20.0]),
    )
    room = MultiTrvPlant(
        MultiTrvPlantParams(
            n_trvs=2, gain_heaters=[3.0, 3.0], coupling_rad_room=[1.5, 1.5]
        ),
        MultiTrvPlantState(T_room=20.0, T_rads=[20.0, 20.0]),
    )

    for _ in range(2_000):
        zones.step(5.0, [0.4, 0.4], 0.0)
        room.step(5.0, [0.4, 0.4], 0.0)

    assert zones.state.T_zones[0] == pytest.approx(room.state.T_room, abs=0.01)
    assert zones.state.T_zones[1] == pytest.approx(room.state.T_room, abs=0.01)


def test_the_settled_state_puts_the_room_sensor_on_the_setpoint():
    """A run starts where its room sensor already reads the target."""
    state = settled_state_at(COLD_CORNER, (1.0, 0.0), 21.0, 0.0)

    assert state.T_zones[0] == pytest.approx(21.0, abs=1e-6)


def test_a_smaller_cold_radiator_scales_only_that_radiator():
    """The warm zone's radiator stays as it is."""
    plant = _case(cold_radiator_scale=0.5).plant

    assert plant.gain_heaters == (1.5, 3.0)
    assert plant.coupling_rad_zone == (0.75, 1.5)


def test_the_even_command_reaches_every_trv_unchanged():
    """Without the split the readings play no part."""
    facade = _facade(_case(), [19.0, 21.0], [30.0, 30.0], split=False)

    assert facade.valve_percents(40.0) == [40.0, 40.0]


def test_the_split_opens_the_colder_trv_by_the_production_rule():
    """2 K below the warmest TRV is 16 percentage points more."""
    facade = _facade(_case(), [19.0, 21.0], [30.0, 30.0])

    assert facade.valve_percents(40.0) == [56.0, 40.0]


def test_the_split_writes_whole_percent():
    """0.3 K below the warmest TRV is 2.4 points, written as 2."""
    facade = _facade(_case(), [20.7, 21.0], [30.0, 30.0])

    assert facade.valve_percents(40.0) == [42.0, 40.0]


def test_a_closed_group_stays_closed_under_the_split():
    """A valve the controller closed stays closed on a colder reading."""
    facade = _facade(_case(), [19.0, 21.0], [30.0, 30.0])

    assert facade.valve_percents(0.0) == [0.0, 0.0]


def test_each_reading_follows_its_own_radiator_share():
    """Two TRVs can sit differently close to their radiators."""
    case = _case(cold_rad_fraction=0.2, warm_rad_fraction=0.1)
    facade = _facade(case, [20.0, 20.0], [40.0, 40.0])

    assert facade.trv_readings() == {"0": 24.0, "1": 22.0}


@pytest.mark.parametrize(
    ("bias", "expected"),
    [(2.0, [40.0, 40.0]), (-1.0, [64.0, 40.0])],
    ids=["too_warm_hides_the_cold_zone", "too_cold_exaggerates_it"],
)
def test_a_biased_cold_trv_moves_the_split(bias, expected):
    """The split acts on what the TRV reports, not on the zone."""
    facade = _facade(_case(cold_trv_bias_K=bias), [19.0, 21.0], [30.0, 30.0])

    assert facade.valve_percents(40.0) == expected


def test_a_warm_trv_reading_too_warm_equals_a_cold_trv_reading_too_cold():
    """The split sees only the difference, so one bias spans both errors."""
    warm = _facade(_case(warm_trv_bias_K=1.0), [19.0, 21.0], [30.0, 30.0])
    cold = _facade(_case(cold_trv_bias_K=-1.0), [19.0, 21.0], [30.0, 30.0])

    assert warm.valve_percents(40.0) == cold.valve_percents(40.0)


def test_a_trv_keeps_its_last_report_until_the_interval_has_passed():
    """Between reports the split works on what the TRV last said."""
    case = _case(trv_report_interval_s=300.0)
    facade = _facade(case, [19.0, 21.0], [30.0, 30.0])
    assert facade.trv_readings() == {"0": 19.0, "1": 21.0}

    facade._plant.state.T_zones[0] = 20.0
    facade.apply(STEP_S, 0.0, 5.0, 0.0, None, True)
    held = facade.trv_readings()["0"]
    for _ in range(9):
        facade.apply(STEP_S, 0.0, 5.0, 0.0, None, True)

    assert held == 19.0
    assert facade.trv_readings()["0"] == pytest.approx(20.0, abs=0.11)


def test_a_sticky_valve_ignores_a_small_command():
    """The deadband keeps a small opening shut."""
    facade = _facade(_case(sticky_valves=True), [20.0, 20.0], [20.0, 20.0], False)

    facade.apply(STEP_S, 4.0, 5.0, 0.0, None, True)

    assert facade.valve_samples == [[0.0, 0.0]]


def test_an_ideal_valve_follows_a_small_command():
    """Without wear the same command opens the valve."""
    facade = _facade(_case(), [20.0, 20.0], [20.0, 20.0], False)

    facade.apply(STEP_S, 4.0, 5.0, 0.0, None, True)

    assert facade.valve_samples == [[pytest.approx(4.0), pytest.approx(4.0)]]


@pytest.mark.parametrize(
    ("weight", "expected"), [(1.0, 19.0), (0.0, 21.0), (0.5, 20.0)]
)
def test_the_room_sensor_reads_where_it_hangs(weight, expected):
    """The controller sees the zone the sensor sits in."""
    facade = _facade(_case(sensor_cold_weight=weight), [19.0, 21.0], [30.0, 30.0])

    assert facade.T_room == pytest.approx(expected)


def test_the_warmup_holds_the_first_conditions_and_scores_after_them():
    """The scenario itself starts unchanged once the warm-up is over."""
    extended = with_warmup(S07_OUTDOOR_STEP_COLD, 3600.0)

    assert extended.outdoor_schedule(1800.0) == S07_OUTDOOR_STEP_COLD.outdoor_schedule(
        0.0
    )
    assert extended.outdoor_schedule(
        3600.0 + 7200.0
    ) == S07_OUTDOOR_STEP_COLD.outdoor_schedule(7200.0)
    assert (
        extended.transient_start_s == S07_OUTDOOR_STEP_COLD.transient_start_s + 3600.0
    )
    assert extended.duration_minutes == S07_OUTDOOR_STEP_COLD.duration_minutes + 60


def test_the_metrics_score_only_the_phase_after_the_warmup():
    """The warm-up's samples leave no trace in any metric."""
    facade = _facade(_case(), [20.0, 20.0], [20.0, 20.0])
    facade.zone_samples = [[10.0, 10.0], [19.0, 21.0], [19.0, 21.25]]
    facade.valve_samples = [[0.0, 0.0], [10.0, 20.0], [15.0, 20.0]]
    facade.heat_samples = [9.0, 1.0, 1.0]

    metrics = zone_metrics([0.0, 30.0, 60.0], [21.0, 21.0, 21.0], facade, 30.0)

    assert metrics.spread_K == pytest.approx(2.125)
    assert metrics.energy_K_h == pytest.approx(1.0)
    assert metrics.valve_travel_percent == pytest.approx(5.0)


def test_excess_is_the_worst_zones_mean_beyond_each_band():
    """Only what lies beyond the band counts, per scored sample."""
    facade = _facade(_case(), [20.0, 20.0], [20.0, 20.0])
    facade.zone_samples = [[19.0, 21.0], [19.0, 21.0]]
    facade.valve_samples = [[0.0, 0.0], [0.0, 0.0]]
    facade.heat_samples = [0.0, 0.0]

    metrics = zone_metrics([0.0, 30.0], [21.0, 21.0], facade, 0.0)

    assert metrics.steady_excess_K == pytest.approx(
        tuple(2.0 - band for band in COMFORT_BANDS_K)
    )
    assert metrics.transition_excess_K == (0.0, 0.0, 0.0)


def test_a_setpoint_change_opens_a_transition_that_ends_on_time():
    """Samples after a change count apart until the transition has passed."""
    facade = _facade(_case(), [20.0, 20.0], [20.0, 20.0])
    t_s = [0.0, 3600.0, TRANSITION_S - 1.0 + 3600.0, TRANSITION_S + 3600.0]
    setpoints = [20.0, 21.0, 21.0, 21.0]
    facade.zone_samples = [[20.0, 20.0], [19.0, 19.0], [19.0, 19.0], [20.5, 21.0]]
    facade.valve_samples = [[0.0, 0.0]] * 4
    facade.heat_samples = [0.0] * 4

    metrics = zone_metrics(t_s, setpoints, facade, 0.0)

    assert metrics.transition_excess_K[1] == pytest.approx(1.5)
    assert metrics.steady_excess_K[1] == pytest.approx(0.0)


def test_the_split_narrows_a_cold_corner_when_the_readings_show_it():
    """A room the split is meant for: the zones draw closer together."""
    case = _case()

    even = run_case("mpc_v2", S07_OUTDOOR_STEP_COLD.name, case, False, 0)
    split = run_case("mpc_v2", S07_OUTDOOR_STEP_COLD.name, case, True, 0)

    assert split.spread_K < even.spread_K


def test_a_run_is_reproducible():
    """The same room and noise give the same result."""
    case = _case()

    assert run_case("mpc", S07_OUTDOOR_STEP_COLD.name, case, True, 1) == run_case(
        "mpc", S07_OUTDOOR_STEP_COLD.name, case, True, 1
    )


def test_a_pair_runs_the_even_command_against_the_split():
    """The two halves of a pair differ in the split and nothing else."""
    case = _case()
    name = S07_OUTDOOR_STEP_COLD.name

    pair = _run_pair(("mpc", name, 2, case))

    assert pair.even == run_case("mpc", name, case, False, 2)
    assert pair.split == run_case("mpc", name, case, True, 2)
    assert pair.even != pair.split


def test_the_warmup_is_left_out_of_the_score(monkeypatch):
    """A run is scored from the end of its warm-up on."""
    scored_from: list[float] = []
    measure = multi_zone_runner.zone_metrics

    def _recording(t_s, setpoints, facade, scored_from_s):
        scored_from.append(scored_from_s)
        return measure(t_s, setpoints, facade, scored_from_s)

    monkeypatch.setattr(multi_zone_runner, "zone_metrics", _recording)

    run_case("mpc", S07_OUTDOOR_STEP_COLD.name, _case(), False, 0)

    assert scored_from == [WARMUP_S]


def test_the_same_draw_gives_the_same_rooms():
    """A sweep can be repeated room by room."""
    assert sample_rooms(20, seed=3) == sample_rooms(20, seed=3)
    assert sample_rooms(20, seed=3) != sample_rooms(20, seed=4)


def test_the_draw_covers_each_range_evenly():
    """Latin hypercube sampling puts one room into every stratum."""
    rooms = sample_rooms(50)

    strata = {int((room.cold_trv_bias_K + 1.0) / 2.0 * 50) for room in rooms}

    assert strata == set(range(50))
    assert all(0.5 <= room.cold_loss_share <= 0.85 for room in rooms)
    assert all(1.0 <= room.air_exchange <= 64.0 for room in rooms)
    assert all(0.6 <= room.cold_radiator_scale <= 1.4 for room in rooms)


def test_the_draw_uses_every_category():
    """Both valves and every report interval occur."""
    rooms = sample_rooms(60)

    assert {room.sticky_valves for room in rooms} == {False, True}
    assert {room.trv_report_interval_s for room in rooms} == set(TRV_REPORT_INTERVALS_S)


def test_the_sweep_pairs_every_room_with_every_controller_and_scenario():
    """A room keeps its noise across controllers and scenarios."""
    units = sweep_units(2)

    assert len(units) == 2 * 2 * len(SCENARIOS)
    assert {unit[2] for unit in units} == {0, 1}


def test_a_room_is_one_sample_however_many_runs_it_holds():
    """Runs of one room average into a single effect."""
    case = _case()
    runs = [
        PairedRun("mpc", "a", 0, case, _metrics(1.0), _metrics(2.0)),
        PairedRun("mpc_v2", "a", 0, case, _metrics(1.0), _metrics(4.0)),
        PairedRun("mpc", "a", 1, case, _metrics(1.0), _metrics(1.0)),
    ]

    effects = room_effects(runs)

    assert [effect.steady_change_K[0] for effect in effects] == [2.0, 0.0]
    assert effects[0].transition_change_K[0] == pytest.approx(4.0)


def test_every_run_of_a_room_counts_towards_its_means():
    """The spread of a room is the mean over all of its runs."""
    case = _case()
    runs = [
        PairedRun("mpc", "a", 0, case, _metrics(spread=1.0), _metrics(spread=0.5)),
        PairedRun("mpc", "b", 0, case, _metrics(spread=3.0), _metrics(spread=1.5)),
    ]

    effect = room_effects(runs)[0]

    assert effect.even_spread_K == pytest.approx(2.0)
    assert effect.split_spread_K == pytest.approx(1.0)


def test_the_interval_brackets_the_mean_and_shrinks_with_more_rooms():
    """More rooms pin the mean down more tightly."""
    few = bootstrap_interval([0.0, 1.0] * 5)
    many = bootstrap_interval([0.0, 1.0] * 500)

    assert few[0] < 0.5 < few[1]
    assert many[1] - many[0] < few[1] - few[0]


def test_the_summary_counts_improved_and_worsened_rooms():
    """A negative change is an improvement."""
    row = summarise([_effect(-0.5), _effect(-0.5), _effect(0.5), _effect(0.0)])

    assert row["rooms"] == 4.0
    assert row["improved"] == 0.5
    assert row["worsened"] == 0.25
    assert row["energy_change_percent"] == pytest.approx(10.0)
    assert row["travel_change_percent"] == pytest.approx(50.0)


def test_rooms_are_grouped_by_their_even_command_spread():
    """A room falls into the band of its spread, and empty bands are left out."""
    groups = by_spread([_effect(0.0, spread=0.1), _effect(0.0, spread=3.0)])

    assert set(groups) == {"0.0-0.2 K", "2.5+ K"}


@pytest.mark.parametrize(
    ("spread", "expected"), [(0.0, "0.0-0.2 K"), (0.2, "0.2-0.4 K")]
)
def test_a_spread_on_a_band_edge_belongs_to_the_upper_band(spread, expected):
    """Every room falls into exactly one band."""
    assert set(by_spread([_effect(0.0, spread=spread)])) == {expected}


@pytest.mark.parametrize(
    ("bias", "expected"),
    [
        (0.25, "accurate"),
        (-0.25, "accurate"),
        (0.26, "reads too warm"),
        (-0.26, "reads too cold"),
    ],
)
def test_a_reading_is_classed_by_its_error(bias, expected):
    """The class names what the cold TRV tells the split."""
    assert reading_class(replace(_case(), cold_trv_bias_K=bias)) == expected
