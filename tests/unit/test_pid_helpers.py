"""Tests for the shared PID-key helpers and the loop and bucket entries."""

from types import SimpleNamespace

import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.utils.calibration.pid import (
    DEFAULT_PID_AUTO_TUNE,
    DEFAULT_PID_KD,
    DEFAULT_PID_KI,
    DEFAULT_PID_KP,
    PIDParams,
    PIDState,
    effective_pid_gain,
    format_bucket,
    freeze_pid_gains,
    pid_auto_tune,
    pid_cycle_params,
    pid_cycle_state,
    pid_loop_state,
    resolve_unique_id,
    round_to_bucket,
    settle_pid_cycle,
)


class TestResolveUniqueId:
    """resolve_unique_id keys state by the entity's unique id, else ``bt``."""

    def test_uses_the_unique_id(self):
        """An entity with a unique id keys its state under it."""
        assert resolve_unique_id(SimpleNamespace(unique_id="entry_1")) == "entry_1"

    @pytest.mark.parametrize("unique_id", [None, ""])
    def test_an_entity_without_a_unique_id_falls_back_to_bt(self, unique_id):
        """No unique id, or an empty one, keys state under ``bt``."""
        assert resolve_unique_id(SimpleNamespace(unique_id=unique_id)) == "bt"

    def test_a_thermostat_keys_by_its_config_entry(self):
        """The thermostat's unique id is the one its constructor received."""
        bt = BetterThermostat.__new__(BetterThermostat)
        bt._unique_id = "entry_1"
        assert resolve_unique_id(bt) == "entry_1"


class TestBucketHelpers:
    """round_to_bucket snaps to 0.5 °C; format_bucket renders the tag."""

    def test_round_down(self):
        """21.2 snaps to 21.0."""
        assert round_to_bucket(21.2) == 21.0

    def test_round_up(self):
        """21.3 snaps to 21.5."""
        assert round_to_bucket(21.3) == 21.5

    def test_round_exact(self):
        """An already-aligned value is unchanged."""
        assert round_to_bucket(21.5) == 21.5

    def test_format(self):
        """format_bucket renders a one-decimal t-tag."""
        assert format_bucket(21.0) == "t21.0"
        assert format_bucket(21.5) == "t21.5"


_LOOP = "uid:climate.trv"


