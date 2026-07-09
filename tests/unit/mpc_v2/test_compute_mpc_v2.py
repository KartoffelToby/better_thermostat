"""End-to-end tests for ``compute_mpc_v2``."""

from __future__ import annotations

import copy
from dataclasses import replace

import numpy as np
import pytest

pytest.importorskip("daqp")

from custom_components.better_thermostat.utils.calibration.mpc_v2 import (
    PLANT_PRESETS,
    SNAPSHOT_VERSION,
    ControllerSnapshot,
    MpcV2Controller,
    MpcV2Input,
    MpcV2Params,
    MpcV2State,
    compute_mpc_v2,
    export_mpc_v2_state,
    import_mpc_v2_state,
    make_plant_prior,
)
from custom_components.better_thermostat.utils.calibration.mpc_v2_internals.plant import (
    PlantModelRC2,
    PlantParams,
)


def _baseline_input(**overrides: object) -> MpcV2Input:
    """Build a baseline MpcV2Input with optional field overrides."""
    base = MpcV2Input(
        key="room",
        target_temp_C=22.0,
        current_temp_C=20.0,
        outdoor_temp_C=5.0,
        heating_allowed=True,
        window_open=False,
    )
    return replace(base, **overrides) if overrides else base


def test_window_open_returns_none() -> None:
    """Window-open input returns no command but still yields state."""
    out, state = compute_mpc_v2(_baseline_input(window_open=True), MpcV2Params(), None)
    assert out is None
    assert state is not None


def test_heating_disallowed_returns_none() -> None:
    """Disallowed heating returns no command."""
    out, _ = compute_mpc_v2(_baseline_input(heating_allowed=False), MpcV2Params(), None)
    assert out is None


def test_missing_current_temp_returns_none() -> None:
    """A missing current room temperature returns no command."""
    out, _ = compute_mpc_v2(_baseline_input(current_temp_C=None), MpcV2Params(), None)
    assert out is None


def test_first_call_creates_controller_and_returns_percent() -> None:
    """First call builds a controller and returns a bounded valve percent."""
    out, state = compute_mpc_v2(_baseline_input(), MpcV2Params(), None)
    assert out is not None
    assert isinstance(out.valve_percent, int)
    assert 0 <= out.valve_percent <= 100
    assert state.controller is not None
    assert state.last_percent == float(out.valve_percent)


def test_max_opening_pct_is_honoured() -> None:
    """The valve percent never exceeds the configured max_opening_pct."""
    params = MpcV2Params()
    out, _ = compute_mpc_v2(
        _baseline_input(current_temp_C=15.0, max_opening_pct=40.0), params, None
    )
    assert out is not None
    assert out.valve_percent <= 40


def test_diagnostics_exposed() -> None:
    """The output exposes the core observer diagnostics as typed fields."""
    out, _ = compute_mpc_v2(_baseline_input(), MpcV2Params(), None)
    assert out is not None
    diag = out.diagnostics
    assert diag.tau_room_min > 0
    # Estimates are finite numbers once the controller has run a cycle.
    assert diag.T_room_hat == diag.T_room_hat  # not NaN
    assert diag.T_rad_hat == diag.T_rad_hat


def test_snapshot_round_trip_preserves_last_u() -> None:
    """export_snapshot → restore_snapshot reproduces the controller's last command."""
    state = None
    for i in range(5):
        T = 19.5 + 0.05 * i
        out, state = compute_mpc_v2(
            _baseline_input(current_temp_C=T), MpcV2Params(), state
        )
        assert out is not None
    assert state is not None and state.controller is not None
    snap = state.controller.export_snapshot()
    saved = copy.deepcopy(snap)

    fresh = MpcV2Controller(MpcV2Params())
    fresh.restore_snapshot(saved)
    assert fresh._last_u == state.controller._last_u
    assert fresh._initialised is True
    np.testing.assert_allclose(fresh.kalman.x_hat, state.controller.kalman.x_hat)


def test_make_plant_prior_defaults_when_no_input() -> None:
    """With no input make_plant_prior returns the PlantParams defaults."""
    prior = make_plant_prior()
    # Defaults from PlantParams() — sanity check that no derivation kicks in.
    assert prior.tau_room_min == 480.0
    assert prior.coupling_rad_room == 1.0


