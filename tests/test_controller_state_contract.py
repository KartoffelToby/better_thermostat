"""Tests for the controller state-threading contract.

``compute_pid``/``compute_tpi``/``compute_mpc`` accept an explicit ``state``
argument, mutate it in place and return it as the updated state.
``compute_pid`` requires the explicit state (the StateManager owns it);
``compute_tpi``/``compute_mpc`` still fall back to a module-level dict
(``_TPI_STATES``/``_MPC_STATES``) when no state is passed, and passing an
explicit state additionally stores it in that dict. These tests pin the
explicit-state path everywhere and the module-global path where it remains.
"""

from collections.abc import Iterator

import pytest

import custom_components.better_thermostat.utils.calibration.mpc as mpc_module
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
import custom_components.better_thermostat.utils.calibration.tpi as tpi_module
from custom_components.better_thermostat.utils.calibration.tpi import (
    TpiInput,
    TpiParams,
    TpiState,
    compute_tpi,
)


@pytest.fixture(autouse=True)
def _reset_globals() -> Iterator[None]:
    """Clear the remaining controller globals before and after each test."""
    tpi_module._TPI_STATES.clear()
    mpc_module._MPC_STATES.clear()
    yield
    tpi_module._TPI_STATES.clear()
    mpc_module._MPC_STATES.clear()


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
    """State-threading contract of ``compute_tpi``."""

    @staticmethod
    def _inp(key: str) -> TpiInput:
        """Return a non-blocked input that triggers a real computation."""
        return TpiInput(key=key, current_temp_C=20.0, target_temp_C=22.0)

    def test_explicit_state_is_returned_and_accumulates(self) -> None:
        """An explicit state is returned as the same object and keeps accumulating."""
        state = TpiState()
        _, st1 = compute_tpi(self._inp("k"), TpiParams(), state=state)
        assert st1 is state
        assert state.last_percent is not None

        tpi_module._TPI_STATES.clear()
        _, st2 = compute_tpi(self._inp("k"), TpiParams(), state=st1)
        assert st2 is state

    def test_missing_state_uses_module_global(self) -> None:
        """Without an explicit state the call uses ``_TPI_STATES``."""
        _, st = compute_tpi(self._inp("kg"), TpiParams())
        assert tpi_module._TPI_STATES["kg"] is st

    def test_explicit_state_is_also_stored_in_global(self) -> None:
        """An explicit state is also written to ``_TPI_STATES``."""
        state = TpiState()
        compute_tpi(self._inp("kl"), TpiParams(), state=state)
        assert tpi_module._TPI_STATES["kl"] is state


class TestMpcStateContract:
    """State-threading contract of ``compute_mpc``."""

    @staticmethod
    def _inp(key: str) -> MpcInput:
        """Return an input with a small error that triggers a regular computation."""
        return MpcInput(
            key=key, target_temp_C=22.0, current_temp_C=21.5, temp_slope_K_per_min=0.0
        )

    def test_explicit_state_is_returned_and_accumulates(self) -> None:
        """An explicit state is returned as the same object and keeps accumulating."""
        params = MpcParams(mpc_adapt=True)
        state = MpcState()
        _, st1 = compute_mpc(
            self._inp("k"), params, state=state, all_states={"k": state}
        )
        assert st1 is state
        assert state.last_integration_ts > 0.0

        mpc_module._MPC_STATES.clear()
        _, st2 = compute_mpc(self._inp("k"), params, state=st1, all_states={"k": st1})
        assert st2 is state

    def test_missing_state_uses_module_global(self) -> None:
        """Without an explicit state the call uses ``_MPC_STATES``."""
        _, st = compute_mpc(self._inp("kg"), MpcParams())
        assert mpc_module._MPC_STATES["kg"] is st

    def test_explicit_state_is_also_stored_in_global(self) -> None:
        """An explicit state is also written to ``_MPC_STATES``."""
        state = MpcState()
        compute_mpc(self._inp("kl"), MpcParams(), state=state)
        assert mpc_module._MPC_STATES["kl"] is state

    def test_sibling_seeding_uses_global_when_all_states_missing(self) -> None:
        """With ``all_states=None`` sibling seeding reads from ``_MPC_STATES``."""
        _, st_a = compute_mpc(self._inp("bucket_a"), MpcParams())
        assert mpc_module._MPC_STATES["bucket_a"] is st_a
        _, st_b = compute_mpc(self._inp("bucket_b"), MpcParams())
        assert mpc_module._MPC_STATES["bucket_b"] is st_b
        assert set(mpc_module._MPC_STATES) >= {"bucket_a", "bucket_b"}
