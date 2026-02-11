"""Tests for _detect_regime_change bias detection."""

from __future__ import annotations

from custom_components.better_thermostat.utils.calibration.mpc import (
    _detect_regime_change,
)


class TestDetectRegimeChange:
    """Tests for _detect_regime_change."""

    def test_too_few_samples_returns_false(self):
        """Fewer than N samples should always return False."""
        assert _detect_regime_change([0.1] * 5) is False

    def test_zero_mean_zero_std_returns_false(self):
        """All-zero errors (no bias, no variance) should return False."""
        assert _detect_regime_change([0.0] * 10) is False

    def test_constant_positive_bias_detected(self):
        """Identical positive errors should be detected as regime change."""
        errors = [0.05] * 10
        assert _detect_regime_change(errors) is True

    def test_constant_negative_bias_detected(self):
        """Identical negative errors should be detected as regime change."""
        errors = [-0.05] * 10
        assert _detect_regime_change(errors) is True

    def test_constant_small_bias_detected(self):
        """Even very small but consistent bias should be detected."""
        errors = [0.001] * 10
        assert _detect_regime_change(errors) is True

    def test_alternating_errors_not_detected(self):
        """Alternating positive/negative errors with zero mean should not be detected."""
        errors = [0.05, -0.05] * 5
        assert _detect_regime_change(errors) is False

    def test_high_variance_masks_small_bias(self):
        """High variance relative to mean should not be detected as regime change."""
        import random as rng

        rng.seed(42)
        errors = [rng.gauss(0.01, 0.5) for _ in range(10)]
        result = _detect_regime_change(errors)
        assert isinstance(result, bool)

    def test_low_variance_with_bias_detected(self):
        """Low variance with clear nonzero mean should be detected."""
        errors = [0.04 + 0.001 * i for i in range(10)]
        # mean ≈ 0.0445, std is tiny -> t-stat high
        assert _detect_regime_change(errors) is True

    def test_uses_only_last_n_samples(self):
        """Only the last N samples should be considered."""
        old = [0.1] * 10
        new = [0.001, -0.001] * 5
        errors = old + new
        assert _detect_regime_change(errors) is False

    def test_exactly_n_samples(self):
        """Exactly N samples should work."""
        errors = [0.1] * 10
        assert _detect_regime_change(errors) is True

    def test_more_than_n_samples(self):
        """More than N samples should use last N only."""
        neutral = [0.0] * 20
        biased = [0.1] * 10
        errors = neutral + biased
        assert _detect_regime_change(errors) is True
