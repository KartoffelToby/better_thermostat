"""Live MPC v2 controller caching + save-time persistence in the StateManager.

The controller is held live in memory across cycles; the persisted
``MpcV2StateData`` snapshot is produced only when state is saved.
"""

from __future__ import annotations

import copy
import json
from unittest.mock import AsyncMock, patch

from custom_components.better_thermostat.utils.calibration.mpc_v2 import (
    SNAPSHOT_VERSION,
    MpcV2Input,
    MpcV2Params,
    MpcV2State,
    compute_mpc_v2,
)
from custom_components.better_thermostat.utils.state_manager import (
    StateManager,
    _deserialize,
    _serialize,
)


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
            target_temperature=22.0,
            room_temperature=19.0,
            outdoor_temperature=5.0,
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


def test_get_mpc_v2_live_rebuilds_from_the_persisted_entry() -> None:
    """A persisted entry seeds the live state without being changed by it."""
    mgr = _make_manager()
    warm = _warm(mgr.get_mpc_v2_live("k", MpcV2Params()))
    mgr._sync_mpc_v2_live()
    persisted = mgr.state.mpc_v2["k"]
    before = copy.deepcopy(persisted)
    mgr._mpc_v2_live.clear()

    live = mgr.get_mpc_v2_live("k", MpcV2Params())

    assert live is not warm
    assert live.controller is not None
    assert live.last_percent == persisted.last_percent
    assert live.created_ts == persisted.created_ts
    assert live.controller.export_snapshot().to_mapping() == persisted.snapshot
    assert persisted == before


def test_an_unreadable_snapshot_version_stays_stored_as_it_was() -> None:
    """A snapshot from a later release starts the controller fresh in memory.

    The stored entry keeps that snapshot, unknown keys and all, so a store
    read by this release and saved again before any controller ran still
    carries it for the release that wrote it.
    """
    future = {"v": SNAPSHOT_VERSION + 1, "x_hat": [20.0, 21.0], "future": [1, 2]}
    stored = {"version": 1, "mpc_v2": {"k": {"created_ts": 5.0, "snapshot": future}}}
    state = _deserialize(json.loads(json.dumps(stored)))
    mgr = _make_manager()
    mgr._state = state

    live = mgr.get_mpc_v2_live("k", MpcV2Params())

    assert live.controller is not None
    assert live.controller._initialised is False
    assert mgr.state.mpc_v2["k"].snapshot == future
    assert _serialize(mgr.state)["mpc_v2"]["k"]["snapshot"] == future
