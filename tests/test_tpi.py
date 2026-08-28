"""Tests for the TPI (Time Proportional Integrator) controller."""

from random import Random
from unittest.mock import patch

import pytest

from custom_components.better_thermostat.utils.calibration.tpi import (
    TpiInput,
    TpiParams,
    TpiState,
    build_tpi_key,
    compute_tpi,
)


class TestTpiController:
    """Test cases for TPI controller.

    State is threaded explicitly per test, mirroring how the StateManager
    owns controller state in production.
    """

    def test_blocked_by_window_or_heating_not_allowed(self):
        """Test that duty cycle is 0 when heating is blocked."""
        params = TpiParams()
        state = TpiState()
        inp = TpiInput(
            key="test",
            current_temp_C=20.0,
            target_temp_C=22.0,
            window_open=True,
            heating_allowed=True,
        )
        result, state = compute_tpi(inp, params, state=state)
        assert result.duty_cycle_pct == 0.0
        assert result.debug["reason"] == "blocked"

        inp.heating_allowed = False
        inp.window_open = False
        result, state = compute_tpi(inp, params, state=state)
        assert result.duty_cycle_pct == 0.0
        assert result.debug["reason"] == "blocked"

    def test_missing_temperatures(self):
        """Test behavior when temperatures are missing."""
        params = TpiParams()
        state = TpiState()
        inp = TpiInput(key="test", current_temp_C=None, target_temp_C=22.0)
        result, state = compute_tpi(inp, params, state=state)
        assert result.duty_cycle_pct == 0.0  # No last_percent, so 0
        assert result.debug["reason"] == "missing_temps"

        # Now with last_percent
        inp.current_temp_C = 20.0
        result, state = compute_tpi(inp, params, state=state)
        # Should calculate normally, clamped to 100
        assert result.duty_cycle_pct == 100.0

    def test_normal_calculation(self):
        """Test normal TPI calculation."""
        params = TpiParams(coef_int=0.5, coef_ext=0.02)
        state = TpiState()
        inp = TpiInput(
            key="test", current_temp_C=20.0, target_temp_C=22.0, outdoor_temp_C=15.0
        )
        result, state = compute_tpi(inp, params, state=state)
        assert result.duty_cycle_pct == 100.0  # clamped
        assert result.debug["error_K"] == 2.0
        assert result.debug["raw_pct"] == 114.0

    def test_overshoot_threshold(self):
        """Test that heating is disabled on overshoot."""
        params = TpiParams(threshold_high=0.5)
        state = TpiState()
        inp = TpiInput(
            key="test",
            current_temp_C=22.6,
            target_temp_C=22.0,  # error = -0.6
        )
        result, state = compute_tpi(inp, params, state=state)
        assert result.duty_cycle_pct == 0.0
        assert result.debug["reason"] == "threshold_high"

    def test_clamping(self):
        """Test min/max clamping."""
        params = TpiParams(clamp_min_pct=10.0, clamp_max_pct=90.0, coef_int=1.0)
        state = TpiState()
        inp = TpiInput(
            key="test",
            current_temp_C=20.0,
            target_temp_C=25.0,  # error=5, duty=500, clamped to 90
        )
        result, state = compute_tpi(inp, params, state=state)
        assert result.duty_cycle_pct == 90.0

        inp.target_temp_C = 19.0  # error=-1, duty=-100, clamped to 10
        result, state = compute_tpi(inp, params, state=state)
        assert result.duty_cycle_pct == 10.0

    def test_build_tpi_key(self):
        """Test key building for state tracking."""

        class MockBT:
            def __init__(self):
                self.bt_target_temp: float | None = 22.5
                self.unique_id = "test_bt"

        bt = MockBT()
        key = build_tpi_key(bt, "climate.test")
        assert key == "test_bt:climate.test:t22.5"

        bt.bt_target_temp = None
        key = build_tpi_key(bt, "climate.test")
        assert key == "test_bt:climate.test:tunknown"


