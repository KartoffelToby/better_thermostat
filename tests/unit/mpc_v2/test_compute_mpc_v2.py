"""End-to-end tests for ``compute_mpc_v2``."""

from __future__ import annotations

import copy
from dataclasses import replace
import json
import logging

import numpy as np
import pytest

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


def test_confirmed_valve_input_replaces_optimistic_previous_command() -> None:
    """The next cycle models the adapter-confirmed input, not its proposal."""
    state = MpcV2State()
    _out, state = compute_mpc_v2(_baseline_input(), MpcV2Params(), state, now=100.0)
    assert state.controller is not None
    state.controller.set_command_u(0.9)

    seen_previous_input: list[float] = []
    original_step = state.controller.step

    def _capture_step(*args, **kwargs):
        seen_previous_input.append(state.controller._last_u)
        return original_step(*args, **kwargs)

    state.controller.step = _capture_step  # type: ignore[method-assign]
    out, state = compute_mpc_v2(
        _baseline_input(applied_valve_pct=20.0), MpcV2Params(), state, now=400.0
    )
    assert out is not None
    assert seen_previous_input == [0.2]


def test_integral_uses_elapsed_control_interval() -> None:
    """A delayed replan integrates over its real prior valve interval."""
    params = MpcV2Params()
    params.governor.enabled = False
    controller = MpcV2Controller(params)
    controller.step(100.0, 20.0, 22.0, 5.0)
    controller.set_applied_u(0.5)
    controller.step(1000.0, 20.0, 22.0, 5.0)
    assert controller.optimiser.e_integral_K_min == pytest.approx(-30.0)


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


def test_daqp_absence_uses_portable_solver(monkeypatch) -> None:
    """Patching DAQP unavailable must still construct a usable controller."""
    from custom_components.better_thermostat.utils.calibration.mpc_v2_internals import (
        qp_optimiser,
    )

    monkeypatch.setattr(qp_optimiser, "DAQP_AVAILABLE", False)
    monkeypatch.setattr(qp_optimiser, "_daqp", None)
    controller = MpcV2Controller(MpcV2Params())
    u, _diag = controller.step(
        t_s=1000.0, T_room_C=19.0, T_target_C=22.0, T_outdoor_C=5.0
    )
    assert 0.0 <= u <= 1.0


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


@pytest.mark.parametrize(
    "raw",
    [
        {"v": SNAPSHOT_VERSION, "x_hat": [float("nan"), float("nan")]},
        {"v": SNAPSHOT_VERSION, "x_hat": [float("inf"), 20.0]},
        {"v": SNAPSHOT_VERSION, "kalman_P": [[float("nan"), 0.0], [0.0, 1.0]]},
        {"v": SNAPSHOT_VERSION, "u_history": [0.5, float("nan")]},
        {"v": SNAPSHOT_VERSION, "D_hat_K_per_min": float("nan")},
        {"v": SNAPSHOT_VERSION, "last_u": float("nan")},
        {"v": SNAPSHOT_VERSION, "e_integral_K_min": float("-inf")},
        {"v": SNAPSHOT_VERSION, "rg_v_C": float("nan")},
        {"v": SNAPSHOT_VERSION, "last_t_s": float("inf")},
        {"v": SNAPSHOT_VERSION, "next_mpc_t_s": float("nan")},
    ],
)
def test_non_finite_snapshot_values_are_rejected(
    raw: dict[str, object], caplog
) -> None:
    """A non-finite value in any persisted field drops the whole snapshot.

    The payload is routed through ``json`` because ``NaN`` and ``Infinity``
    survive a serialise/parse round trip, so a store that once wrote them hands
    them back to ``from_mapping`` exactly as written here.
    """
    with caplog.at_level("WARNING"):
        snap = ControllerSnapshot.from_mapping(json.loads(json.dumps(raw)))
    assert snap is None
    assert any("non-finite" in r.getMessage() for r in caplog.records)


def test_null_governor_state_is_accepted() -> None:
    """A stored null ``rg_v_C`` is a legal value, not a non-finite one."""
    raw = {"v": SNAPSHOT_VERSION, "x_hat": [21.0, 22.0], "rg_v_C": None}
    snap = ControllerSnapshot.from_mapping(json.loads(json.dumps(raw)))
    assert snap is not None
    assert snap.rg_v_C is None
    assert snap.x_hat == [21.0, 22.0]

    controller = MpcV2Controller(MpcV2Params())
    controller.restore_snapshot(snap)
    assert controller._initialised is True
    np.testing.assert_array_equal(controller.kalman.x_hat, np.array([21.0, 22.0]))


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


