"""Live MPC v2 controller caching + save-time persistence in the StateManager.

The controller is held live in memory across cycles; the persisted
``MpcV2StateData`` snapshot is produced only when state is saved.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from custom_components.better_thermostat.utils.calibration.mpc_v2 import (
    MpcV2Input,
    MpcV2Params,
    MpcV2State,
    compute_mpc_v2,
)
from custom_components.better_thermostat.utils.state_manager import StateManager


def _make_manager() -> StateManager:
    """Build a StateManager with a mocked HA Store."""
    mock_hass = AsyncMock()
    with patch("custom_components.better_thermostat.utils.state_manager.Store"):
        return StateManager(mock_hass, "test_entry")


def _warm(state: MpcV2State) -> MpcV2State:
    """Run one compute cycle so the state holds a live controller."""
    _out, state = compute_mpc_v2(
        MpcV2Input(
            key="k",
            target_temp_C=22.0,
            current_temp_C=19.0,
            outdoor_temp_C=5.0,
            heating_allowed=True,
            window_open=False,
        ),
        MpcV2Params(),
        state=state,
        now=0.0,
    )
    return state


def test_get_mpc_v2_live_caches_same_instance() -> None:
    """Repeated get returns the same live instance (no per-cycle rebuild)."""
    mgr = _make_manager()
    first = mgr.get_mpc_v2_live("k", MpcV2Params())
    second = mgr.get_mpc_v2_live("k", MpcV2Params())
    assert isinstance(first, MpcV2State)
    assert first is second


def test_set_mpc_v2_live_marks_dirty() -> None:
    """Storing live state marks the manager dirty for the next save."""
    mgr = _make_manager()
    mgr.set_mpc_v2_live("k", MpcV2State())
    assert mgr.dirty is True


def test_sync_folds_live_controller_into_snapshot() -> None:
    """The save-time fold serialises the live controller into mpc_v2 snapshot."""
    mgr = _make_manager()
    live = _warm(mgr.get_mpc_v2_live("k", MpcV2Params()))
    mgr.set_mpc_v2_live("k", live)
    assert "k" not in mgr.state.mpc_v2  # nothing persisted per cycle
    mgr._sync_mpc_v2_live()
    assert mgr.state.mpc_v2["k"].snapshot  # controller state captured at save


def test_rehydrates_live_controller_from_persisted_snapshot() -> None:
    """A fresh manager loaded with a persisted snapshot rebuilds a controller."""
    seed = _make_manager()
    seed.set_mpc_v2_live("k", _warm(seed.get_mpc_v2_live("k", MpcV2Params())))
    seed._sync_mpc_v2_live()
    persisted = seed.state.mpc_v2["k"]

    loaded = _make_manager()
    loaded.state.mpc_v2["k"] = persisted
    rebuilt = loaded.get_mpc_v2_live("k", MpcV2Params())
    assert rebuilt.controller is not None
