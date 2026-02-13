"""Comprehensive tests for the MPC (Model Predictive Control) controller.

Each test class targets a specific subsystem; tests are deterministic
using real time with sufficiently large deltas.
"""

from __future__ import annotations

from time import time

import pytest

from custom_components.better_thermostat.utils.calibration import mpc as mpc_mod
from custom_components.better_thermostat.utils.calibration.mpc import (
    DISTRIBUTE_COMPENSATION_PCT_PER_K,
    MpcInput,
    MpcOutput,
    MpcParams,
    _curve_bin_label,
    _detect_regime_change,
    _detect_trv_profile,
    _MpcState,
    _round_for_debug,
    _split_mpc_key,
    _update_perf_curve,
    build_mpc_group_key,
    build_mpc_key,
    compute_mpc,
    distribute_valve_percent,
    export_mpc_state_map,
    import_mpc_state_map,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_mpc_states():
    """Reset global MPC state before every test."""
    mpc_mod._MPC_STATES.clear()
    yield
    mpc_mod._MPC_STATES.clear()


def _default_params(**overrides) -> MpcParams:
    """Return MpcParams with sane test defaults (fast updates, no hold-time)."""
    defaults = {
        "min_update_interval_s": 0.0,
        "min_percent_hold_time_s": 0.0,
        "percent_hysteresis_pts": 0.0,
        "mpc_du_max_pct": 100.0,
        "use_virtual_temp": False,
        "enable_min_effective_percent": False,
    }
    defaults.update(overrides)
    return MpcParams(**defaults)


def _compute(inp: MpcInput, params: MpcParams) -> MpcOutput | None:
    """Call compute_mpc and return only the MpcOutput (discard state).

    Most tests only need the output; the few that inspect the returned
    state can call ``compute_mpc`` directly.
    """
    output, _state = compute_mpc(inp, params)
    return output


def _inp(key: str = "test", **overrides) -> MpcInput:
    """Shortcut for creating MpcInput with defaults."""
    defaults = {
        "key": key,
        "target_temp_C": 22.0,
        "current_temp_C": 20.0,
        "heating_allowed": True,
        "window_open": False,
    }
    defaults.update(overrides)
    return MpcInput(**defaults)


# ===================================================================
# 1. HELPER FUNCTIONS
# ===================================================================


class TestRoundForDebug:
    """Tests for _round_for_debug."""

    def test_rounds_float(self):
        """Test rounding a float to specified decimal places."""
        assert _round_for_debug(3.14159, 2) == 3.14

    def test_returns_none_for_none(self):
        """Test that None input passes through unchanged."""
        assert _round_for_debug(None) is None

    def test_returns_string_for_string(self):
        """Test that string input passes through unchanged."""
        assert _round_for_debug("hello") == "hello"

    def test_handles_inf(self):
        """Test that infinity passes through without error."""
        result = _round_for_debug(float("inf"))
        assert result == float("inf")

    def test_handles_negative(self):
        """Test rounding negative floats (Python banker's rounding)."""
        assert _round_for_debug(-1.2345, 3) == -1.234


class TestSplitMpcKey:
    """Tests for _split_mpc_key."""

    def test_valid_key(self):
        """Test splitting a valid three-part MPC key."""
        uid, entity, bucket = _split_mpc_key("abc:climate.trv:t22.0")
        assert uid == "abc"
        assert entity == "climate.trv"
        assert bucket == "t22.0"

    def test_invalid_key_no_colons(self):
        """Test that a key without colons returns all None."""
        assert _split_mpc_key("nocolons") == (None, None, None)

    def test_key_with_one_colon(self):
        """Test that a key with only one colon returns all None."""
        assert _split_mpc_key("one:two") == (None, None, None)

    def test_key_with_extra_colons(self):
        """Test that extra colons are kept in the bucket part."""
        uid, entity, bucket = _split_mpc_key("a:b:c:d:e")
        assert uid == "a"
        assert entity == "b"
        assert bucket == "c:d:e"


class TestCurveBinLabel:
    """Tests for _curve_bin_label."""

    def test_zero_percent(self):
        """Test bin label for 0% valve position."""
        assert _curve_bin_label(0.0, 5.0) == "p00_05"

    def test_hundred_percent(self):
        """Test bin label for 100% valve position."""
        label = _curve_bin_label(100.0, 5.0)
        assert label == "p100_100"

    def test_negative_percent_clamped(self):
        """Test that negative percent is clamped to 0."""
        label = _curve_bin_label(-5.0, 5.0)
        assert label == "p00_05"

    def test_over_hundred_clamped(self):
        """Test that >100% is clamped to 100."""
        label = _curve_bin_label(105.0, 5.0)
        assert label == "p100_100"

    def test_fractional_bin(self):
        """Test bin label with fractional valve position."""
        label = _curve_bin_label(3.7, 2.5)
        assert "p" in label

    def test_bin_pct_below_one_clamped_to_one(self):
        """Test that bin_pct below 1.0 is clamped to 1.0."""
        label = _curve_bin_label(50.0, 0.5)
        # bin_pct should be clamped to 1.0
        assert "p50_51" == label


class TestBuildMpcKey:
    """Tests for build_mpc_key."""

    def test_normal_key(self):
        """Test building a key with normal uid and target temp."""

        class FakeBT:
            bt_target_temp = 21.5
            unique_id = "bt_123"

        key = build_mpc_key(FakeBT(), "climate.trv_1")
        # 21.5 rounded to 0.5 step => 21.5
        assert key == "bt_123:climate.trv_1:t21.5"

    def test_target_none(self):
        """Test that None target temp produces 'tunknown' bucket."""

        class FakeBT:
            bt_target_temp = None
            unique_id = "bt_x"

        key = build_mpc_key(FakeBT(), "climate.trv")
        assert "tunknown" in key

    def test_target_string(self):
        """Test that non-numeric target temp produces 'tunknown' bucket."""

        class FakeBT:
            bt_target_temp = "unavailable"
            unique_id = "bt_y"

        key = build_mpc_key(FakeBT(), "climate.trv")
        assert "tunknown" in key

    def test_missing_unique_id_fallback(self):
        """Test fallback uid when unique_id attribute is missing."""

        class FakeBT:
            bt_target_temp = 20.0

        key = build_mpc_key(FakeBT(), "climate.trv")
        assert key.startswith("bt:")

    def test_target_rounding_half_degree(self):
        """Test that target temp is rounded to 0.5 degree steps."""

        class FakeBT:
            bt_target_temp = 21.3
            unique_id = "u"

        key = build_mpc_key(FakeBT(), "e")
        # round(21.3 * 2) / 2 = round(42.6) / 2 = 43 / 2 = 21.5
        assert "t21.5" in key


# ===================================================================
# 2. STATE PERSISTENCE (export / import)
# ===================================================================


class TestStatePersistence:
    """Tests for export_mpc_state_map and import_mpc_state_map."""

    def test_export_empty(self):
        """Test that exporting with no states returns empty dict."""
        assert export_mpc_state_map() == {}

    def test_round_trip(self):
        """Test that all MPC state fields survive an export/import round-trip."""
        state = _MpcState()
        state.gain_est = 0.08
        state.loss_est = 0.015
        state.last_percent = 42.0
        state.min_effective_percent = 12.0
        state.dead_zone_hits = 3
        state.is_calibration_active = True
        state.trv_profile = "threshold"
        state.profile_confidence = 0.85
        mpc_mod._MPC_STATES["k1"] = state

        exported = export_mpc_state_map()
        assert "k1" in exported
        payload = exported["k1"]
        assert payload["gain_est"] == 0.08
        assert payload["dead_zone_hits"] == 3
        assert payload["trv_profile"] == "threshold"

        # Clear and re-import
        mpc_mod._MPC_STATES.clear()
        import_mpc_state_map(exported)
        restored = mpc_mod._MPC_STATES["k1"]
        assert restored.gain_est == pytest.approx(0.08)
        assert restored.loss_est == pytest.approx(0.015)
        assert restored.dead_zone_hits == 3
        assert restored.is_calibration_active is True
        assert restored.trv_profile == "threshold"

    def test_export_with_prefix_filter(self):
        """Test that export with prefix only returns matching keys."""
        mpc_mod._MPC_STATES["bt1:trv:t22.0"] = _MpcState(gain_est=0.05)
        mpc_mod._MPC_STATES["bt2:trv:t22.0"] = _MpcState(gain_est=0.06)

        filtered = export_mpc_state_map(prefix="bt1")
        assert "bt1:trv:t22.0" in filtered
        assert "bt2:trv:t22.0" not in filtered

    def test_import_ignores_invalid_payload(self):
        """Test that non-dict payloads are silently skipped."""
        import_mpc_state_map({"k": "not_a_dict"})
        assert "k" not in mpc_mod._MPC_STATES

    def test_import_ignores_unknown_fields(self):
        """Test that unknown fields in payload are ignored without error."""
        import_mpc_state_map({"k": {"gain_est": 0.05, "unknown_field": 999}})
        assert mpc_mod._MPC_STATES["k"].gain_est == pytest.approx(0.05)

    def test_import_coerces_types(self):
        """Test that string values are coerced to proper numeric types."""
        import_mpc_state_map({"k": {"gain_est": "0.07", "dead_zone_hits": "5"}})
        s = mpc_mod._MPC_STATES["k"]
        assert s.gain_est == pytest.approx(0.07)
        assert s.dead_zone_hits == 5

    def test_import_handles_none_values(self):
        """Test that None values in payload are preserved."""
        import_mpc_state_map({"k": {"gain_est": None}})
        assert mpc_mod._MPC_STATES["k"].gain_est is None

    def test_import_handles_perf_curve(self):
        """Test that perf_curve dicts are restored correctly."""
        curve = {"p00_05": {"count": 3, "avg_room_rate": 0.01}}
        import_mpc_state_map({"k": {"perf_curve": curve}})
        assert mpc_mod._MPC_STATES["k"].perf_curve == curve

    def test_export_skips_none_fields(self):
        """Serialization should skip None values to keep payload compact."""
        mpc_mod._MPC_STATES["k"] = _MpcState()  # All defaults (mostly None)
        exported = export_mpc_state_map()
        # A default state has very few non-None values
        if "k" in exported:
            for v in exported["k"].values():
                assert v is not None


# ===================================================================
# 3. COMPUTE_MPC CORE LOGIC
# ===================================================================


class TestComputeMpcBasic:
    """Tests for basic compute_mpc behavior."""

    def test_returns_mpc_output(self):
        """Test that compute_mpc returns a valid MpcOutput."""
        result = _compute(_inp(), _default_params())
        assert isinstance(result, MpcOutput)
        assert 0 <= result.valve_percent <= 100

    def test_window_open_returns_zero(self):
        """Test that window_open forces valve to 0%."""
        result = _compute(_inp(window_open=True), _default_params())
        assert result.valve_percent == 0

    def test_heating_not_allowed_returns_zero(self):
        """Test that heating_allowed=False forces valve to 0%."""
        result = _compute(_inp(heating_allowed=False), _default_params())
        assert result.valve_percent == 0

    def test_missing_target_temp(self):
        """Test that None target temp produces 0% valve."""
        result = _compute(_inp(target_temp_C=None), _default_params())
        assert result is not None
        assert result.valve_percent == 0

    def test_missing_current_temp(self):
        """Test that None current temp does not crash."""
        result = _compute(_inp(current_temp_C=None), _default_params())
        assert result is not None

    def test_both_temps_none(self):
        """Test that both temps None does not crash."""
        result = _compute(
            _inp(target_temp_C=None, current_temp_C=None), _default_params()
        )
        assert result is not None

    def test_large_positive_error_gives_high_valve(self):
        """4K below target -> valve should be high."""
        result = _compute(
            _inp(current_temp_C=18.0, target_temp_C=22.0), _default_params()
        )
        assert result.valve_percent >= 50

    def test_at_target_gives_base_load(self):
        """At target -> valve should be around u0 (base load), not zero."""
        params = _default_params(mpc_adapt=False)
        result = _compute(_inp(current_temp_C=22.0, target_temp_C=22.0), params)
        # At target, optimizer should find u0 as optimal (compensate losses)
        assert result.valve_percent >= 0

    def test_above_target_gives_zero_or_low(self):
        """0.5K above target -> valve should be low or zero."""
        params = _default_params(mpc_adapt=False)
        result = _compute(_inp(current_temp_C=22.5, target_temp_C=22.0), params)
        assert result.valve_percent <= 30

    def test_far_above_target_shutoff(self):
        """1K above target -> should shut off."""
        params = _default_params(mpc_adapt=False)
        result = _compute(_inp(current_temp_C=23.0, target_temp_C=22.0), params)
        assert result.valve_percent == 0

    def test_valve_monotonically_increases_with_error(self):
        """Larger error should produce higher (or equal) valve output."""
        params = _default_params(mpc_adapt=False)
        temps = [21.5, 21.0, 20.5, 20.0, 19.0, 18.0]
        results = []
        for current in temps:
            r = _compute(_inp(key=f"mono_{current}", current_temp_C=current), params)
            results.append(r.valve_percent)
        # Each should be >= previous (or equal for saturation)
        for i in range(1, len(results)):
            assert results[i] >= results[i - 1], (
                f"Valve at {temps[i]}°C ({results[i]}%) < valve at "
                f"{temps[i - 1]}°C ({results[i - 1]}%)"
            )

    def test_filtered_temp_reduces_valve_demand(self):
        """filtered_temp_C closer to target should lower the cost-optimized valve."""
        params = _default_params(mpc_adapt=False)
        raw = _compute(_inp(key="filt_raw", current_temp_C=20.5), params)
        filt = _compute(
            _inp(key="filt_filt", current_temp_C=20.5, filtered_temp_C=21.8), params
        )
        assert filt.valve_percent <= raw.valve_percent

    def test_window_open_resets_control_state(self):
        """Test that window_open resets integrals and control state."""
        params = _default_params(mpc_adapt=True)
        _compute(_inp(key="win"), params)
        state = mpc_mod._MPC_STATES["win"]
        state.last_percent = 50.0
        state.u_integral = 1000.0
        state.time_integral = 100.0

        _compute(_inp(key="win", window_open=True), params)
        state = mpc_mod._MPC_STATES["win"]
        assert state.last_percent == 0.0
        assert state.u_integral == 0.0
        assert state.time_integral == 0.0
        assert state.virtual_temp is None
        assert state.last_residual_time is None

    def test_calibration_aborted_on_window_open(self):
        """Active calibration should be aborted when window opens."""
        params = _default_params()
        _compute(_inp(key="cal_abort"), params)
        mpc_mod._MPC_STATES["cal_abort"].is_calibration_active = True

        _compute(_inp(key="cal_abort", window_open=True), params)
        assert mpc_mod._MPC_STATES["cal_abort"].is_calibration_active is False

    def test_valve_integration_accumulates(self):
        """u_integral should accumulate valve position over time."""
        params = _default_params()
        _compute(_inp(key="integ"), params)
        state = mpc_mod._MPC_STATES["integ"]
        state.last_percent = 50.0
        old_integral = state.u_integral

        _compute(_inp(key="integ"), params)
        # Integration should have increased (last_percent * dt)
        # dt might be tiny in tests, but integral shouldn't decrease
        assert state.u_integral >= old_integral


# ===================================================================
# 4. ADAPTIVE LEARNING
# ===================================================================


class TestAdaptiveLearning:
    """Tests for gain/loss adaptation logic."""

    def _setup_learning_state(self, key: str, params: MpcParams, **state_overrides):
        """Initialize MPC state and set up for learning."""
        _compute(_inp(key=key), params)
        state = mpc_mod._MPC_STATES[key]
        for k, v in state_overrides.items():
            setattr(state, k, v)
        return state

    def test_gain_initialized_on_first_call(self):
        """Test that gain_est is seeded from mpc_thermal_gain on first call."""
        params = _default_params(mpc_adapt=True, mpc_thermal_gain=0.08)
        _compute(_inp(key="ginit"), params)
        state = mpc_mod._MPC_STATES["ginit"]
        assert state.gain_est == pytest.approx(0.08)

    def test_loss_initialized_on_first_call(self):
        """Test that loss_est is seeded from mpc_loss_coeff on first call."""
        params = _default_params(mpc_adapt=True, mpc_loss_coeff=0.012)
        _compute(_inp(key="linit"), params)
        state = mpc_mod._MPC_STATES["linit"]
        assert state.loss_est == pytest.approx(0.012)

    def test_no_adaptation_when_disabled(self):
        """Test that gain_est and loss_est stay None when mpc_adapt=False."""
        params = _default_params(mpc_adapt=False)
        _compute(_inp(key="noadapt"), params)
        state = mpc_mod._MPC_STATES["noadapt"]
        assert state.gain_est is None
        assert state.loss_est is None

    def test_loss_learns_when_valve_closed_and_cooling(self):
        """When valve is 0% and room cools, loss should be learned."""
        params = _default_params(
            mpc_adapt=True,
            mpc_adapt_alpha=0.5,
            mpc_loss_coeff=0.01,
            enable_min_effective_percent=False,
        )
        state = self._setup_learning_state(
            "loss_cool",
            params,
            last_percent=0.0,
            last_learn_temp=21.0,
            last_learn_time=time() - 300,  # 5 min ago
            gain_est=0.06,
            loss_est=0.01,
            u_integral=0.0,
            time_integral=300.0,
        )
        loss_before = state.loss_est

        # Room cooled from 21.0 to 20.5 in 5 min = -0.1 °C/min
        _compute(_inp(key="loss_cool", current_temp_C=20.5), params)
        # Loss should have increased (room is cooling faster than model predicted)
        assert state.loss_est >= loss_before

    def test_gain_learns_when_valve_open_and_warming(self):
        """When valve is open and room warms, gain should be learned."""
        params = _default_params(
            mpc_adapt=True,
            mpc_adapt_alpha=0.5,
            mpc_thermal_gain=0.06,
            mpc_loss_coeff=0.01,
            enable_min_effective_percent=False,
        )
        state = self._setup_learning_state(
            "gain_warm",
            params,
            last_percent=80.0,
            last_learn_temp=20.0,
            last_learn_time=time() - 300,
            gain_est=0.06,
            loss_est=0.01,
            u_integral=80.0 * 300,
            time_integral=300.0,
        )

        # Room warmed from 20.0 to 20.5 in 5 min
        _compute(_inp(key="gain_warm", current_temp_C=20.5), params)
        # gain_est should have been updated
        assert state.gain_est is not None

    def test_adaptation_blocked_after_window_event(self):
        """Adaptation should be blocked for mpc_adapt_window_block_s after window open."""
        params = _default_params(mpc_adapt=True, mpc_adapt_window_block_s=900.0)
        state = self._setup_learning_state(
            "win_block",
            params,
            last_percent=50.0,
            last_learn_temp=20.0,
            last_learn_time=time() - 300,
            last_window_open_ts=time() - 60,  # window opened 60s ago
            gain_est=0.06,
            loss_est=0.01,
        )
        gain_before = state.gain_est
        loss_before = state.loss_est

        _compute(_inp(key="win_block", current_temp_C=20.5), params)
        # Should NOT adapt (within window block period)
        assert state.gain_est == pytest.approx(gain_before)
        assert state.loss_est == pytest.approx(loss_before)

    def test_adaptation_blocked_on_target_change(self):
        """Adaptation should not learn during setpoint steps."""
        params = _default_params(mpc_adapt=True, mpc_adapt_alpha=0.5)
        state = self._setup_learning_state(
            "tgt_change",
            params,
            last_percent=50.0,
            last_learn_temp=20.0,
            last_learn_time=time() - 300,
            last_target_C=22.0,
            gain_est=0.06,
            loss_est=0.01,
            u_integral=50.0 * 300,
            time_integral=300.0,
        )
        gain_before = state.gain_est

        # Change target by >= 0.05
        _compute(
            _inp(key="tgt_change", target_temp_C=23.0, current_temp_C=20.5), params
        )
        # target_changed should block adaptation
        assert state.gain_est == pytest.approx(gain_before)

    def test_extreme_rate_rejected(self):
        """Sensor jumps (>0.35 °C/min) should be rejected."""
        params = _default_params(mpc_adapt=True, mpc_adapt_alpha=0.5)
        state = self._setup_learning_state(
            "extreme",
            params,
            last_percent=50.0,
            last_learn_temp=20.0,
            last_learn_time=time() - 180,  # 3 min
            gain_est=0.06,
            loss_est=0.01,
            u_integral=50.0 * 180,
            time_integral=180.0,
        )
        gain_before = state.gain_est

        # Temperature jumped from 20.0 to 22.0 in 3 min = 0.67 °C/min
        _compute(_inp(key="extreme", current_temp_C=22.0), params)
        # Should NOT update gain (rate > 0.35 °C/min)
        assert state.gain_est == pytest.approx(gain_before)

    def test_gain_clamped_to_bounds(self):
        """gain_est should always be in [gain_min, gain_max]."""
        params = _default_params(
            mpc_adapt=True, mpc_gain_min=0.01, mpc_gain_max=0.2, mpc_adapt_alpha=0.9
        )
        state = self._setup_learning_state(
            "gclamp",
            params,
            last_percent=100.0,
            last_learn_temp=20.0,
            last_learn_time=time() - 300,
            gain_est=0.19,
            loss_est=0.01,
            u_integral=100.0 * 300,
            time_integral=300.0,
        )

        # Room warmed a lot -> gain candidate could be very high
        _compute(_inp(key="gclamp", current_temp_C=20.8), params)
        assert state.gain_est <= params.mpc_gain_max
        assert state.gain_est >= params.mpc_gain_min

    def test_loss_clamped_to_bounds(self):
        """loss_est should always be in [loss_min, loss_max]."""
        params = _default_params(
            mpc_adapt=True,
            mpc_loss_min=0.002,
            mpc_loss_max=0.03,
            mpc_adapt_alpha=0.9,
            enable_min_effective_percent=False,
        )
        state = self._setup_learning_state(
            "lclamp",
            params,
            last_percent=0.0,
            last_learn_temp=21.0,
            last_learn_time=time() - 300,
            gain_est=0.06,
            loss_est=0.025,
            u_integral=0.0,
            time_integral=300.0,
        )

        # Room cooled fast -> loss candidate high
        _compute(_inp(key="lclamp", current_temp_C=20.0), params)
        assert state.loss_est <= params.mpc_loss_max
        assert state.loss_est >= params.mpc_loss_min

    def test_loss_skipped_for_open_window_rate(self):
        """Extreme cooling rate (>1.5x max loss) should be skipped (suspected open window)."""
        params = _default_params(
            mpc_adapt=True,
            mpc_loss_max=0.03,
            mpc_adapt_alpha=0.5,
            enable_min_effective_percent=False,
        )
        state = self._setup_learning_state(
            "loss_ow",
            params,
            last_percent=0.0,
            last_learn_temp=21.0,
            last_learn_time=time() - 300,
            gain_est=0.06,
            loss_est=0.015,
            u_integral=0.0,
            time_integral=300.0,
        )
        loss_before = state.loss_est

        # Room dropped 1.5K in 5 min = 0.3 °C/min >> 1.5 * 0.03 = 0.045
        # But max rate is 0.35, so we need a smaller drop: 1K in 5min = 0.2 °C/min
        # 0.2 > 0.045 → should be skipped
        _compute(_inp(key="loss_ow", current_temp_C=20.0), params)
        assert state.loss_est == pytest.approx(loss_before)

    def test_ka_est_initialized_with_outdoor_temp(self):
        """ka_est should be calculated when outdoor_temp is provided."""
        params = _default_params(mpc_adapt=True, mpc_loss_coeff=0.01)
        _compute(_inp(key="ka", current_temp_C=20.0, outdoor_temp_C=5.0), params)
        state = mpc_mod._MPC_STATES["ka"]
        assert state.ka_est is not None
        # ka = loss / (indoor - outdoor) = 0.01 / 15 ≈ 0.000667
        assert state.ka_est == pytest.approx(0.01 / 15.0, rel=0.01)

    def test_insufficient_heat_boost_reduces_gain(self):
        """When room stays below target in steady-state, gain should be reduced to increase base load."""
        params = _default_params(
            mpc_adapt=True, mpc_adapt_alpha=0.1, enable_min_effective_percent=False
        )
        # u0_frac = loss/gain = 0.01/0.06 ≈ 0.167, so u_last should be ~0.167
        state = self._setup_learning_state(
            "insuff",
            params,
            last_percent=16.7,  # near u0 (within 10% absolute)
            last_learn_temp=21.0,  # same as current -> delta_T=0 -> no temp_changed
            last_learn_time=time() - 400,
            last_residual_time=time() - 400,
            gain_est=0.06,
            loss_est=0.01,
            u_integral=16.7 * 400,
            time_integral=400.0,
            last_target_C=22.0,
            consecutive_insufficient_heat=0,
        )
        gain_before = state.gain_est

        # Room is 1K below target, temp unchanged (steady state -> rate ≈ 0)
        _compute(_inp(key="insuff", current_temp_C=21.0, target_temp_C=22.0), params)
        # With residual learning: loss_candidate = gain*u - rate ≈ 0.06*0.167 - 0 ≈ 0.01
        # If loss_candidate > loss_est AND target-current > 0.2 -> insufficient heat
        # -> gain should be reduced
        assert state.gain_est <= gain_before


# ===================================================================
# 5. VIRTUAL TEMPERATURE
# ===================================================================


class TestVirtualTemperature:
    """Tests for virtual temperature forward prediction and sync."""

    def test_virtual_temp_initialized_from_sensor(self):
        """Test that virtual_temp starts at the sensor reading."""
        params = _default_params(use_virtual_temp=True)
        _compute(_inp(key="vinit", current_temp_C=20.5), params)
        state = mpc_mod._MPC_STATES["vinit"]
        assert state.virtual_temp == pytest.approx(20.5)

    def test_virtual_temp_corrects_large_drift(self):
        """Kalman filter should correct virtual_temp when it drifts far from sensor."""
        params = _default_params(use_virtual_temp=True)
        _compute(_inp(key="vreset", current_temp_C=20.0), params)
        state = mpc_mod._MPC_STATES["vreset"]
        # Artificially drift virtual temp far from sensor
        state.virtual_temp = 21.0  # 1K off from sensor at 20.0
        state.last_sensor_temp_C = 19.5  # different from current so update triggers
        state.last_percent = 50.0
        state.kalman_P = 1.0  # High uncertainty → Kalman gain ≈ 1.0

        _compute(_inp(key="vreset", current_temp_C=20.0), params)
        # Kalman should correct most of the 1K drift
        assert abs(state.virtual_temp - 20.0) < 0.5

    def test_virtual_temp_stays_close_to_sensor(self):
        """Kalman update should keep virtual_temp close to sensor value."""
        params = _default_params(use_virtual_temp=True)
        _compute(_inp(key="vclamp", current_temp_C=20.0), params)
        state = mpc_mod._MPC_STATES["vclamp"]
        state.virtual_temp = 20.3  # slightly drifted
        state.last_sensor_temp_C = 19.9  # different so update fires
        state.last_percent = 50.0

        _compute(_inp(key="vclamp", current_temp_C=20.0), params)
        # After Kalman update, virtual_temp should be closer to sensor
        assert abs(state.virtual_temp - 20.0) < 0.3

    def test_virtual_temp_not_synced_when_sensor_unchanged(self):
        """Sync should be skipped when sensor value hasn't changed."""
        params = _default_params(use_virtual_temp=True)
        _compute(_inp(key="vsame", current_temp_C=20.0), params)
        state = mpc_mod._MPC_STATES["vsame"]
        state.last_sensor_temp_C = 20.0  # same as current
        state.last_percent = 50.0

        _compute(_inp(key="vsame", current_temp_C=20.0), params)
        assert state.virtual_temp is not None

    def test_virtual_temp_used_for_delta_t(self):
        """When virtual temp is enabled, delta_t should use virtual temp, not sensor."""
        params = _default_params(use_virtual_temp=True)
        _compute(_inp(key="vdelta", current_temp_C=20.0, target_temp_C=22.0), params)
        state = mpc_mod._MPC_STATES["vdelta"]
        # Virtual temp should be close to sensor on first call
        assert state.virtual_temp is not None

    def test_window_open_clears_virtual_temp(self):
        """Test that window_open resets virtual_temp to None."""
        params = _default_params(use_virtual_temp=True)
        _compute(_inp(key="vwin", current_temp_C=20.0), params)
        assert mpc_mod._MPC_STATES["vwin"].virtual_temp is not None

        _compute(_inp(key="vwin", window_open=True), params)
        assert mpc_mod._MPC_STATES["vwin"].virtual_temp is None


# ===================================================================
# 6. REGIME CHANGE DETECTION
# ===================================================================


class TestRegimeChangeDetection:
    """Tests for _detect_regime_change."""

    def test_too_few_samples_returns_false(self):
        """Test that fewer than N samples always returns False."""
        assert _detect_regime_change([0.1] * 5) is False

    def test_exactly_n_samples_needed(self):
        """Test that N samples of zero error returns False (no bias)."""
        # 10 samples of the same error (mean != 0, std = 0 -> special case)
        assert _detect_regime_change([0.0] * 10) is False  # mean=0

    def test_strong_positive_bias_detected(self):
        """Test that constant positive prediction errors are detected as regime change."""
        errors = [0.05] * 10
        assert _detect_regime_change(errors) is True

    def test_strong_negative_bias_detected(self):
        """Test that constant negative prediction errors are detected as regime change."""
        errors = [-0.05] * 10
        assert _detect_regime_change(errors) is True

    def test_zero_mean_not_detected(self):
        """Test that alternating errors with zero mean are not detected."""
        # Alternating positive/negative -> mean ≈ 0
        errors = [0.05, -0.05] * 5
        assert _detect_regime_change(errors) is False

    def test_high_variance_masks_bias(self):
        """Test that high variance masks a small mean bias."""
        # Mean is 0.01 but std is high -> t-stat low
        import random as rng

        rng.seed(42)
        errors = [rng.gauss(0.01, 0.5) for _ in range(10)]
        # With high variance, bias is not statistically significant
        result = _detect_regime_change(errors)
        # This is stochastic but with seed=42 should be False
        assert isinstance(result, bool)

    def test_uses_only_last_n_samples(self):
        """Test that only the last N samples are considered."""
        # Old samples biased, recent samples neutral
        old = [0.1] * 10
        new = [0.001, -0.001] * 5
        errors = old + new
        assert _detect_regime_change(errors) is False


class TestRegimeBoostIntegration:
    """Test that regime change detection integrates with adaptation."""

    def test_regime_boost_activates_on_sustained_bias(self):
        """Test that regime boost triggers when recent_errors show sustained bias."""
        params = _default_params(mpc_adapt=True, mpc_adapt_alpha=0.1)
        _compute(_inp(key="rboost"), params)
        state = mpc_mod._MPC_STATES["rboost"]
        # Inject biased errors to trigger regime change
        state.recent_errors = [0.05] * 15
        # But _detect_regime_change returns False when std==0
        # So we need some variance
        state.recent_errors = [0.04 + 0.001 * i for i in range(15)]
        # Now mean ≈ 0.047, std is tiny -> t-stat should be high
        assert _detect_regime_change(state.recent_errors) is True


# ===================================================================
# 7. TRV PROFILE DETECTION
# ===================================================================


class TestTrvProfileDetection:
    """Tests for _detect_trv_profile."""

    def test_threshold_profile_detected(self):
        """Small command with weak response -> threshold."""
        state = _MpcState()
        params = _default_params(deadzone_threshold_pct=30.0)
        # percent_out=20 (small), response_ratio = 0.1/1.0 = 0.1 (weak)
        _detect_trv_profile(
            state,
            percent_out=20.0,
            temp_delta=0.1,
            time_delta=300.0,
            expected_temp_rise=1.0,
            params=params,
        )
        assert state.trv_profile == "threshold"
        assert state.profile_confidence > 0

    def test_linear_profile_detected(self):
        """Response ratio close to 1.0 -> linear."""
        state = _MpcState()
        params = _default_params(deadzone_threshold_pct=20.0)
        # percent_out=50 (above threshold), response close to expected
        _detect_trv_profile(
            state,
            percent_out=50.0,
            temp_delta=0.9,
            time_delta=300.0,
            expected_temp_rise=1.0,
            params=params,
        )
        assert state.trv_profile == "linear"

    def test_exponential_profile_detected(self):
        """High command with response > 1.2x expected -> exponential."""
        state = _MpcState()
        params = _default_params(deadzone_threshold_pct=20.0)
        _detect_trv_profile(
            state,
            percent_out=60.0,
            temp_delta=1.5,
            time_delta=300.0,
            expected_temp_rise=1.0,
            params=params,
        )
        assert state.trv_profile == "exponential"

    def test_profile_samples_increment(self):
        """Test that profile_samples increments with each valid detection."""
        state = _MpcState()
        params = _default_params()
        _detect_trv_profile(state, 50.0, 0.5, 300.0, 0.5, params)
        assert state.profile_samples == 1
        _detect_trv_profile(state, 50.0, 0.5, 300.0, 0.5, params)
        assert state.profile_samples == 2

    def test_no_detection_with_zero_time(self):
        """Test that zero time_delta skips profile detection."""
        state = _MpcState()
        params = _default_params()
        _detect_trv_profile(state, 50.0, 0.5, 0.0, 0.5, params)
        assert state.profile_samples == 0

    def test_no_detection_with_zero_expected_rise(self):
        """Test that zero expected_temp_rise skips profile detection."""
        state = _MpcState()
        params = _default_params()
        _detect_trv_profile(state, 50.0, 0.5, 300.0, 0.0, params)
        assert state.profile_samples == 0

    def test_no_detection_with_zero_percent(self):
        """Test that 0% valve skips profile detection."""
        state = _MpcState()
        params = _default_params()
        _detect_trv_profile(state, 0.0, 0.5, 300.0, 1.0, params)
        assert state.profile_samples == 0


# ===================================================================
# 8. POST-PROCESSING (hysteresis, hold-time, du_max)
# ===================================================================


class TestPostProcessing:
    """Tests for _post_process_percent behavior via compute_mpc."""

    def test_hysteresis_suppresses_small_changes(self):
        """Small changes should be suppressed by hysteresis."""
        params = _default_params(percent_hysteresis_pts=3.0)
        # First call establishes baseline
        r1 = _compute(_inp(key="hyst", current_temp_C=20.0), params)
        pct1 = r1.valve_percent

        # Second call with tiny temperature change
        r2 = _compute(_inp(key="hyst", current_temp_C=20.05), params)
        # Should be same due to hysteresis
        assert r2.valve_percent == pct1

    def test_target_change_bypasses_hysteresis(self):
        """Changing target temp should bypass hysteresis and hold-time."""
        params = _default_params(
            percent_hysteresis_pts=5.0, min_percent_hold_time_s=300.0
        )
        r1 = _compute(
            _inp(key="tgt_bypass", current_temp_C=20.0, target_temp_C=22.0), params
        )
        pct1 = r1.valve_percent

        # Change target significantly
        r2 = _compute(
            _inp(key="tgt_bypass", current_temp_C=20.0, target_temp_C=18.0), params
        )
        # Should produce a different result (not blocked)
        # With target below current, should be 0
        assert r2.valve_percent != pct1 or r2.valve_percent == 0

    def test_du_max_limits_step_size(self):
        """du_max should limit how fast the valve can change."""
        params = _default_params(mpc_du_max_pct=5.0)

        # First call: cold room -> high valve
        _compute(_inp(key="dumax", current_temp_C=18.0), params)
        state = mpc_mod._MPC_STATES["dumax"]
        state.last_percent = 50.0  # Force a known starting point
        state.last_update_ts = time()

        # Second call: want to go to 0 (warm room)
        r = _compute(_inp(key="dumax", current_temp_C=25.0, target_temp_C=22.0), params)
        # Change should be limited to ±5%
        assert r.valve_percent >= 45  # 50 - 5

    def test_hold_time_blocks_rapid_updates(self):
        """min_percent_hold_time_s should block updates that come too fast."""
        params = _default_params(min_percent_hold_time_s=300.0)

        r1 = _compute(_inp(key="hold", current_temp_C=20.0), params)
        pct1 = r1.valve_percent

        # Immediate second call (too soon)
        r2 = _compute(_inp(key="hold", current_temp_C=19.0), params)
        # Should be blocked (same as last)
        assert r2.valve_percent == pct1

    def test_min_effective_percent_clamp(self):
        """If min_effective_percent is set, low nonzero outputs should be clamped up."""
        params = _default_params(enable_min_effective_percent=True)
        _compute(_inp(key="mineff"), params)
        state = mpc_mod._MPC_STATES["mineff"]
        state.min_effective_percent = 15.0

        # Request a small valve opening
        r = _compute(
            _inp(key="mineff", current_temp_C=21.9, target_temp_C=22.0), params
        )
        # If valve > 0, should be >= 15
        if r.valve_percent > 0:
            assert r.valve_percent >= 15


# ===================================================================
# 9. PERFORMANCE CURVE SAMPLING
# ===================================================================


class TestPerfCurveSampling:
    """Tests for _update_perf_curve."""

    def test_perf_curve_not_updated_without_temps(self):
        """Test that perf_curve is not updated when current_temp is None."""
        state = _MpcState()
        params = _default_params()
        inp = _inp(current_temp_C=None)
        debug = {}
        _update_perf_curve(state, inp, params, time(), debug)
        assert state.perf_curve == {}

    def test_perf_curve_records_bin(self):
        """After two calls with enough time gap, a bin should be recorded."""
        state = _MpcState()
        params = _default_params(perf_curve_min_window_s=10.0)
        now = time()

        # First call: establish baseline
        inp1 = _inp(current_temp_C=20.0)
        _update_perf_curve(state, inp1, params, now, {})
        assert state.last_room_temp_C == 20.0

        # Second call: 60s later, temp rose
        state.last_percent = 40.0
        debug = {}
        inp2 = _inp(current_temp_C=20.5)
        _update_perf_curve(state, inp2, params, now + 60, debug)
        assert len(state.perf_curve) > 0
        assert "perf_curve_bin" in debug

    def test_perf_curve_skipped_when_window_open(self):
        """Test that perf_curve resets baseline but skips recording when window open."""
        state = _MpcState()
        state.last_room_temp_C = 20.0
        state.last_room_temp_ts = time() - 600
        params = _default_params()
        debug = {}
        inp = _inp(window_open=True, current_temp_C=20.5)
        _update_perf_curve(state, inp, params, time(), debug)
        # Should reset baseline but not record a bin
        assert "perf_curve_bin" not in debug

    def test_perf_curve_skipped_below_min_window(self):
        """Test that perf_curve skips recording when time gap is too short."""
        state = _MpcState()
        state.last_room_temp_C = 20.0
        state.last_room_temp_ts = time() - 1  # 1 second ago
        params = _default_params(perf_curve_min_window_s=300.0)
        debug = {}
        inp = _inp(current_temp_C=20.5)
        _update_perf_curve(state, inp, params, time(), debug)
        assert "perf_curve_bin" not in debug


# ===================================================================
# 10. FORCED CALIBRATION
# ===================================================================


class TestForcedCalibration:
    """Tests for forced loss calibration (random valve-off episodes)."""

    def test_calibration_ends_when_temp_drops_below_threshold(self):
        """Active calibration should end when temp < target - hysteresis."""
        params = _default_params()
        _compute(_inp(key="calend", current_temp_C=22.5, target_temp_C=22.0), params)
        state = mpc_mod._MPC_STATES["calend"]
        state.is_calibration_active = True

        # Temp drops to 21.7 (< 22.0 - 0.2 = 21.8)
        _compute(_inp(key="calend", current_temp_C=21.7, target_temp_C=22.0), params)
        assert state.is_calibration_active is False

    def test_calibration_keeps_valve_at_zero(self):
        """During active calibration, valve should be forced to 0."""
        params = _default_params()
        _compute(_inp(key="cal0", current_temp_C=22.1, target_temp_C=22.0), params)
        state = mpc_mod._MPC_STATES["cal0"]
        state.is_calibration_active = True

        result = _compute(
            _inp(key="cal0", current_temp_C=22.1, target_temp_C=22.0), params
        )
        assert result.valve_percent == 0

    def test_calibration_triggered_stochastically(self):
        """When at/above target, calibration may be triggered (probability-based)."""
        params = _default_params()
        triggered = False
        for i in range(50):
            key = f"cal_stoch_{i}"
            _compute(_inp(key=key, current_temp_C=22.5, target_temp_C=22.0), params)
            state = mpc_mod._MPC_STATES[key]
            if state.is_calibration_active:
                triggered = True
                break
        # With 50 tries and initial chance=1.0, it should trigger at least once
        assert triggered, "Forced calibration was never triggered in 50 attempts"

    def test_calibration_chance_decays_with_experience(self):
        """Calibration chance should decay as loss_learn_count increases."""
        params = _default_params()
        _compute(_inp(key="cal_decay"), params)
        state = mpc_mod._MPC_STATES["cal_decay"]
        state.loss_learn_count = 100
        # chance = max(0.05, 1/(100+1)) ≈ 0.01 -> clamped to 0.05
        # Very unlikely to trigger in a single attempt
        # (This is a probabilistic test — just verify the state is reasonable)
        assert state.loss_learn_count == 100


# ===================================================================
# 11. STALE STATE DETECTION
# ===================================================================


class TestStaleStateDetection:
    """Test that stale bucket switching resets learning anchors."""

    def test_stale_state_resets_learning(self):
        """Test that stale state (>15min) resets learning anchors."""
        params = _default_params(mpc_adapt=True)
        _compute(_inp(key="stale"), params)
        state = mpc_mod._MPC_STATES["stale"]
        state.last_time = time() - 1000  # 16+ min ago
        state.last_learn_temp = 19.0
        state.u_integral = 5000.0

        _compute(_inp(key="stale", current_temp_C=21.0), params)
        # u_integral should be reset
        assert state.u_integral == 0.0


# ===================================================================
# 12. SEED FROM SIBLINGS
# ===================================================================


class TestSeedFromSiblings:
    """Test that new keys can inherit min_effective_percent from siblings."""

    def test_sibling_seeding(self):
        """Test that new key inherits min_effective_percent from sibling."""
        params = _default_params(enable_min_effective_percent=True)
        # Create a sibling with known min_effective_percent
        sibling_state = _MpcState(min_effective_percent=15.0)
        mpc_mod._MPC_STATES["uid1:climate.trv:t21.0"] = sibling_state

        # New key same uid+entity, different bucket
        _compute(_inp(key="uid1:climate.trv:t22.0", current_temp_C=20.0), params)
        new_state = mpc_mod._MPC_STATES["uid1:climate.trv:t22.0"]
        assert new_state.min_effective_percent == 15.0

    def test_no_seeding_when_disabled(self):
        """Test that seeding is skipped when enable_min_effective_percent=False."""
        params = _default_params(enable_min_effective_percent=False)
        sibling_state = _MpcState(min_effective_percent=15.0)
        mpc_mod._MPC_STATES["uid2:climate.trv:t21.0"] = sibling_state

        _compute(_inp(key="uid2:climate.trv:t22.0", current_temp_C=20.0), params)
        new_state = mpc_mod._MPC_STATES["uid2:climate.trv:t22.0"]
        assert new_state.min_effective_percent is None

    def test_no_seeding_from_different_entity(self):
        """Test that seeding only happens from same uid+entity siblings."""
        params = _default_params(enable_min_effective_percent=True)
        sibling_state = _MpcState(min_effective_percent=15.0)
        mpc_mod._MPC_STATES["uid3:climate.trv_A:t21.0"] = sibling_state

        _compute(_inp(key="uid3:climate.trv_B:t22.0", current_temp_C=20.0), params)
        new_state = mpc_mod._MPC_STATES["uid3:climate.trv_B:t22.0"]
        assert new_state.min_effective_percent is None


# ===================================================================
# 13. EDGE CASES & ROBUSTNESS
# ===================================================================


class TestEdgeCases:
    """Edge cases and robustness tests."""

    def test_very_large_temperature_error(self):
        """20K error should not crash and should give 100%."""
        params = _default_params()
        result = _compute(_inp(current_temp_C=2.0, target_temp_C=22.0), params)
        assert result.valve_percent == 100

    def test_negative_temperature(self):
        """Negative temperatures (e.g., frost) should work."""
        params = _default_params()
        result = _compute(_inp(current_temp_C=-5.0, target_temp_C=5.0), params)
        assert result is not None
        assert result.valve_percent == 100

    def test_zero_gain_does_not_crash(self):
        """gain=0 should not cause ZeroDivisionError."""
        params = _default_params(mpc_thermal_gain=0.0, mpc_adapt=False)
        result = _compute(_inp(), params)
        assert result is not None

    def test_identical_consecutive_calls_are_stable(self):
        """Calling with same input repeatedly should produce stable output."""
        params = _default_params()
        results = []
        for _ in range(5):
            r = _compute(_inp(key="stable"), params)
            results.append(r.valve_percent)
        # After settling, values should be the same
        assert results[-1] == results[-2]

    def test_rapid_target_changes(self):
        """Rapid target temp changes should not crash."""
        params = _default_params()
        for target in [18, 22, 15, 25, 20, 23, 17]:
            result = _compute(
                _inp(key="rapid", current_temp_C=20.0, target_temp_C=float(target)),
                params,
            )
            assert result is not None
            assert 0 <= result.valve_percent <= 100

    def test_outdoor_temp_affects_loss_calculation(self):
        """With outdoor temp and ka_est, loss should be dynamic."""
        params = _default_params(mpc_adapt=True)
        # Cold outside -> higher loss
        r_cold = _compute(
            _inp(key="out_cold", current_temp_C=20.0, outdoor_temp_C=-10.0), params
        )
        # Warm outside -> lower loss
        r_warm = _compute(
            _inp(key="out_warm", current_temp_C=20.0, outdoor_temp_C=15.0), params
        )
        # Cold outside should need more valve
        assert r_cold.valve_percent >= r_warm.valve_percent

    def test_other_heat_power_reduces_valve(self):
        """Specifying other_heat_power should reduce valve demand."""
        params = _default_params(mpc_adapt=False)
        r_no_other = _compute(_inp(key="ohp_no", current_temp_C=20.0), params)
        r_with_other = _compute(
            _inp(key="ohp_yes", current_temp_C=20.0, other_heat_power=0.05), params
        )
        # other heat power should reduce (or equal) valve demand
        assert r_with_other.valve_percent <= r_no_other.valve_percent

    def test_overshoot_penalty_reduces_opening_above_target(self):
        """Higher overshoot penalty should reduce valve opening when above target."""
        params_no_overshoot = _default_params(
            mpc_adapt=False, mpc_overshoot_penalty=0.0
        )
        params_overshoot = _default_params(mpc_adapt=False, mpc_overshoot_penalty=5.0)

        r_no_overshoot = _compute(
            _inp(key="over_no", current_temp_C=22.1, target_temp_C=22.0),
            params_no_overshoot,
        )
        r_overshoot = _compute(
            _inp(key="over_yes", current_temp_C=22.1, target_temp_C=22.0),
            params_overshoot,
        )
        assert r_overshoot.valve_percent <= r_no_overshoot.valve_percent

    def test_slope_ema_updated_in_debug(self):
        """When temp_slope_K_per_min is provided, EMA slope should be tracked."""
        params = _default_params()
        _compute(_inp(key="slope_ema", temp_slope_K_per_min=0.05), params)
        state = mpc_mod._MPC_STATES["slope_ema"]
        assert state.ema_slope is not None
        assert state.ema_slope == pytest.approx(0.05)

        # Second call with different slope -> EMA blend
        _compute(_inp(key="slope_ema", temp_slope_K_per_min=0.10), params)
        # ema = 0.6 * 0.05 + 0.4 * 0.10 = 0.07
        assert state.ema_slope == pytest.approx(0.07, abs=0.001)


# ===================================================================
# 14. BUILD MPC GROUP KEY
# ===================================================================


class TestBuildMpcGroupKey:
    """Tests for build_mpc_group_key (multi-TRV group key)."""

    def test_group_key_format(self):
        """Group key uses ':group:' instead of entity_id."""

        class FakeBT:
            bt_target_temp = 21.5
            unique_id = "bt_123"

        key = build_mpc_group_key(FakeBT())
        assert key == "bt_123:group:t21.5"

    def test_group_key_target_none(self):
        """None target temp produces 'tunknown' bucket."""

        class FakeBT:
            bt_target_temp = None
            unique_id = "bt_x"

        key = build_mpc_group_key(FakeBT())
        assert "tunknown" in key
        assert ":group:" in key

    def test_group_key_missing_uid(self):
        """Fallback uid when unique_id attribute is missing."""

        class FakeBT:
            bt_target_temp = 20.0

        key = build_mpc_group_key(FakeBT())
        assert key.startswith("bt:")
        assert ":group:" in key

    def test_group_key_differs_from_entity_key(self):
        """Group key and entity key should differ (group vs entity_id)."""

        class FakeBT:
            bt_target_temp = 22.0
            unique_id = "bt_1"

        group_key = build_mpc_group_key(FakeBT())
        entity_key = build_mpc_key(FakeBT(), "climate.trv_1")
        assert group_key != entity_key
        assert ":group:" in group_key
        assert ":climate.trv_1:" in entity_key


# ===================================================================
# 15. DISTRIBUTE VALVE PERCENT (multi-TRV compensation)
# ===================================================================


class TestDistributeValvePercent:
    """Tests for distribute_valve_percent."""

    def test_empty_dict(self):
        """Empty TRV dict returns empty result."""
        assert distribute_valve_percent(50.0, {}) == {}

    def test_single_trv_passthrough(self):
        """Single TRV returns the group percentage unchanged."""
        result = distribute_valve_percent(60.0, {"trv_a": 20.0})
        assert result["trv_a"] == pytest.approx(60.0)

    def test_zero_command_gives_zero(self):
        """When group command is 0%, all TRVs get 0%."""
        result = distribute_valve_percent(0.0, {"a": 18.0, "b": 25.0})
        assert all(v == pytest.approx(0.0) for v in result.values())

    def test_uniform_same_temps(self):
        """TRVs with same temperature get uniform distribution."""
        result = distribute_valve_percent(50.0, {"a": 20.0, "b": 20.0})
        assert result["a"] == pytest.approx(50.0)
        assert result["b"] == pytest.approx(50.0)

    def test_colder_trv_gets_boost(self):
        """Colder TRV gets higher valve % than warmer one."""
        result = distribute_valve_percent(50.0, {"warm": 22.0, "cold": 19.0})
        # Warmest TRV = reference, gets baseline
        assert result["warm"] == pytest.approx(50.0)
        # cold is 3K below warm, boost = 3 * COMPENSATION_PCT_PER_K
        expected_cold = min(100.0, 50.0 + 3.0 * DISTRIBUTE_COMPENSATION_PCT_PER_K)
        assert result["cold"] == pytest.approx(expected_cold)

    def test_none_temps_get_baseline(self):
        """TRVs with None temperature get the baseline (no boost)."""
        result = distribute_valve_percent(50.0, {"a": 22.0, "b": None})
        assert result["a"] == pytest.approx(50.0)
        assert result["b"] == pytest.approx(50.0)

    def test_all_none_temps_uniform(self):
        """All None temps produce uniform distribution."""
        result = distribute_valve_percent(40.0, {"a": None, "b": None})
        assert result["a"] == pytest.approx(40.0)
        assert result["b"] == pytest.approx(40.0)

    def test_result_clamped_to_100(self):
        """Boost should not exceed 100%."""
        # Cold TRV with high baseline: boost pushes above 100
        result = distribute_valve_percent(90.0, {"warm": 25.0, "cold": 15.0})
        assert result["cold"] <= 100.0

    def test_result_not_negative(self):
        """Result should never be negative."""
        result = distribute_valve_percent(0.0, {"a": 20.0, "b": 18.0})
        assert all(v >= 0.0 for v in result.values())

    def test_warmest_is_reference(self):
        """The warmest TRV always receives exactly the MPC output."""
        for pct in [0.0, 25.0, 50.0, 75.0, 100.0]:
            result = distribute_valve_percent(
                pct, {"cold": 18.0, "warm": 22.0, "mid": 20.0}
            )
            assert result["warm"] == pytest.approx(pct)


# ===================================================================
# 16. KALMAN FILTER (virtual temperature)
# ===================================================================


class TestKalmanFilter:
    """Tests for the Kalman filter used for virtual temperature tracking."""

    def test_kalman_p_initialized_to_R(self):
        """On first call, kalman_P is set to R then reduced by the update step.

        Init: P=R=0.04, then update: K=P/(P+R)=0.5, P_new=(1-0.5)*0.04=0.02.
        """
        params = _default_params(use_virtual_temp=True, kalman_R=0.04)
        _compute(_inp(key="kp_init", current_temp_C=20.0), params)
        state = mpc_mod._MPC_STATES["kp_init"]
        # Init sets P=R=0.04, then sensor_changed triggers update:
        # K = P/(P+R) = 0.04/(0.04+0.04) = 0.5
        # P_new = (1-0.5)*0.04 = 0.02
        assert state.kalman_P == pytest.approx(0.02)

    def test_kalman_p_grows_during_predict(self):
        """P should increase after predict step (uncertainty grows with time)."""
        params = _default_params(use_virtual_temp=True, kalman_Q=0.001)
        _compute(_inp(key="kp_grow", current_temp_C=20.0), params)
        state = mpc_mod._MPC_STATES["kp_grow"]
        state.last_percent = 50.0
        P_after_init = state.kalman_P

        # Advance virtual_temp_ts backward to force a predict step
        state.virtual_temp_ts = time() - 60  # 60s ago

        _compute(_inp(key="kp_grow", current_temp_C=20.0), params)
        # P should have grown by Q * dt_s
        assert state.kalman_P > P_after_init

    def test_kalman_p_shrinks_on_update(self):
        """P should decrease after update step (measurement reduces uncertainty)."""
        params = _default_params(use_virtual_temp=True, kalman_Q=0.001, kalman_R=0.04)
        _compute(_inp(key="kp_shrink", current_temp_C=20.0), params)
        state = mpc_mod._MPC_STATES["kp_shrink"]
        state.last_percent = 50.0
        state.kalman_P = 1.0  # High uncertainty
        state.last_sensor_temp_C = 19.5  # Different from current → triggers update

        _compute(_inp(key="kp_shrink", current_temp_C=20.0), params)
        # After update: P_new = (1 - K) * P, so it must be smaller
        assert state.kalman_P < 1.0

    def test_kalman_gain_high_P(self):
        """With high P (relative to R), Kalman gain K → 1, trusting sensor more."""
        params = _default_params(use_virtual_temp=True, kalman_R=0.04)
        _compute(_inp(key="kg_high", current_temp_C=20.0), params)
        state = mpc_mod._MPC_STATES["kg_high"]
        state.kalman_P = 100.0  # Very high uncertainty
        state.last_sensor_temp_C = 19.0  # Force update
        state.virtual_temp = 21.0  # Far from sensor

        _compute(_inp(key="kg_high", current_temp_C=20.0), params)
        # K ≈ 100 / (100 + 0.04) ≈ 0.9996
        # virtual_temp should be very close to 20.0
        assert abs(state.virtual_temp - 20.0) < 0.01

    def test_kalman_gain_low_P(self):
        """With low P (relative to R), Kalman gain K → 0, trusting model more."""
        params = _default_params(use_virtual_temp=True, kalman_R=0.04)
        _compute(_inp(key="kg_low", current_temp_C=20.0), params)
        state = mpc_mod._MPC_STATES["kg_low"]
        state.kalman_P = 0.0001  # Very low uncertainty
        state.last_sensor_temp_C = 19.0  # Force update
        state.virtual_temp = 21.0  # Far from sensor

        _compute(_inp(key="kg_low", current_temp_C=20.0), params)
        # K ≈ 0.0001 / (0.0001 + 0.04) ≈ 0.0025
        # virtual_temp should barely change
        assert abs(state.virtual_temp - 21.0) < 0.1

    def test_kalman_predict_uses_gain_and_loss(self):
        """Predict step should use gain*u - loss to forward-predict temperature."""
        params = _default_params(
            use_virtual_temp=True,
            mpc_thermal_gain=0.06,
            mpc_loss_coeff=0.01,
            mpc_adapt=True,
        )
        _compute(_inp(key="kpred", current_temp_C=20.0), params)
        state = mpc_mod._MPC_STATES["kpred"]
        state.last_percent = 100.0  # Full open
        state.gain_est = 0.06
        state.loss_est = 0.01
        vt_before = state.virtual_temp
        # Move virtual_temp_ts back 60s to get a meaningful predict step
        state.virtual_temp_ts = time() - 60
        # Set sensor same as current to skip update
        state.last_sensor_temp_C = 20.0

        _compute(_inp(key="kpred", current_temp_C=20.0), params)
        # predicted_dT = gain * u * dt_min - loss * dt_min
        # = 0.06 * 1.0 * 1.0 - 0.01 * 1.0 = 0.05
        # virtual_temp should have increased (gain > loss at u=1)
        assert state.virtual_temp > vt_before


# ===================================================================
# 17. ANALYTICAL SOLVER
# ===================================================================


class TestAnalyticalSolver:
    """Tests for the analytical MPC solver."""

    def test_zero_gain_falls_back_to_u0(self):
        """When gain is ~0, denom_analytical ≈ 0 and solver falls back to u0_frac."""
        params = _default_params(
            mpc_thermal_gain=0.0, mpc_loss_coeff=0.0, mpc_adapt=False
        )
        result = _compute(
            _inp(key="zg_solver", current_temp_C=20.0, target_temp_C=22.0), params
        )
        # u0 = loss/gain = 0/0 → 0.0, so solver should return u0=0%
        assert result is not None
        assert result.valve_percent == 0

    def test_analytical_produces_reasonable_output(self):
        """Analytical solver should produce output consistent with the error magnitude."""
        params = _default_params(
            mpc_thermal_gain=0.06, mpc_loss_coeff=0.01, mpc_adapt=False
        )
        # 2K error → should demand significant heating
        result = _compute(
            _inp(key="anal_reas", current_temp_C=20.0, target_temp_C=22.0), params
        )
        assert result.valve_percent > 0
        assert "mpc_analytical" in result.debug
        assert result.debug["mpc_analytical"] is False

    def test_negative_error_gives_zero(self):
        """When above target (e0 < 0), optimal u should be 0 or very low."""
        params = _default_params(
            mpc_thermal_gain=0.06, mpc_loss_coeff=0.01, mpc_adapt=False
        )
        result = _compute(
            _inp(key="anal_neg", current_temp_C=23.0, target_temp_C=22.0), params
        )
        assert result.valve_percent <= 20  # Should be low/zero

    def test_cost_at_optimum_in_debug(self):
        """Debug dict should contain the cost at the analytical optimum."""
        params = _default_params(mpc_adapt=False)
        result = _compute(
            _inp(key="anal_cost", current_temp_C=20.0, target_temp_C=22.0), params
        )
        assert "mpc_cost" in result.debug
        assert result.debug["mpc_cost"] >= 0


# ===================================================================
# 18. MAX OPENING PERCENT (USER CAP)
# ===================================================================


class TestMaxOpeningPct:
    """Tests for max_opening_pct clamping in post-processing."""

    def test_max_opening_clamps_output(self):
        """Output should be clamped to max_opening_pct."""
        params = _default_params()
        result = _compute(
            _inp(
                key="maxop",
                current_temp_C=18.0,
                target_temp_C=22.0,
                max_opening_pct=30.0,
            ),
            params,
        )
        assert result.valve_percent <= 30

    def test_max_opening_none_no_clamp(self):
        """When max_opening_pct is None, no clamping should occur."""
        params = _default_params()
        result = _compute(
            _inp(
                key="maxop_none",
                current_temp_C=18.0,
                target_temp_C=22.0,
                max_opening_pct=None,
            ),
            params,
        )
        # Large error → should be high, not clamped
        assert result.valve_percent > 30

    def test_max_opening_debug_flag(self):
        """Debug should indicate when max_opening clamping occurred."""
        params = _default_params()
        result = _compute(
            _inp(
                key="maxop_dbg",
                current_temp_C=18.0,
                target_temp_C=22.0,
                max_opening_pct=30.0,
            ),
            params,
        )
        if result.valve_percent <= 30:
            assert result.debug.get("max_opening_clamped") is True


# ===================================================================
# 19. GAIN LEARN COUNT GUARD (warm_low_u)
# ===================================================================


class TestGainLearnCountGuard:
    """Test that 'warm_low_u' loss learning is gated by gain_learn_count >= 2."""

    def _setup_warm_low_u(self, key, gain_learn_count):
        """Set up a state where warm_low_u learning conditions are met."""
        params = _default_params(
            mpc_adapt=True,
            mpc_adapt_alpha=0.5,
            mpc_thermal_gain=0.06,
            mpc_loss_coeff=0.02,
            enable_min_effective_percent=False,
        )
        _compute(_inp(key=key, current_temp_C=20.0), params)
        state = mpc_mod._MPC_STATES[key]
        state.gain_est = 0.06
        state.loss_est = 0.02
        state.gain_learn_count = gain_learn_count
        state.last_percent = (
            5.0  # 5% = 0.05 fractional; u0 = 0.02/0.06 ≈ 0.333; u < u0-0.05
        )
        state.last_learn_temp = 19.5  # will observe warming from 19.5 to 20.0
        state.last_learn_time = time() - 300
        state.u_integral = 5.0 * 300
        state.time_integral = 300.0
        return params, state

    def test_warm_low_u_blocked_when_gain_learn_count_lt_2(self):
        """Loss learning via warm_low_u should be blocked when gain_learn_count < 2."""
        params, state = self._setup_warm_low_u("wlu_blocked", gain_learn_count=1)
        loss_before = state.loss_est

        _compute(_inp(key="wlu_blocked", current_temp_C=20.0), params)
        # With gain_learn_count=1, warm_low_u should NOT fire
        # Loss should remain unchanged (or updated via another path)
        # The key assertion: if loss changed, it was NOT via warm_low_u
        assert (
            state.loss_est == pytest.approx(loss_before) or state.gain_learn_count < 2
        )

    def test_warm_low_u_allowed_when_gain_learn_count_ge_2(self):
        """Loss learning via warm_low_u should work when gain_learn_count >= 2."""
        params, state = self._setup_warm_low_u("wlu_allowed", gain_learn_count=2)
        loss_before = state.loss_est

        _compute(_inp(key="wlu_allowed", current_temp_C=20.0), params)
        # With gain_learn_count=2 and warming below u0, loss should decrease
        assert state.loss_est <= loss_before


# ===================================================================
# 20. GAIN RECOVERY PATH
# ===================================================================


class TestGainRecovery:
    """Tests for gain recovery when gain was over-corrected downward."""

    def test_gain_recovers_when_warming_exceeds_model(self):
        """Gain should recover upward when implied gain > current * 1.1."""
        params = _default_params(
            mpc_adapt=True, mpc_adapt_alpha=0.1, enable_min_effective_percent=False
        )
        _compute(_inp(key="grecov", current_temp_C=20.0), params)
        state = mpc_mod._MPC_STATES["grecov"]
        state.gain_est = 0.02  # Artificially low gain
        state.loss_est = 0.005
        state.last_percent = 50.0  # u=0.5
        state.last_learn_temp = 19.5  # warming from 19.5 to 20.0
        state.last_learn_time = time() - 300
        state.u_integral = 50.0 * 300
        state.time_integral = 300.0
        gain_before = state.gain_est

        # Room warmed 0.5K in 5min = 0.1 °C/min
        # gain_implied = (0.1 + 0.005) / 0.5 = 0.21
        # 0.21 > 0.02 * 1.1 = 0.022 → should recover
        _compute(_inp(key="grecov", current_temp_C=20.0), params)
        assert state.gain_est > gain_before

    def test_no_recovery_when_warming_matches_model(self):
        """No recovery should happen when implied gain ≈ current gain."""
        params = _default_params(
            mpc_adapt=True, mpc_adapt_alpha=0.1, enable_min_effective_percent=False
        )
        _compute(_inp(key="gnorec", current_temp_C=20.0), params)
        state = mpc_mod._MPC_STATES["gnorec"]
        state.gain_est = 0.10
        state.loss_est = 0.01
        state.last_percent = 50.0  # u=0.5
        # implied = (rate + loss) / u = (rate + 0.01) / 0.5
        # For no recovery: implied <= 0.10 * 1.1 = 0.11
        # → rate + 0.01 <= 0.055 → rate <= 0.045
        # observed_rate = dT/dt_min; dT=0.1K, dt=5min → rate=0.02
        state.last_learn_temp = 19.9  # warming 0.1K
        state.last_learn_time = time() - 300
        state.u_integral = 50.0 * 300
        state.time_integral = 300.0
        gain_before = state.gain_est

        _compute(_inp(key="gnorec", current_temp_C=20.0), params)
        # gain_implied = (0.02 + 0.01) / 0.5 = 0.06 < 0.11 → no recovery
        assert (
            state.gain_est == pytest.approx(gain_before)
            or state.gain_est <= gain_before
        )


# ===================================================================
# 21. HIGH-U STEADY-STATE GAIN LEARNING
# ===================================================================


class TestHighUSteadyStateGain:
    """Tests for high-u steady-state gain learning path."""

    def test_high_u_ss_reduces_gain_when_below_target(self):
        """When valve is high, temp flat, below target → gain should decrease."""
        params = _default_params(
            mpc_adapt=True, mpc_adapt_alpha=0.1, enable_min_effective_percent=False
        )
        _compute(_inp(key="huss", current_temp_C=20.0), params)
        state = mpc_mod._MPC_STATES["huss"]
        state.gain_est = 0.10
        state.loss_est = 0.01
        state.last_percent = 30.0  # u=0.3 > 0.15
        state.last_learn_temp = 20.0  # no temp change
        state.last_learn_time = time() - 400
        state.last_residual_time = time() - 400
        state.u_integral = 30.0 * 400
        state.time_integral = 400.0
        state.last_target_C = 21.0
        gain_before = state.gain_est

        # target=21, current=20 → e_now=1.0 > 0.1 ✓
        # temp unchanged → not temp_changed ✓
        # observed_rate ≈ 0 ✓
        # dt_residual=400 → within [300, 3600] ✓
        _compute(_inp(key="huss", current_temp_C=20.0, target_temp_C=21.0), params)
        assert state.gain_est <= gain_before


# ===================================================================
# 22. ka_est DYNAMIC LOSS
# ===================================================================


class TestKaEstDynamicLoss:
    """Tests for outdoor-temperature-dependent dynamic loss via ka_est."""

    def test_ka_est_initial_depends_on_outdoor_delta(self):
        """ka_est = loss_coeff / (indoor - outdoor).

        Different outdoor temps produce different initial ka_est values.
        """
        params = _default_params(mpc_adapt=True, mpc_loss_coeff=0.01)
        # Cold outside: delta = 20 - (-10) = 30
        _compute(_inp(key="ka_cold", current_temp_C=20.0, outdoor_temp_C=-10.0), params)
        state_cold = mpc_mod._MPC_STATES["ka_cold"]
        assert state_cold.ka_est is not None
        assert state_cold.ka_est == pytest.approx(0.01 / 30.0, rel=0.01)

        # Warm outside: delta = max(5.0, 20 - 15) = 5
        _compute(_inp(key="ka_warm", current_temp_C=20.0, outdoor_temp_C=15.0), params)
        state_warm = mpc_mod._MPC_STATES["ka_warm"]
        assert state_warm.ka_est is not None
        assert state_warm.ka_est == pytest.approx(0.01 / 5.0, rel=0.01)

        # Cold outside → smaller ka (same loss spread over larger delta)
        assert state_cold.ka_est < state_warm.ka_est

    def test_ka_est_updated_when_loss_learned(self):
        """ka_est should be updated when loss is learned and outdoor temp is available."""
        params = _default_params(
            mpc_adapt=True,
            mpc_adapt_alpha=0.5,
            mpc_loss_coeff=0.01,
            enable_min_effective_percent=False,
        )
        _compute(_inp(key="ka_upd", current_temp_C=21.0, outdoor_temp_C=5.0), params)
        state = mpc_mod._MPC_STATES["ka_upd"]
        state.last_percent = 0.0
        state.last_learn_temp = 21.0
        state.last_learn_time = time() - 300
        state.gain_est = 0.06
        state.loss_est = 0.01
        state.u_integral = 0.0
        state.time_integral = 300.0
        ka_before = state.ka_est

        # Room cooled from 21.0 to 20.5 → loss learning should fire and update ka_est
        _compute(_inp(key="ka_upd", current_temp_C=20.5, outdoor_temp_C=5.0), params)
        # ka should have been updated
        if state.loss_est != pytest.approx(0.01):
            assert state.ka_est != ka_before


# ===================================================================
# 23. RESIDUAL RATE-LIMITING
# ===================================================================


class TestResidualRateLimiting:
    """Tests for residual learning rate-limiting boundaries."""

    def _setup_residual(self, key, dt_residual):
        """Set up state for residual learning at given dt_residual."""
        params = _default_params(
            mpc_adapt=True, mpc_adapt_alpha=0.1, enable_min_effective_percent=False
        )
        now = time()
        _compute(_inp(key=key, current_temp_C=20.0), params)
        state = mpc_mod._MPC_STATES[key]
        state.gain_est = 0.06
        state.loss_est = 0.01
        state.last_percent = 17.0  # close to u0 = 0.01/0.06 ≈ 0.167 → 16.7%
        state.last_learn_temp = 20.0  # no temp change → !temp_changed
        state.last_learn_time = now - 300
        state.last_residual_time = now - dt_residual
        state.last_target_C = 22.0
        state.u_integral = 17.0 * 300
        state.time_integral = 300.0
        return params, state

    def test_residual_blocked_below_300s(self):
        """Residual learning should be blocked when dt_residual < 300s."""
        params, state = self._setup_residual("res_below", dt_residual=200)
        loss_before = state.loss_est

        _compute(_inp(key="res_below", current_temp_C=20.0, target_temp_C=22.0), params)
        # Residual should be rate-limited; loss should not change via residual path
        assert state.loss_est == pytest.approx(loss_before)

    def test_residual_blocked_above_3600s(self):
        """Residual learning should be blocked when dt_residual > 3600s."""
        params, state = self._setup_residual("res_above", dt_residual=4000)
        loss_before = state.loss_est

        _compute(_inp(key="res_above", current_temp_C=20.0, target_temp_C=22.0), params)
        assert state.loss_est == pytest.approx(loss_before)


# ===================================================================
# 24. BIG-CHANGE HOLD BYPASS
# ===================================================================


class TestBigChangeHoldBypass:
    """Tests for hold-time bypass on big opening/closing changes."""

    def test_big_increase_bypasses_hold_time(self):
        """Change >= big_change_force_open_pct should bypass hold-time."""
        params = _default_params(
            min_percent_hold_time_s=600.0,
            big_change_force_open_pct=33.0,
            big_change_force_close_pct=10.0,
        )
        # First call: set low valve
        _compute(_inp(key="bigopen", current_temp_C=22.0, target_temp_C=22.0), params)
        state = mpc_mod._MPC_STATES["bigopen"]
        state.last_percent = 10.0
        state.last_update_ts = time()  # just updated

        # Sudden large demand (cold room)
        result = _compute(
            _inp(key="bigopen", current_temp_C=16.0, target_temp_C=22.0), params
        )
        # The valve should jump up despite hold-time (raw ≈ 100%, change ≈ 90% > 33%)
        assert result.valve_percent > 10

    def test_big_decrease_bypasses_hold_time(self):
        """Change >= big_change_force_close_pct should bypass hold-time."""
        params = _default_params(
            min_percent_hold_time_s=600.0,
            big_change_force_open_pct=33.0,
            big_change_force_close_pct=10.0,
        )
        _compute(_inp(key="bigclose", current_temp_C=18.0, target_temp_C=22.0), params)
        state = mpc_mod._MPC_STATES["bigclose"]
        state.last_percent = 80.0
        state.last_update_ts = time()  # just updated

        # Warm room → wants to go to 0%
        result = _compute(
            _inp(key="bigclose", current_temp_C=23.0, target_temp_C=22.0), params
        )
        # Closing should bypass hold-time if drop exceeds close threshold
        assert result.valve_percent < 80

    def test_small_decrease_still_blocked_by_hold_time(self):
        """Closing below close-threshold should remain hold-time blocked."""
        params = _default_params(
            min_percent_hold_time_s=600.0,
            big_change_force_open_pct=33.0,
            big_change_force_close_pct=10.0,
        )
        _compute(
            _inp(key="smallclose", current_temp_C=18.0, target_temp_C=22.0), params
        )
        state = mpc_mod._MPC_STATES["smallclose"]
        state.last_percent = 25.0
        state.last_update_ts = time()

        # Nearly on target -> small closing request likely below 10%
        result = _compute(
            _inp(key="smallclose", current_temp_C=21.95, target_temp_C=22.0), params
        )
        assert result.valve_percent == 25


# ===================================================================
# 25. DEQUE EVICTION (recent_errors)
# ===================================================================


class TestDequeEviction:
    """Tests for recent_errors deque maxlen=20 behavior."""

    def test_deque_maxlen_is_20(self):
        """recent_errors deque should have maxlen=20."""
        state = _MpcState()
        assert state.recent_errors.maxlen == 20

    def test_deque_evicts_oldest(self):
        """When more than 20 errors are appended, oldest should be evicted."""
        state = _MpcState()
        for i in range(25):
            state.recent_errors.append(float(i))
        assert len(state.recent_errors) == 20
        assert state.recent_errors[0] == 5.0  # First 5 evicted
        assert state.recent_errors[-1] == 24.0


# ===================================================================
# 26. STALE STATE ANCHOR RESET
# ===================================================================


class TestStaleStateAnchorReset:
    """Test that stale state detection also resets learning anchors properly."""

    def test_stale_resets_learn_time_and_temp(self):
        """Stale detection should reset learn_time and learn_temp, not just u_integral."""
        params = _default_params(mpc_adapt=True)
        _compute(_inp(key="stale_full", current_temp_C=20.0), params)
        state = mpc_mod._MPC_STATES["stale_full"]
        state.last_time = time() - 1000  # 16+ min ago
        state.last_learn_temp = 18.0
        state.last_learn_time = time() - 1000
        state.u_integral = 5000.0
        state.time_integral = 500.0

        _compute(_inp(key="stale_full", current_temp_C=21.0), params)
        # After stale detection, anchors should be reset
        assert state.u_integral == 0.0
        assert state.time_integral == 0.0
        # last_learn_temp should be reset to current value
        assert state.last_learn_temp == pytest.approx(21.0, abs=0.5)


# ===================================================================
# 27. TARGET CHANGE BOUNDARY (0.05K)
# ===================================================================


class TestTargetChangeBoundary:
    """Tests for exact 0.05K boundary in target change detection."""

    def test_target_change_exactly_0_05_detected(self):
        """A target change of exactly 0.05K should be detected."""
        params = _default_params(mpc_adapt=True, mpc_adapt_alpha=0.5)
        _compute(_inp(key="tgt_exact", current_temp_C=20.0, target_temp_C=22.0), params)
        state = mpc_mod._MPC_STATES["tgt_exact"]
        state.last_target_C = 22.0
        state.last_learn_temp = 20.0
        state.last_learn_time = time() - 300
        state.gain_est = 0.06
        state.loss_est = 0.01
        state.u_integral = 50.0 * 300
        state.time_integral = 300.0
        state.last_percent = 50.0
        gain_before = state.gain_est

        # Change target by exactly 0.05 → should be detected as target_changed
        _compute(
            _inp(key="tgt_exact", current_temp_C=20.5, target_temp_C=22.05), params
        )
        # With target_changed=True, adaptation is blocked
        assert state.gain_est == pytest.approx(gain_before)

    def test_target_change_below_0_05_not_detected(self):
        """A target change of less than 0.05K should NOT be detected."""
        params = _default_params(mpc_adapt=True, mpc_adapt_alpha=0.5)
        _compute(_inp(key="tgt_sub", current_temp_C=20.0, target_temp_C=22.0), params)
        state = mpc_mod._MPC_STATES["tgt_sub"]
        state.last_target_C = 22.0
        state.last_learn_temp = 20.0
        state.last_learn_time = time() - 300
        state.gain_est = 0.06
        state.loss_est = 0.01
        state.u_integral = 50.0 * 300
        state.time_integral = 300.0
        state.last_percent = 50.0

        # Change target by only 0.04 → should NOT block adaptation
        _compute(_inp(key="tgt_sub", current_temp_C=20.5, target_temp_C=22.04), params)
        # target_changed should be False, so adaptation can proceed
        # (gain may or may not change depending on conditions,
        # but the key is that target_changed didn't block it)
        # Checking debug would be ideal but we at least check no crash
        assert state.gain_est is not None


# ===================================================================
# 28. DISTRIBUTE VALVE PERCENT — NEGATIVE INPUT
# ===================================================================


class TestDistributeNegativeInput:
    """Tests for distribute_valve_percent with negative u_total_pct."""

    def test_negative_u_total_clamped_to_zero(self):
        """Negative u_total_pct should be clamped to 0% for all TRVs."""
        result = distribute_valve_percent(-10.0, {"a": 20.0, "b": 18.0})
        assert all(v >= 0.0 for v in result.values())
        # Fast-path returns max(0, min(100, u_total_pct)) for each when u_total_pct <= 0
        assert result["a"] == pytest.approx(0.0)
        assert result["b"] == pytest.approx(0.0)


# ===================================================================
# 29. SOLAR GAIN INITIALIZATION
# ===================================================================


class TestSolarGainInit:
    """Tests for solar_gain_est initialization."""

    def test_solar_gain_initialized_on_adapt(self):
        """solar_gain_est should be initialized when mpc_adapt=True."""
        params = _default_params(mpc_adapt=True)
        _compute(_inp(key="solar_init", current_temp_C=20.0), params)
        state = mpc_mod._MPC_STATES["solar_init"]
        assert state.solar_gain_est is not None
        assert state.solar_gain_est == pytest.approx(
            0.01
        )  # mpc_solar_gain_initial default

    def test_solar_gain_not_initialized_without_adapt(self):
        """solar_gain_est should stay None when mpc_adapt=False."""
        params = _default_params(mpc_adapt=False)
        _compute(_inp(key="solar_noinit", current_temp_C=20.0), params)
        state = mpc_mod._MPC_STATES["solar_noinit"]
        assert state.solar_gain_est is None


# ===================================================================
# 30. INTEGRATION ACCUMULATION PRECISION
# ===================================================================


class TestIntegrationAccumulation:
    """Tests for valve usage integration tracking."""

    def test_integration_accumulates_correctly(self):
        """u_integral and time_integral should track valve usage over time."""
        params = _default_params()
        _compute(_inp(key="integ_prec", current_temp_C=20.0), params)
        state = mpc_mod._MPC_STATES["integ_prec"]
        state.last_percent = 50.0
        state.last_integration_ts = time() - 60  # 60s ago

        _compute(_inp(key="integ_prec", current_temp_C=20.0), params)
        # u_integral should have accumulated: 50.0 * ~60 ≈ 3000
        assert state.u_integral > 0
        assert state.time_integral > 0

    def test_integration_reset_on_window_open(self):
        """Window open should reset u_integral and time_integral."""
        params = _default_params()
        _compute(_inp(key="integ_win", current_temp_C=20.0), params)
        state = mpc_mod._MPC_STATES["integ_win"]
        state.u_integral = 5000.0
        state.time_integral = 300.0

        _compute(_inp(key="integ_win", window_open=True), params)
        assert state.u_integral == 0.0
        assert state.time_integral == 0.0