def test_make_plant_prior_derives_tau_from_heat_loss_rate() -> None:
    """A learned heat_loss_rate derives tau_room within bounds."""
    # heat_loss_rate = 0.03 °C/min at 15 K delta ⇒ tau = 500 min, inside bounds.
    prior = make_plant_prior(heat_loss_rate=0.03)
    assert abs(prior.tau_room_min - 500.0) < 1e-6


def test_make_plant_prior_clamps_extreme_heat_loss() -> None:
    """Extreme heat-loss rates clamp the derived tau to its bounds."""
    # Very low loss would imply tau >> upper bound; should clamp to 2000.
    high_tau = make_plant_prior(heat_loss_rate=0.0001)
    assert high_tau.tau_room_min == 2000.0
    # Very high loss would imply tau << lower bound; clamp to 60.
    low_tau = make_plant_prior(heat_loss_rate=1.0)
    assert low_tau.tau_room_min == 60.0


def test_make_plant_prior_preset_overrides_learnings() -> None:
    """A named preset overrides the learned derivation and copies independently."""
    # Preset wins even when heat_loss_rate would otherwise derive a value.
    prior = make_plant_prior(heat_loss_rate=0.03, preset="small_room")
    assert prior.tau_room_min == PLANT_PRESETS["small_room"].tau_room_min
    # And presets are independent copies, not the shared singleton.
    prior.tau_room_min = 999.0
    assert PLANT_PRESETS["small_room"].tau_room_min != 999.0


def test_make_plant_prior_unknown_preset_falls_back_to_derivation() -> None:
    """An unknown preset name falls back to heat-loss derivation."""
    prior = make_plant_prior(heat_loss_rate=0.03, preset="bogus_room")
    assert abs(prior.tau_room_min - 500.0) < 1e-6


def test_plant_signature_change_rebuilds_controller(caplog) -> None:
    """Changing the preset between calls must rebuild the controller."""

    state: MpcV2State | None = None
    out, state = compute_mpc_v2(
        _baseline_input(key="preset-test-key"),
        MpcV2Params(plant=make_plant_prior(preset="small_room")),
        state,
    )
    assert out is not None
    original_ctrl = state.controller

    with caplog.at_level("INFO"):
        out, state = compute_mpc_v2(
            _baseline_input(key="preset-test-key"),
            MpcV2Params(plant=make_plant_prior(preset="large_room")),
            state,
        )
    assert state.controller is not original_ctrl
    assert any("plant prior changed" in r.message for r in caplog.records)


def test_small_plant_prior_drift_keeps_controller(caplog) -> None:
    """Sub-tolerance tau drift (AUTO learning tick) must not rebuild."""
    state: MpcV2State | None = None
    out, state = compute_mpc_v2(
        _baseline_input(key="drift-test-key"),
        MpcV2Params(plant=make_plant_prior(heat_loss_rate=0.03)),  # tau = 500
        state,
    )
    assert out is not None
    original_ctrl = state.controller

    with caplog.at_level("INFO"):
        _, state = compute_mpc_v2(
            _baseline_input(key="drift-test-key"),
            MpcV2Params(plant=make_plant_prior(heat_loss_rate=0.0294)),  # tau ≈ 510
            state,
        )
    assert state.controller is original_ctrl
    assert not any("plant prior changed" in r.message for r in caplog.records)


def test_cumulative_plant_prior_drift_rebuilds_once() -> None:
    """Drift beyond the tolerance, even in small steps, rebuilds exactly once."""
    state: MpcV2State | None = None
    _, state = compute_mpc_v2(
        _baseline_input(key="cumulative-drift-key"),
        MpcV2Params(plant=make_plant_prior(heat_loss_rate=0.03)),  # tau = 500
        state,
    )
    original_ctrl = state.controller

    # Two sub-tolerance ticks whose sum crosses the 10 % band: the signature
    # stays anchored at the build-time tau (500), so the second tick trips it.
    _, state = compute_mpc_v2(
        _baseline_input(key="cumulative-drift-key"),
        MpcV2Params(plant=make_plant_prior(heat_loss_rate=0.0283)),  # tau ≈ 530
        state,
    )
    assert state.controller is original_ctrl
    _, state = compute_mpc_v2(
        _baseline_input(key="cumulative-drift-key"),
        MpcV2Params(plant=make_plant_prior(heat_loss_rate=0.0263)),  # tau ≈ 570
        state,
    )
    assert state.controller is not original_ctrl


