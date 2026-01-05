import math

import pytest

from custom_components.better_thermostat.events import temperature as temp_events


class DummyBT:
    """Dummy BetterThermostat instance for testing."""

    def __init__(self):
        self.device_name = "dummy"
        self.external_temp_ema_tau_s = 900.0
        self.external_temp_ema = None
        self._external_temp_ema_ts = None
        self.cur_temp_filtered = None


def test_external_temp_ema_initializes(monkeypatch):
    """Test that external temperature EMA initializes correctly on first reading."""
    bt = DummyBT()

    monkeypatch.setattr(temp_events, "monotonic", lambda: 100.0)
    ema = temp_events._update_external_temp_ema(bt, 20.0)

    assert ema == 20.0
    assert bt.external_temp_ema == 20.0
    assert bt.cur_temp_filtered == 20.0


def test_external_temp_ema_time_based(monkeypatch):
    """Test that external temperature EMA applies time-based smoothing."""
    bt = DummyBT()

    # First sample
    monkeypatch.setattr(temp_events, "monotonic", lambda: 100.0)
    temp_events._update_external_temp_ema(bt, 20.0)

    # Second sample after 900s with tau=900s -> alpha = 1-exp(-1)
    monkeypatch.setattr(temp_events, "monotonic", lambda: 1000.0)
    ema = temp_events._update_external_temp_ema(bt, 21.0)

    alpha = 1.0 - math.exp(-1.0)
    expected = 20.0 + alpha * (21.0 - 20.0)

    assert ema == pytest.approx(expected, rel=1e-6, abs=1e-6)
    assert bt.external_temp_ema == pytest.approx(expected, rel=1e-6, abs=1e-6)
    assert bt.cur_temp_filtered == round(expected, 2)


def test_external_temp_ema_zero_dt_no_change(monkeypatch):
    """Test that EMA does not change when time delta is zero."""
    bt = DummyBT()

    monkeypatch.setattr(temp_events, "monotonic", lambda: 100.0)
    temp_events._update_external_temp_ema(bt, 20.0)

    # Same timestamp => alpha=0
    monkeypatch.setattr(temp_events, "monotonic", lambda: 100.0)
    ema = temp_events._update_external_temp_ema(bt, 30.0)

    assert ema == 20.0
    assert bt.cur_temp_filtered == 20.0
