"""Tests for import_mpc_state_map handling of string and int fields."""

from __future__ import annotations

import pytest

from custom_components.better_thermostat.utils.calibration import mpc as mpc_mod
from custom_components.better_thermostat.utils.calibration.mpc import (
    _MpcState,
    export_mpc_state_map,
    import_mpc_state_map,
)


@pytest.fixture(autouse=True)
def _clean_mpc_states():
    """Reset global MPC state before every test."""
    mpc_mod._MPC_STATES.clear()
    yield
    mpc_mod._MPC_STATES.clear()


class TestImportStringFields:
    """Tests for correct type coercion in import_mpc_state_map."""

    def test_trv_profile_survives_round_trip(self):
        """trv_profile should be preserved as a string after export/import."""
        state = _MpcState()
        state.trv_profile = "threshold"
        state.gain_est = 0.08
        mpc_mod._MPC_STATES["k1"] = state

        exported = export_mpc_state_map()
        mpc_mod._MPC_STATES.clear()
        import_mpc_state_map(exported)

        restored = mpc_mod._MPC_STATES["k1"]
        assert restored.trv_profile == "threshold"
        assert restored.gain_est == pytest.approx(0.08)

    def test_trv_profile_unknown_survives_round_trip(self):
        """Default trv_profile 'unknown' should also survive round-trip."""
        state = _MpcState()
        state.trv_profile = "unknown"
        mpc_mod._MPC_STATES["k2"] = state

        exported = export_mpc_state_map()
        mpc_mod._MPC_STATES.clear()
        import_mpc_state_map(exported)

        assert mpc_mod._MPC_STATES["k2"].trv_profile == "unknown"

    def test_trv_profile_all_known_values(self):
        """All known trv_profile values should survive round-trip."""
        for profile in ("unknown", "linear", "threshold", "exponential"):
            mpc_mod._MPC_STATES.clear()
            state = _MpcState()
            state.trv_profile = profile
            mpc_mod._MPC_STATES["k"] = state

            exported = export_mpc_state_map()
            mpc_mod._MPC_STATES.clear()
            import_mpc_state_map(exported)

            assert mpc_mod._MPC_STATES["k"].trv_profile == profile

    def test_profile_samples_survives_round_trip(self):
        """profile_samples (int) should survive export/import."""
        state = _MpcState()
        state.profile_samples = 42
        mpc_mod._MPC_STATES["k3"] = state

        exported = export_mpc_state_map()
        mpc_mod._MPC_STATES.clear()
        import_mpc_state_map(exported)

        assert mpc_mod._MPC_STATES["k3"].profile_samples == 42

    def test_is_calibration_active_survives_round_trip(self):
        """is_calibration_active (bool) should survive export/import."""
        state = _MpcState()
        state.is_calibration_active = True
        mpc_mod._MPC_STATES["k4"] = state

        exported = export_mpc_state_map()
        mpc_mod._MPC_STATES.clear()
        import_mpc_state_map(exported)

        assert mpc_mod._MPC_STATES["k4"].is_calibration_active is True

    def test_loss_learn_count_survives_round_trip(self):
        """loss_learn_count (int) should survive export/import."""
        state = _MpcState()
        state.loss_learn_count = 15
        mpc_mod._MPC_STATES["k5"] = state

        exported = export_mpc_state_map()
        mpc_mod._MPC_STATES.clear()
        import_mpc_state_map(exported)

        assert mpc_mod._MPC_STATES["k5"].loss_learn_count == 15

    def test_regime_boost_active_survives_round_trip(self):
        """regime_boost_active (bool) should survive export/import."""
        state = _MpcState()
        state.regime_boost_active = True
        mpc_mod._MPC_STATES["k_rb"] = state

        exported = export_mpc_state_map()
        mpc_mod._MPC_STATES.clear()
        import_mpc_state_map(exported)

        assert mpc_mod._MPC_STATES["k_rb"].regime_boost_active is True

    def test_consecutive_insufficient_heat_survives_round_trip(self):
        """consecutive_insufficient_heat (int) should survive export/import."""
        state = _MpcState()
        state.consecutive_insufficient_heat = 5
        mpc_mod._MPC_STATES["k_cih"] = state

        exported = export_mpc_state_map()
        mpc_mod._MPC_STATES.clear()
        import_mpc_state_map(exported)

        restored = mpc_mod._MPC_STATES["k_cih"]
        assert restored.consecutive_insufficient_heat == 5
        assert isinstance(restored.consecutive_insufficient_heat, int)

    def test_perf_curve_survives_round_trip(self):
        """perf_curve (dict) should survive export/import."""
        state = _MpcState()
        state.perf_curve = {"p00_10": {"rate": 0.05, "count": 3}}
        mpc_mod._MPC_STATES["k_pc"] = state

        exported = export_mpc_state_map()
        mpc_mod._MPC_STATES.clear()
        import_mpc_state_map(exported)

        restored = mpc_mod._MPC_STATES["k_pc"]
        assert isinstance(restored.perf_curve, dict)
        assert "p00_10" in restored.perf_curve
        assert restored.perf_curve["p00_10"]["rate"] == pytest.approx(0.05)
        assert restored.perf_curve["p00_10"]["count"] == 3

    def test_full_state_round_trip(self):
        """All field types should survive a full export/import round-trip."""
        state = _MpcState()
        state.gain_est = 0.08
        state.loss_est = 0.015
        state.last_percent = 42.0
        state.min_effective_percent = 12.0
        state.dead_zone_hits = 3
        state.is_calibration_active = True
        state.trv_profile = "threshold"
        state.profile_confidence = 0.85
        state.profile_samples = 10
        state.loss_learn_count = 7
        state.regime_boost_active = True
        state.consecutive_insufficient_heat = 3
        state.perf_curve = {"p50_60": {"rate": 0.1, "count": 5}}
        mpc_mod._MPC_STATES["k6"] = state

        exported = export_mpc_state_map()
        mpc_mod._MPC_STATES.clear()
        import_mpc_state_map(exported)

        restored = mpc_mod._MPC_STATES["k6"]
        assert restored.gain_est == pytest.approx(0.08)
        assert restored.loss_est == pytest.approx(0.015)
        assert restored.last_percent == pytest.approx(42.0)
        assert restored.min_effective_percent == pytest.approx(12.0)
        assert restored.dead_zone_hits == 3
        assert restored.is_calibration_active is True
        assert restored.trv_profile == "threshold"
        assert restored.profile_confidence == pytest.approx(0.85)
        assert restored.profile_samples == 10
        assert restored.loss_learn_count == 7
        assert restored.regime_boost_active is True
        assert restored.consecutive_insufficient_heat == 3
        assert "p50_60" in restored.perf_curve
