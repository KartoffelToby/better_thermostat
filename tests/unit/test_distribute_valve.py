"""The split of a group's valve command by its TRVs' own readings.

The warmest TRV of the group is the reference and receives the group
command; every colder TRV receives 8 percent more per kelvin it reads below
the warmest one, held to the valve's range.
"""

import pytest

from custom_components.better_thermostat.utils.calibration.mpc import (
    DISTRIBUTE_COMPENSATION_PERCENT_PER_K,
    distribute_valve_percent,
)


def test_the_compensation_is_eight_percent_per_kelvin():
    assert DISTRIBUTE_COMPENSATION_PERCENT_PER_K == 8.0


def test_the_warmest_trv_gets_the_group_command_and_a_colder_one_more():
    result = distribute_valve_percent(40.0, {"a": 22.0, "b": 22.5, "c": 23.0})

    assert result == pytest.approx({"a": 48.0, "b": 44.0, "c": 40.0})


def test_a_share_is_held_to_the_valve_range():
    result = distribute_valve_percent(45.0, {"warm": 28.1, "cold": 19.5})

    assert result == pytest.approx({"warm": 45.0, "cold": 100.0})


def test_a_command_above_the_range_is_held_for_every_trv():
    result = distribute_valve_percent(130.0, {"warm": 22.0, "cold": 21.0})

    assert result == {"warm": 100.0, "cold": 100.0}


@pytest.mark.parametrize("command", [0.0, -5.0])
def test_a_closed_group_stays_closed(command):
    result = distribute_valve_percent(command, {"warm": 25.0, "cold": 18.0})

    assert result == {"warm": 0.0, "cold": 0.0}


def test_a_trv_without_a_reading_gets_the_group_command_and_is_no_reference():
    result = distribute_valve_percent(
        30.0, {"cold": 22.0, "silent": None, "broken": float("nan"), "warm": 25.0}
    )

    assert result == pytest.approx(
        {"cold": 54.0, "silent": 30.0, "broken": 30.0, "warm": 30.0}
    )


@pytest.mark.parametrize(
    "readings",
    [{}, {"only": 20.0}, {"only": 20.0, "silent": None}, {"a": 21.0, "b": 21.0}],
    ids=["no_trv", "one_trv", "one_reading", "same_reading"],
)
def test_without_two_different_readings_every_trv_gets_the_group_command(readings):
    result = distribute_valve_percent(55.0, readings)

    assert result == dict.fromkeys(readings, 55.0)