class TestTpiTimeHandling:
    """Injected timestamps replace the module's wall clock."""

    def test_injected_now_is_used_without_wall_clock(self):
        """Passing ``now`` stamps the state; the wall clock is never read."""
        params = TpiParams()
        state = TpiState()
        inp = TpiInput(
            key="k",
            current_temp_C=20.0,
            target_temp_C=22.0,
            window_open=False,
            heating_allowed=True,
        )
        with patch(
            "custom_components.better_thermostat.utils.calibration.tpi.monotonic",
            side_effect=AssertionError("wall clock must not be read"),
        ):
            result, state = compute_tpi(inp, params, state=state, now=500.0)

        assert result is not None
        assert state.last_update_ts == 500.0


class TestTpiOverManyCycles:
    """Properties of a run of cycles, not of a single computation."""

    def test_duty_cycle_depends_only_on_the_current_readings(self):
        """With both temperatures present, no earlier cycle may shift the output.

        TPI carries no accumulator, so replaying a reading against a fresh
        state has to give the same duty cycle as the same reading reached at
        the end of a long, varied run.
        """
        params = TpiParams()
        rng = Random(7)
        state = TpiState()

        for cycle in range(200):
            inp = TpiInput(
                key="k",
                current_temp_C=18.0 + rng.random() * 6.0,
                target_temp_C=20.0 + rng.random() * 3.0,
                outdoor_temp_C=-10.0 + rng.random() * 30.0,
            )
            carried, state = compute_tpi(inp, params, state=state, now=float(cycle))
            fresh, _ = compute_tpi(inp, params, state=TpiState(), now=float(cycle))
            assert carried.duty_cycle_pct == fresh.duty_cycle_pct

    def test_constant_error_holds_a_constant_duty_cycle(self):
        """A standing error must neither ramp the duty cycle up nor let it decay.

        The command is proportional to the current error, so repeating the same
        reading has to repeat the same duty cycle for as long as the error
        stands.
        """
        params = TpiParams(coef_int=0.6, coef_ext=0.01)
        state = TpiState()
        inp = TpiInput(
            key="k", current_temp_C=21.8, target_temp_C=22.0, outdoor_temp_C=5.0
        )
        expected_pct = 100.0 * (
            params.coef_int * (22.0 - 21.8) + params.coef_ext * (22.0 - 5.0)
        )

        duty_cycles = []
        for cycle in range(50):
            result, state = compute_tpi(
                inp, params, state=state, now=float(cycle) * 300.0
            )
            duty_cycles.append(result.duty_cycle_pct)

        assert duty_cycles == pytest.approx([expected_pct] * 50)

    def test_last_duty_cycle_is_held_for_every_cycle_of_a_sensor_dropout(self):
        """A room sensor that stops reporting must freeze the command, not drop it.

        The held value is the only state that reaches across cycles, so it has
        to survive the whole gap and give way to live readings again once the
        sensor returns.
        """
        params = TpiParams()
        state = TpiState()
        reading = TpiInput(
            key="k", current_temp_C=21.7, target_temp_C=22.0, outdoor_temp_C=5.0
        )
        gap = TpiInput(
            key="k", current_temp_C=None, target_temp_C=22.0, outdoor_temp_C=5.0
        )

        warm, state = compute_tpi(reading, params, state=state, now=0.0)
        held_pct = warm.duty_cycle_pct
        assert held_pct > 0.0

        for cycle in range(1, 13):
            result, state = compute_tpi(gap, params, state=state, now=float(cycle))
            assert result.debug["reason"] == "missing_temps"
            assert result.duty_cycle_pct == held_pct

        colder = TpiInput(
            key="k", current_temp_C=21.0, target_temp_C=22.0, outdoor_temp_C=5.0
        )
        after_gap, _ = compute_tpi(colder, params, state=state, now=200.0)
        fresh, _ = compute_tpi(colder, params, state=TpiState(), now=200.0)
        assert after_gap.duty_cycle_pct == fresh.duty_cycle_pct
        assert after_gap.duty_cycle_pct != held_pct
