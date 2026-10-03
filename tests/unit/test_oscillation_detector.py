"""The oscillation detector flags sustained output thrash — and only that.

Annunciation only: a false positive must stay cheap, so the detector
errs toward quiet. Backoff is a manual decision until the detector is
validated against the calibration benchmark.
"""

from custom_components.better_thermostat.core.calibrator import detect_oscillation


def test_sustained_full_swings_are_flagged():
    """0/100 thrash over the window is the textbook positive."""
    outputs = [0.0, 100.0, 0.0, 100.0, 0.0, 100.0, 0.0]
    assert detect_oscillation(outputs) is True


def test_a_monotonic_ramp_is_quiet():
    """Steady convergence has no reversals."""
    outputs = [0.0, 20.0, 40.0, 60.0, 80.0, 100.0, 100.0, 100.0]
    assert detect_oscillation(outputs) is False


def test_small_flutter_is_quiet():
    """Reversals below the swing threshold are normal modulation."""
    outputs = [50.0, 55.0, 48.0, 53.0, 47.0, 52.0, 46.0, 51.0]
    assert detect_oscillation(outputs) is False


def test_a_short_history_is_quiet():
    """Two swings are a correction, not an oscillation."""
    outputs = [0.0, 100.0, 0.0]
    assert detect_oscillation(outputs) is False


def test_only_the_recent_window_counts():
    """Old thrash followed by a long settled stretch is quiet."""
    outputs = [0.0, 100.0, 0.0, 100.0, 0.0, 100.0] + [50.0] * 10
    assert detect_oscillation(outputs) is False


def test_an_empty_history_is_quiet():
    """No data, no alarm."""
    assert detect_oscillation([]) is False