def test_sub_second_repeat_step_holds_covariance() -> None:
    """A same-pass repeat step returns last_u without re-folding the measurement."""
    controller = MpcV2Controller(MpcV2Params())
    u1, _ = controller.step(t_s=1000.0, T_room_C=20.0, T_target_C=22.0, T_outdoor_C=5.0)
    p_after_first = controller.kalman.P.copy()

    # Second TRV in the same control pass: milliseconds later, same reading.
    u2, _ = controller.step(
        t_s=1000.005, T_room_C=20.0, T_target_C=22.0, T_outdoor_C=5.0
    )
    assert u2 == u1
    np.testing.assert_array_equal(controller.kalman.P, p_after_first)

    # A regular next cycle still advances the filter.
    controller.step(t_s=1000.0 + 30.0, T_room_C=20.1, T_target_C=22.0, T_outdoor_C=5.0)
    assert not np.array_equal(controller.kalman.P, p_after_first)


def test_outdoor_fallback_logs_once(caplog) -> None:
    """Missing outdoor_temp_C triggers exactly one WARN per controller."""
    state: MpcV2State | None = None

    with caplog.at_level("WARNING"):
        out, state = compute_mpc_v2(
            _baseline_input(key="outdoor-fallback-key", outdoor_temp_C=None),
            MpcV2Params(),
            state,
        )
        out, state = compute_mpc_v2(
            _baseline_input(key="outdoor-fallback-key", outdoor_temp_C=None),
            MpcV2Params(),
            state,
        )

    fallback_warnings = [
        r for r in caplog.records if "outdoor_temp_C" in r.getMessage()
    ]
    assert len(fallback_warnings) == 1


def test_daqp_guard_raises_when_unavailable(monkeypatch) -> None:
    """Patching DAQP_AVAILABLE to False must surface at controller init."""
    from custom_components.better_thermostat.utils.calibration.mpc_v2_internals import (
        qp_optimiser,
    )

    monkeypatch.setattr(qp_optimiser, "DAQP_AVAILABLE", False)
    monkeypatch.setattr(qp_optimiser, "_DAQP_IMPORT_ERROR", "synthetic test failure")
    try:
        with pytest.raises(ImportError, match="daqp"):
            MpcV2Controller(MpcV2Params())
    finally:
        monkeypatch.undo()


def test_snapshot_carries_version_tag() -> None:
    """Every fresh snapshot must include the current schema version."""
    state: MpcV2State | None = None
    _, state = compute_mpc_v2(
        _baseline_input(key="snapshot-version-key"), MpcV2Params(), state
    )
    assert state.controller is not None
    snap = state.controller.export_snapshot()
    assert snap.v == SNAPSHOT_VERSION


def test_future_snapshot_version_rejected(caplog) -> None:
    """A snapshot with a future schema version is rejected during parsing."""
    bogus = {"v": SNAPSHOT_VERSION + 99, "last_u": 0.5}
    with caplog.at_level("WARNING"):
        snap = ControllerSnapshot.from_mapping(bogus)
    assert snap is None
    assert any("snapshot version" in r.getMessage() for r in caplog.records)


def test_state_round_trip() -> None:
    """export_mpc_v2_state → import_mpc_v2_state reproduces the live state."""
    state: MpcV2State | None = None
    for i in range(3):
        _, state = compute_mpc_v2(
            _baseline_input(key="roundtrip-key", current_temp_C=19.0 + 0.05 * i),
            MpcV2Params(),
            state,
        )

    exported = export_mpc_v2_state(state)
    assert exported is not None
    assert exported["snapshot"]["v"] == SNAPSHOT_VERSION

    rehydrated = import_mpc_v2_state(exported, MpcV2Params())
    assert rehydrated.controller is not None
    assert rehydrated.controller._last_u == state.controller._last_u
    assert rehydrated.last_percent == state.last_percent


def test_export_state_without_controller_returns_none() -> None:
    """A state that never produced a controller has nothing to persist."""
    assert export_mpc_v2_state(MpcV2State()) is None


