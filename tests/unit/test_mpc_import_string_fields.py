"""Tests for deserialize_mpc handling of string, int, and bool fields.

MPC state is persisted through the StateManager; ``deserialize_mpc`` is the
deserializer applied on load and must coerce JSON-typed payloads back into
the proper field types.
"""

from __future__ import annotations

from dataclasses import asdict

import pytest

from custom_components.better_thermostat.utils.calibration.mpc import _MpcState
from custom_components.better_thermostat.utils.state_manager import deserialize_mpc


def _round_trip(state: _MpcState) -> _MpcState:
    """Serialize a state like the StateManager does and deserialize it again."""
    return deserialize_mpc(asdict(state))


class TestImportStringFields:
    """Tests for correct type coercion in deserialize_mpc."""

    def test_trv_profile_survives_round_trip(self):
        """trv_profile should be preserved as a string after a round-trip."""
        state = _MpcState()
        state.trv_profile = "threshold"
        state.gain_est = 0.08

        restored = _round_trip(state)
        assert restored.trv_profile == "threshold"
        assert restored.gain_est == pytest.approx(0.08)

    def test_trv_profile_unknown_survives_round_trip(self):
        """Default trv_profile 'unknown' should also survive a round-trip."""
        state = _MpcState()
        state.trv_profile = "unknown"

        assert _round_trip(state).trv_profile == "unknown"

    def test_trv_profile_all_known_values(self):
        """All known trv_profile values should survive a round-trip."""
        for profile in ("unknown", "linear", "threshold", "exponential"):
            state = _MpcState()
            state.trv_profile = profile

            assert _round_trip(state).trv_profile == profile

    def test_profile_samples_survives_round_trip(self):
        """profile_samples (int) should survive a round-trip."""
        state = _MpcState()
        state.profile_samples = 42

        assert _round_trip(state).profile_samples == 42

    def test_is_calibration_active_survives_round_trip(self):
        """is_calibration_active (bool) should survive a round-trip."""
        state = _MpcState()
        state.is_calibration_active = True

        assert _round_trip(state).is_calibration_active is True

    def test_loss_learn_count_survives_round_trip(self):
        """loss_learn_count (int) should survive a round-trip."""
        state = _MpcState()
        state.loss_learn_count = 15

        assert _round_trip(state).loss_learn_count == 15

    def test_regime_boost_active_survives_round_trip(self):
        """regime_boost_active (bool) should survive a round-trip."""
        state = _MpcState()
        state.regime_boost_active = True

        assert _round_trip(state).regime_boost_active is True

    def test_consecutive_insufficient_heat_survives_round_trip(self):
        """consecutive_insufficient_heat (int) should survive a round-trip."""
        state = _MpcState()
        state.consecutive_insufficient_heat = 5

        restored = _round_trip(state)
        assert restored.consecutive_insufficient_heat == 5
        assert isinstance(restored.consecutive_insufficient_heat, int)

    def test_string_payload_values_are_coerced(self):
        """JSON payloads with string-typed numbers are coerced on load."""
        restored = deserialize_mpc(
            {
                "gain_est": "0.07",
                "profile_samples": "42",
                "loss_learn_count": "7",
                "trv_profile": "linear",
            }
        )
        assert restored.gain_est == pytest.approx(0.07)
        assert restored.profile_samples == 42
        assert restored.loss_learn_count == 7
        assert restored.trv_profile == "linear"

    def test_perf_curve_survives_round_trip(self):
        """perf_curve (dict) should survive a round-trip."""
        state = _MpcState()
        state.perf_curve = {"p00_10": {"rate": 0.05, "count": 3}}

        restored = _round_trip(state)
        assert isinstance(restored.perf_curve, dict)
        assert "p00_10" in restored.perf_curve
        assert restored.perf_curve["p00_10"]["rate"] == pytest.approx(0.05)
        assert restored.perf_curve["p00_10"]["count"] == 3

    def test_full_state_round_trip(self):
        """All field types should survive a full round-trip."""
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

        restored = _round_trip(state)
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