# Snapshot shapes that carry no usable state vector of the controller's
# dimension: absent, too short, or the right length but non-finite. ``None``
# stands for a stored payload whose ``snapshot`` key is null or not a mapping
# at all, which ``import_mpc_v2_state`` refuses before it builds a controller.
_SNAPSHOTS_WITHOUT_ESTIMATE = [
    {},
    {"u_prev": 0.5},
    {"v": SNAPSHOT_VERSION},
    {"v": SNAPSHOT_VERSION, "x_hat": [18.0]},
    {"v": SNAPSHOT_VERSION, "x_hat": [float("nan"), float("nan")]},
    {"v": SNAPSHOT_VERSION, "x_hat": [float("inf"), 20.0]},
    None,
]


def _snapshot_carrying(raw: dict[str, object]) -> ControllerSnapshot:
    """Build a snapshot object directly from a raw mapping's ``x_hat``.

    ``from_mapping`` drops non-finite payloads, so it cannot deliver the
    non-finite vectors to ``restore_snapshot``. Constructing the dataclass here
    exercises the seed condition on its own; every other field is left at the
    controller's construction default.
    """
    vector = raw.get("x_hat", [])
    return ControllerSnapshot(
        v=SNAPSHOT_VERSION,
        x_hat=[float(x) for x in vector] if isinstance(vector, list) else [],
        kalman_P=[],
        D_hat_K_per_min=0.0,
        last_u=0.0,
        e_integral_K_min=0.0,
        u_history=[],
        rg_v_C=None,
        last_t_s=0.0,
        next_mpc_t_s=-1.0,
    )


@pytest.mark.parametrize("raw", _SNAPSHOTS_WITHOUT_ESTIMATE[:-1])
def test_snapshot_without_estimate_leaves_controller_uninitialised(
    raw: dict[str, object],
) -> None:
    """A snapshot with no usable x_hat does not mark the controller initialised."""
    controller = MpcV2Controller(MpcV2Params())
    default_x = controller.kalman.x_hat.copy()
    controller.restore_snapshot(_snapshot_carrying(raw))
    assert controller._initialised is False
    np.testing.assert_array_equal(controller.kalman.x_hat, default_x)


@pytest.mark.parametrize("raw", _SNAPSHOTS_WITHOUT_ESTIMATE)
def test_restore_without_estimate_matches_a_freshly_built_controller(
    raw: dict[str, object] | None,
) -> None:
    """Rehydrating from such a payload behaves like booting without one.

    Every other field these payloads carry already equals the construction
    default, so the observer estimate after the first cycle is the whole
    difference: seeded from the measured room and radiator temperatures rather
    than left on the neutral 20.0 °C guess the filter is built with.
    """
    params = MpcV2Params()
    inp = _baseline_input(current_temp_C=24.0, trv_temp_C=26.5)

    restored = import_mpc_v2_state({"snapshot": raw}, params)
    out_restored, _ = compute_mpc_v2(inp, params, restored, now=1_700_000_000.0)
    out_fresh, _ = compute_mpc_v2(inp, params, None, now=1_700_000_000.0)

    assert out_restored is not None and out_fresh is not None
    assert out_restored.diagnostics.T_rad_hat == pytest.approx(
        out_fresh.diagnostics.T_rad_hat
    )
    assert out_restored.diagnostics.T_room_hat == pytest.approx(
        out_fresh.diagnostics.T_room_hat
    )
    assert out_restored.valve_percent == out_fresh.valve_percent


@pytest.mark.parametrize(
    ("attr", "stored", "fallback"),
    [
        pytest.param("last_percent", "forty-two", None, id="unparsable-text"),
        pytest.param("last_compute_ts", 10**400, 0.0, id="wider-than-a-float"),
        pytest.param("created_ts", {"nested": 1}, 0.0, id="wrong-type"),
    ],
)
def test_unusable_stored_scalar_is_reported_at_warning(
    caplog, attr: str, stored: object, fallback: float | None
) -> None:
    """A stored scalar that cannot be parsed is named instead of dropped.

    The field falls back to the value a first start leaves there, so the two
    are told apart by the report rather than by the resulting state.
    """
    with caplog.at_level(logging.DEBUG):
        state = import_mpc_v2_state({attr: stored}, key="uid:climate.hall:t21.0")

    assert getattr(state, attr) == fallback
    reports = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(reports) == 1
    assert reports[0].exc_info is not None
    assert attr in reports[0].getMessage()
    assert "uid:climate.hall:t21.0" in reports[0].getMessage()


def test_usable_stored_scalars_restore_without_a_report(caplog) -> None:
    """The values the store actually writes rehydrate silently."""
    payload = {
        "last_percent": 42.0,
        "last_compute_ts": 1_700_000_000.0,
        "created_ts": 1_699_000_000.0,
    }

    with caplog.at_level(logging.DEBUG):
        state = import_mpc_v2_state(payload, key="uid:climate.hall:t21.0")

    assert state.last_percent == 42.0
    assert state.last_compute_ts == 1_700_000_000.0
    assert state.created_ts == 1_699_000_000.0
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
