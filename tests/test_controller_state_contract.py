"""Tests for the controller state-threading contract.

``compute_pid``/``compute_tpi``/``compute_mpc`` require an explicit ``state``
argument, mutate it in place and return it as the updated state; the
StateManager owns all controller state and there are no module-level state
dicts left. These tests pin the explicit-state contract.
"""

from custom_components.better_thermostat.utils.calibration.mpc import (
    MpcInput,
    MpcParams,
    MpcState,
    compute_mpc,
)
from custom_components.better_thermostat.utils.calibration.pid import (
    PIDParams,
    PIDState,
    compute_pid,
)
from custom_components.better_thermostat.utils.calibration.tpi import (
    TpiInput,
    TpiParams,
    TpiState,
    compute_tpi,
)


def _pid_call(state: PIDState, key: str = "k") -> PIDState:
    """Call ``compute_pid`` with fixed inputs (error = 2.0 K)."""
    params = PIDParams(auto_tune=False, min_hold_time_s=0.0)
    _, _, out = compute_pid(
        params=params,
        inp_target_temp_C=22.0,
        inp_current_temp_C=20.0,
        inp_trv_temp_C=21.0,
        inp_temp_slope_K_per_min=0.0,
        key=key,
        state=state,
    )
    return out


class TestPidStateContract:
    """State-threading contract of ``compute_pid`` (explicit state only)."""

    def test_explicit_state_is_returned_and_accumulates(self) -> None:
        """The explicit state is returned as the same object and keeps accumulating."""
        state = PIDState()
        out1 = _pid_call(state)
        assert out1 is state
        assert state.last_abs_error == 2.0

        out2 = _pid_call(out1)
        assert out2 is state
        assert state.previous_abs_error == 2.0


class TestTpiStateContract:
    """State-threading contract of ``compute_tpi`` (explicit state only)."""

    @staticmethod
    def _inp(key: str) -> TpiInput:
        """Return a non-blocked input that triggers a real computation."""
        return TpiInput(key=key, current_temp_C=20.0, target_temp_C=22.0)

    def test_explicit_state_is_returned_and_accumulates(self) -> None:
        """The explicit state is returned as the same object and keeps accumulating."""
        state = TpiState()
        _, st1 = compute_tpi(self._inp("k"), TpiParams(), state=state)
        assert st1 is state
        assert state.last_percent is not None

        _, st2 = compute_tpi(self._inp("k"), TpiParams(), state=st1)
        assert st2 is state


class TestMpcStateContract:
    """State-threading contract of ``compute_mpc`` (explicit state only)."""

    @staticmethod
    def _inp(key: str) -> MpcInput:
        """Return an input with a small error that triggers a regular computation."""
        return MpcInput(
            key=key, target_temp_C=22.0, current_temp_C=21.5, temp_slope_K_per_min=0.0
        )

    def test_explicit_state_is_returned_and_accumulates(self) -> None:
        """The explicit state is returned as the same object and keeps accumulating."""
        params = MpcParams(mpc_adapt=True)
        state = MpcState()
        _, st1 = compute_mpc(
            self._inp("k"), params, state=state, all_states={"k": state}
        )
        assert st1 is state
        assert state.last_integration_ts > 0.0

        _, st2 = compute_mpc(self._inp("k"), params, state=st1, all_states={"k": st1})
        assert st2 is state

    def test_sibling_seeding_reads_from_all_states(self) -> None:
        """Sibling seeding copies min_effective_percent from the all_states map."""
        sibling = MpcState(min_effective_percent=18.0)
        all_states = {"uid:climate.trv:t21.0": sibling}
        state = MpcState()
        compute_mpc(
            MpcInput(
                key="uid:climate.trv:t22.0",
                target_temp_C=22.0,
                current_temp_C=21.5,
                temp_slope_K_per_min=0.0,
            ),
            MpcParams(enable_min_effective_percent=True),
            state=state,
            all_states=all_states,
        )
        assert state.min_effective_percent == 18.0


class TestNoModuleStateGlobals:
    """The controller modules must not hold any module-level state dicts."""

    def test_no_state_globals_left(self) -> None:
        """No ``*_STATES`` module global exists in pid/tpi/mpc."""
        from custom_components.better_thermostat.utils.calibration import mpc, pid, tpi

        for module in (pid, tpi, mpc):
            offenders = [name for name in vars(module) if name.endswith("_STATES")]
            assert offenders == [], f"{module.__name__} still has {offenders}"