class TestLoopState:
    """pid_loop_state returns the TRV's loop entry, carried over if missing."""

    def test_returns_the_stored_entry(self):
        """A stored loop entry is returned as it is."""
        loop = PIDState(pid_integral=5.0)
        assert pid_loop_state({_LOOP: loop}, _LOOP) is loop

    def test_without_any_entry_starts_empty(self):
        """A TRV with nothing stored starts from a fresh state."""
        assert pid_loop_state({}, _LOOP) == PIDState()

    def test_continues_from_the_bucket_that_ran_last(self):
        """A store with bucket entries only continues from the latest one.

        Its integral and output carry over, so the first cycle does not
        restart the integrator; its learned gains stay on the bucket.
        """
        states = {
            f"{_LOOP}:t20.0": PIDState(pid_integral=10.0, pid_last_time=100.0),
            f"{_LOOP}:t21.0": PIDState(
                pid_integral=40.0, last_percent=55.0, pid_last_time=900.0, pid_kp=80.0
            ),
            "uid:climate.other:t21.0": PIDState(pid_integral=70.0, pid_last_time=1e6),
        }

        loop = pid_loop_state(states, _LOOP)

        assert loop.pid_integral == 40.0
        assert loop.last_percent == 55.0
        assert loop.pid_kp is None
        assert loop.auto_tune is None
        assert _LOOP not in states

    def test_adopts_auto_tune_on_and_leaves_the_learned_gains(self):
        """With auto-tuning on the learned gains stay on their buckets."""
        states = {
            f"{_LOOP}:t21.0": PIDState(
                auto_tune=True, pid_kp=80.0, pid_integral=12.0, pid_last_time=900.0
            )
        }

        loop = pid_loop_state(states, _LOOP)

        assert loop.auto_tune is True
        assert loop.pid_kp is None
        assert loop.pid_integral == 12.0

    def test_adopts_auto_tune_off_and_its_gains(self):
        """The switch flag on the buckets moves to the loop entry.

        With auto-tuning off the gains of a bucket that holds the flag
        become the gains set by hand, so every bucket runs with them. A
        bucket created after the switch went off ran with the defaults and
        is not where they come from; the integral still continues from it.
        """
        states = {
            f"{_LOOP}:t21.0": PIDState(
                auto_tune=False,
                pid_kp=150.0,
                pid_ki=0.02,
                pid_kd=900.0,
                pid_last_time=900.0,
            ),
            f"{_LOOP}:t19.0": PIDState(
                pid_integral=33.0, pid_last_time=950.0, pid_kp=60.0
            ),
        }

        loop = pid_loop_state(states, _LOOP)

        assert loop.auto_tune is False
        assert (loop.pid_kp, loop.pid_ki, loop.pid_kd) == (150.0, 0.02, 900.0)
        assert loop.pid_integral == 33.0
        assert pid_auto_tune(loop, PIDState()) is False
        assert effective_pid_gain(loop, states[f"{_LOOP}:t19.0"], "kp") == 150.0

    def test_continues_from_the_current_target_over_a_later_stamp(self):
        """The bucket of the current target is where the controller ran last.

        The stamps come from the monotonic clock, which a host reboot
        restarts: a bucket last run before a reboot can carry a larger
        stamp than the one the controller ran at afterwards.
        """
        states = {
            f"{_LOOP}:t19.0": PIDState(pid_integral=10.0, pid_last_time=900_000.0),
            f"{_LOOP}:t21.0": PIDState(pid_integral=40.0, pid_last_time=500.0),
        }

        loop = pid_loop_state(states, _LOOP, f"{_LOOP}:t21.0")

        assert loop.pid_integral == 40.0

    def test_takes_the_hand_set_gains_from_the_current_target(self):
        """With auto-tuning off the gains come from the current target too."""
        states = {
            f"{_LOOP}:t19.0": PIDState(
                auto_tune=False, pid_kp=90.0, pid_last_time=900_000.0
            ),
            f"{_LOOP}:t21.0": PIDState(
                auto_tune=False, pid_kp=150.0, pid_last_time=500.0
            ),
        }

        loop = pid_loop_state(states, _LOOP, f"{_LOOP}:t21.0")

        assert loop.pid_kp == 150.0

    def test_a_current_target_that_never_ran_falls_back_to_the_stamps(self):
        """A bucket without a cycle carries no state to continue from."""
        states = {
            f"{_LOOP}:t19.0": PIDState(pid_integral=10.0, pid_last_time=900.0),
            f"{_LOOP}:t21.0": PIDState(pid_kp=70.0),
        }

        loop = pid_loop_state(states, _LOOP, f"{_LOOP}:t21.0")

        assert loop.pid_integral == 10.0


class TestFreezeGains:
    """Turning auto-tuning off keeps the gains in use at the current target."""

    def test_the_learned_gain_beats_an_older_hand_set_one(self):
        """A hand-set start value does not return once auto-tuning stops."""
        loop = PIDState(auto_tune=True, pid_kp=100.0)
        bucket = PIDState(pid_kp=72.0)

        freeze_pid_gains(loop, bucket)

        assert loop.auto_tune is False
        assert (loop.pid_kp, loop.pid_ki, loop.pid_kd) == (
            72.0,
            DEFAULT_PID_KI,
            DEFAULT_PID_KD,
        )
        assert effective_pid_gain(loop, bucket, "kp") == 72.0
        assert effective_pid_gain(loop, PIDState(pid_kp=45.0), "kp") == 72.0
        assert effective_pid_gain(loop, None, "kp") == 72.0

    def test_with_auto_tune_already_off_nothing_moves(self):
        """A second turn-off keeps the gains set by hand."""
        loop = PIDState(auto_tune=False, pid_kp=150.0)

        freeze_pid_gains(loop, PIDState(pid_kp=72.0))

        assert loop.pid_kp == 150.0
        assert loop.pid_ki is None


