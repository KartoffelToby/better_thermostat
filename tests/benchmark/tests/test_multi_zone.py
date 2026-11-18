"""Multi-zone plant and valve split sweep tests."""

from __future__ import annotations

import pytest

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
    ZoneCase,
    _MultiZoneFacade,
    run_case,
    with_warmup,
    zone_metrics,
)
from tests.benchmark.scenarios import S07_OUTDOOR_STEP_COLD

COLD_CORNER = MultiZonePlantParams(loss_shares=(0.8, 0.2), air_exchange=1.0)


def _case(**overrides) -> ZoneCase:
    values = {
        "cold_loss_share": 0.8,
        "air_exchange": 1.0,
        "rad_fraction": 0.0,
        "sensor_cold_weight": 0.5,
        "cold_trv_bias_K": 0.0,
    }
    return ZoneCase(**{**values, **overrides})


def _facade(case: ZoneCase, T_zones, T_rads, split=True) -> _MultiZoneFacade:
    plant = MultiZonePlant(case.plant, MultiZonePlantState(T_zones, T_rads))
    return _MultiZoneFacade(plant, case, split)


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


def test_the_even_command_reaches_every_trv_unchanged():
    """Without the split the readings play no part."""
    facade = _facade(_case(), [19.0, 21.0], [30.0, 30.0], split=False)

    assert facade.valve_percents(40.0) == [40.0, 40.0]


def test_the_split_opens_the_colder_trv_by_the_production_rule():
    """2 K below the warmest TRV is 16 percentage points more."""
    facade = _facade(_case(), [19.0, 21.0], [30.0, 30.0])

    assert facade.valve_percents(40.0) == [56.0, 40.0]


def test_a_closed_group_stays_closed_under_the_split():
    """A colder reading never opens a valve the controller closed."""
    facade = _facade(_case(), [19.0, 21.0], [30.0, 30.0])

    assert facade.valve_percents(0.0) == [0.0, 0.0]


def test_a_reading_follows_the_trvs_own_radiator():
    """The hotter radiator pushes its TRV's reading up."""
    facade = _facade(_case(rad_fraction=0.2), [20.0, 20.0], [40.0, 20.0])

    assert facade.trv_readings() == {"0": 24.0, "1": 20.0}


def test_a_biased_cold_trv_hides_the_cold_zone():
    """A cold TRV reading warm receives no boost."""
    facade = _facade(_case(cold_trv_bias_K=2.0), [19.0, 21.0], [30.0, 30.0])

    assert facade.valve_percents(40.0) == [40.0, 40.0]


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
    """Discomfort of the worst zone beyond the band, valve travel of all TRVs."""
    facade = _facade(_case(), [20.0, 20.0], [20.0, 20.0])
    facade.zone_samples = [[10.0, 10.0], [19.0, 21.0], [19.0, 21.25]]
    facade.valve_samples = [[0.0, 0.0], [10.0, 20.0], [15.0, 20.0]]
    facade.heat_samples = [9.0, 1.0, 1.0]

    metrics = zone_metrics([0.0, 30.0, 60.0], [21.0, 21.0, 21.0], facade, 30.0)

    assert metrics.spread_K == pytest.approx(2.125)
    assert metrics.worst_zone_discomfort_K_h == pytest.approx(3.0 * 30.0 / 3600.0)
    assert metrics.energy_K_h == pytest.approx(1.0)
    assert metrics.valve_travel_percent == pytest.approx(5.0)


def test_the_split_warms_a_cold_corner_when_the_readings_show_it():
    """A room the split is meant for: the zones draw closer together."""
    case = _case(rad_fraction=0.0)

    even = run_case("mpc_v2", S07_OUTDOOR_STEP_COLD.name, case, False, 0)
    split = run_case("mpc_v2", S07_OUTDOOR_STEP_COLD.name, case, True, 0)

    assert split.spread_K < even.spread_K


def test_a_run_is_reproducible():
    """The same room and noise give the same result."""
    case = _case()

    assert run_case("mpc", S07_OUTDOOR_STEP_COLD.name, case, True, 1) == run_case(
        "mpc", S07_OUTDOOR_STEP_COLD.name, case, True, 1
    )
