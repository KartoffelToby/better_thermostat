"""The benchmark's room-temperature input stage gates and filters like production."""

from __future__ import annotations

import math

import pytest

from tests.benchmark.input_stage import (
    EMA_TAU_S,
    EMA_TICK_S,
    SIGNIFICANCE_K,
    RoomTemperatureInput,
)


def _feed(stage: RoomTemperatureInput, readings: list[tuple[float, float | None]]):
    for t, reading in readings:
        stage.update(t, reading)


def test_first_reading_is_committed() -> None:
    stage = RoomTemperatureInput()
    stage.update(0.0, 20.004)
    assert stage.room_temperature == 20.0
    assert stage.room_temperature_filtered == 20.0


def test_a_move_below_the_threshold_waits() -> None:
    stage = RoomTemperatureInput()
    _feed(stage, [(0.0, 20.0), (60.0, 20.1)])
    assert stage.room_temperature == 20.0


def test_a_significant_move_is_committed_at_once() -> None:
    stage = RoomTemperatureInput()
    _feed(stage, [(0.0, 20.0), (60.0, 20.0 + SIGNIFICANCE_K)])
    assert stage.room_temperature == pytest.approx(20.11)


def test_same_direction_moves_accumulate() -> None:
    stage = RoomTemperatureInput()
    # Each delta is taken against the committed value: 0.05 + 0.06 = 0.11.
    _feed(stage, [(0.0, 20.0), (60.0, 20.05), (120.0, 20.06)])
    assert stage.room_temperature == pytest.approx(20.06)


def test_a_small_move_that_holds_is_committed_after_the_plateau() -> None:
    stage = RoomTemperatureInput()
    _feed(stage, [(0.0, 20.0), (60.0, 20.05)])
    _feed(stage, [(t, 20.05) for t in (90.0, 120.0, 150.0)])
    assert stage.room_temperature == 20.0
    stage.update(180.0, 20.05)
    assert stage.room_temperature == pytest.approx(20.05)


def test_flicker_back_to_the_committed_value_restarts_the_plateau() -> None:
    stage = RoomTemperatureInput()
    _feed(stage, [(0.0, 20.0), (60.0, 20.05), (120.0, 20.0), (150.0, 20.05)])
    _feed(stage, [(t, 20.05) for t in (180.0, 210.0, 240.0)])
    assert stage.room_temperature == 20.0
    stage.update(270.0, 20.05)
    assert stage.room_temperature == pytest.approx(20.05)


def test_the_filter_follows_a_step_with_the_production_time_constant() -> None:
    stage = RoomTemperatureInput()
    stage.update(0.0, 20.0)
    stage.update(EMA_TICK_S, 21.0)
    for t in range(int(EMA_TICK_S), int(EMA_TICK_S + EMA_TAU_S) + 1, 30):
        stage.update(float(t), 21.0)
    assert stage.room_temperature == 21.0
    assert stage.room_temperature_filtered == pytest.approx(
        21.0 - math.exp(-1.0), abs=0.005
    )


def test_the_tick_derives_the_slope_from_the_filter() -> None:
    stage = RoomTemperatureInput()
    for minute in range(30):
        stage.update(minute * 60.0, 20.0 + 0.02 * minute)
    slope = stage.temperature_slope_K_per_min
    assert slope is not None
    assert 0.01 < slope < 0.03


def test_no_slope_before_two_ticks() -> None:
    stage = RoomTemperatureInput()
    _feed(stage, [(0.0, 20.0), (30.0, 20.0)])
    assert stage.temperature_slope_K_per_min is None


def test_a_long_outage_restarts_the_filter_at_the_returning_reading() -> None:
    stage = RoomTemperatureInput()
    _feed(stage, [(t, 20.0) for t in range(0, 601, 60)])
    _feed(stage, [(t, None) for t in range(660, 901, 60)])
    stage.update(960.0, 20.05)
    assert stage.room_temperature == pytest.approx(20.05)
    assert stage.room_temperature_filtered == pytest.approx(20.05)


def test_a_short_outage_keeps_the_filter() -> None:
    stage = RoomTemperatureInput()
    _feed(stage, [(t, 20.0) for t in range(0, 601, 60)])
    stage.update(630.0, None)
    stage.update(660.0, 20.5)
    assert stage.room_temperature == pytest.approx(20.5)
    assert stage.room_temperature_filtered is not None
    assert stage.room_temperature_filtered < 20.5


def test_the_stage_holds_its_value_through_an_outage() -> None:
    stage = RoomTemperatureInput()
    _feed(stage, [(0.0, 20.0), (60.0, None), (120.0, None)])
    assert stage.room_temperature == 20.0


def test_reset_forgets_every_reading() -> None:
    stage = RoomTemperatureInput()
    _feed(stage, [(t, 20.0 + 0.02 * t / 60) for t in range(0, 601, 60)])
    stage.reset()
    assert stage.room_temperature is None
    assert stage.room_temperature_filtered is None
    assert stage.temperature_slope_K_per_min is None