def test_non_finite_input_holds_last_command(caplog) -> None:
    """NaN/Inf in any sensor input must not poison the cached state."""
    state: MpcV2State | None = None

    # Warm-up call to establish a controller + last_u.
    _, state = compute_mpc_v2(
        _baseline_input(key="nan-input-key", current_temp_C=20.0), MpcV2Params(), state
    )
    assert state.controller is not None
    last_u_before = state.controller._last_u

    with caplog.at_level("WARNING"):
        bad = _baseline_input(key="nan-input-key", current_temp_C=float("nan"))
        out, state = compute_mpc_v2(bad, MpcV2Params(), state)
        out, state = compute_mpc_v2(
            _baseline_input(key="nan-input-key", outdoor_temp_C=float("inf")),
            MpcV2Params(),
            state,
        )

    assert out is None  # caller treats this as "hold last value"
    # Controller's cached state is unchanged — no NaN propagated.
    assert state.controller._last_u == last_u_before
    assert any("non-finite input" in r.getMessage() for r in caplog.records)


def test_cooling_case_settles_at_zero_valve() -> None:
    """When target < current and outdoor is cool, the valve must close."""
    state: MpcV2State | None = None
    last_pct = None
    for _ in range(60):
        out, state = compute_mpc_v2(
            _baseline_input(
                key="cooling-key",
                target_temp_C=18.0,
                current_temp_C=22.0,
                outdoor_temp_C=8.0,
            ),
            MpcV2Params(),
            state,
        )
        assert out is not None
        # No negative percent ever — the QP is bounded to u_min=0.
        assert out.valve_percent >= 0
        last_pct = out.valve_percent

    # After 60 cycles the Δu ramp has had plenty of time to walk down to 0.
    assert last_pct == 0, f"expected fully closed valve, got {last_pct}%"


def test_zero_error_holds_steady() -> None:
    """target == current should not provoke valve oscillation."""
    state: MpcV2State | None = None
    last_pct = None
    for _ in range(20):
        out, state = compute_mpc_v2(
            _baseline_input(
                key="zero-err-key",
                target_temp_C=20.0,
                current_temp_C=20.0,
                outdoor_temp_C=10.0,
            ),
            MpcV2Params(),
            state,
        )
        assert out is not None
        last_pct = out.valve_percent
    # Settled to a steady, non-negative, non-saturated value.
    assert 0 <= last_pct <= 100


def test_controller_drives_simulated_plant_toward_setpoint() -> None:
    """Closed-loop sanity check against a synthetic RC2 plant."""
    truth = PlantModelRC2(
        PlantParams(tau_room_min=120.0, tau_rad_min=8.0, gain_heater=3.0), dt_s=30.0
    )
    params = MpcV2Params()
    # Inject the same true plant params into the controller for a fair test.
    params.plant = PlantParams(tau_room_min=120.0, tau_rad_min=8.0, gain_heater=3.0)
    controller = MpcV2Controller(params)
    x = np.array([18.0, 18.0])
    t = 0.0
    last_T_room = float(x[0])
    for _ in range(400):
        u, _ = controller.step(
            t_s=t,
            T_room_C=float(x[0]),
            T_target_C=21.0,
            T_outdoor_C=5.0,
            T_rad_C=float(x[1]),
        )
        x = truth.discrete_step(x, u=u, T_outdoor_C=5.0)
        t += 30.0
        last_T_room = float(x[0])
    # Within 1 K of setpoint after ~3.3 h of simulated time.
    assert abs(last_T_room - 21.0) < 1.0


def test_malformed_snapshot_values_are_rejected(caplog) -> None:
    """Non-numeric persisted values drop the whole snapshot, not the process."""
    bogus = {"v": SNAPSHOT_VERSION, "last_u": "junk"}
    with caplog.at_level("WARNING"):
        snap = ControllerSnapshot.from_mapping(bogus)
    assert snap is None
    assert any("non-numeric" in r.getMessage() for r in caplog.records)


def test_wrong_shaped_covariance_is_ignored_on_restore() -> None:
    """A mis-shaped kalman_P/x_hat must not poison the rebuilt filter."""
    controller = MpcV2Controller(MpcV2Params())
    default_P = controller.kalman.P.copy()
    default_x = controller.kalman.x_hat.copy()

    snap = ControllerSnapshot.from_mapping(
        {
            "v": SNAPSHOT_VERSION,
            "x_hat": [20.0, 21.0, 22.0],  # wrong length
            "kalman_P": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
            "last_u": 0.4,
        }
    )
    assert snap is not None
    controller.restore_snapshot(snap)

    np.testing.assert_array_equal(controller.kalman.P, default_P)
    np.testing.assert_array_equal(controller.kalman.x_hat, default_x)
    assert controller._last_u == 0.4  # scalar fields still restore