class TestEffectiveGains:
    """The gain in use depends on auto-tuning and where a value is set."""

    def test_defaults_without_any_value(self):
        """Nothing stored: the defaults apply."""
        assert effective_pid_gain(None, None, "kp") == DEFAULT_PID_KP
        assert pid_auto_tune(None, None) is DEFAULT_PID_AUTO_TUNE

    def test_auto_tune_off_puts_the_hand_set_gain_first(self):
        """With auto-tuning off a hand-set gain beats a learned one."""
        loop = PIDState(auto_tune=False, pid_kp=150.0)
        assert effective_pid_gain(loop, PIDState(pid_kp=45.0), "kp") == 150.0
        assert effective_pid_gain(loop, PIDState(), "ki") == 0.01

    def test_auto_tune_on_puts_the_learned_gain_first(self):
        """With auto-tuning on a learned gain wins; a hand-set gain seeds."""
        loop = PIDState(auto_tune=True, pid_kp=150.0)
        assert effective_pid_gain(loop, PIDState(pid_kp=45.0), "kp") == 45.0
        assert effective_pid_gain(loop, PIDState(), "kp") == 150.0

    def test_the_loop_flag_beats_the_bucket_flag(self):
        """A bucket's own flag counts only while the loop entry has none."""
        assert pid_auto_tune(PIDState(auto_tune=True), PIDState(auto_tune=False))
        assert not pid_auto_tune(PIDState(), PIDState(auto_tune=False))


class TestCycleSplit:
    """A cycle runs on the loop entry and splits back into both entries."""

    def test_tuned_gains_go_to_the_bucket_and_the_loop_keeps_its_own(self):
        """Auto-tuned gains land on the bucket, the rest on the loop entry."""
        loop = PIDState(pid_integral=20.0, pid_kp=150.0, auto_tune=True)
        bucket = PIDState()
        params = pid_cycle_params(loop, bucket)
        cycle = pid_cycle_state(loop, params, 21.0)
        cycle.pid_integral = 25.0
        cycle.pid_kp = 135.0

        new_loop = settle_pid_cycle(loop, bucket, cycle, params)

        assert bucket.pid_kp == 135.0
        assert new_loop.pid_kp == 150.0
        assert new_loop.pid_integral == 25.0
        assert new_loop.auto_tune is True

    def test_a_new_target_bucket_starts_without_the_old_errors(self):
        """Errors measured against the previous target do not carry over.

        Auto-tuning would read the drop from a large error at the old
        target to a small one at the new target as an overshoot.
        """
        loop = PIDState(
            last_target_temperature=22.0,
            last_abs_error=1.0,
            previous_abs_error=1.0,
            last_delta_sign=1,
            last_tune_ts=500.0,
            pid_integral=30.0,
        )

        cycle = pid_cycle_state(loop, PIDParams(), 21.0)

        assert cycle.last_abs_error is None
        assert cycle.previous_abs_error is None
        assert cycle.last_delta_sign is None
        assert cycle.last_tune_ts == 500.0
        assert cycle.pid_integral == 30.0
        assert loop.last_abs_error == 1.0

    def test_a_target_in_the_same_bucket_keeps_the_errors(self):
        """Within one bucket the errors stay for auto-tuning to compare."""
        loop = PIDState(last_target_temperature=21.1, last_abs_error=0.4)

        assert pid_cycle_state(loop, PIDParams(), 20.9).last_abs_error == 0.4

    def test_without_auto_tune_the_bucket_is_left_alone(self):
        """Auto-tuning off: the learned gains on the bucket stay unchanged."""
        loop = PIDState(auto_tune=False, pid_kp=150.0)
        bucket = PIDState(pid_kp=45.0)
        params = pid_cycle_params(loop, bucket)
        assert params.kp == 150.0

        settle_pid_cycle(loop, bucket, pid_cycle_state(loop, params, 21.0), params)

        assert bucket.pid_kp == 45.0
