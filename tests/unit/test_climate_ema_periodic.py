"""Branch coverage for BetterThermostat._async_update_ema_periodic.

The periodic EMA tick keeps the external-temperature filter converging when the
sensor is silent, and derives a temperature slope from the EMA change.  These
tests pin the skip conditions, the slope math and which ticks write the state.
"""

from functools import partial
from unittest.mock import MagicMock, patch

import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.core.clock import FakeClock
from tests.factories import ThermostatStandIn

_CLIMATE = "custom_components.better_thermostat.climate"
_EMA = f"{_CLIMATE}._update_room_temperature_ema"


@pytest.fixture
def bt():
    """Minimal BetterThermostat mock for the periodic EMA tick."""
    mock = ThermostatStandIn()
    mock.device_name = "Test BT"
    mock.startup_running = False
    mock.last_known_external_temperature = 20.0
    mock.room_temperature_ema = None
    mock._slope_periodic_last_ts = None
    mock.temperature_slope = None
    mock.room_temperature_filtered = None
    mock._minute_tick_attributes = partial(
        BetterThermostat._minute_tick_attributes, mock
    )
    mock.async_write_ha_state = MagicMock()
    mock.clock = FakeClock()
    return mock


def _filter_update(ema: float):
    """Return an EMA update that publishes ``ema`` the way the real one does."""

    def update(bt, _raw: float) -> float:
        bt.room_temperature_filtered = round(ema, 2)
        return ema

    return update


@pytest.mark.asyncio
async def test_skips_while_startup_running(bt):
    """During startup the tick is a no-op (no EMA update, no state write)."""
    bt.startup_running = True
    with patch(_EMA) as ema:
        await BetterThermostat._async_update_ema_periodic(bt)
    ema.assert_not_called()
    bt.async_write_ha_state.assert_not_called()


@pytest.mark.asyncio
async def test_skips_without_last_known_temperature(bt):
    """Without a last known external temperature, nothing is updated."""
    bt.last_known_external_temperature = None
    with patch(_EMA) as ema:
        await BetterThermostat._async_update_ema_periodic(bt)
    ema.assert_not_called()
    bt.async_write_ha_state.assert_not_called()


@pytest.mark.asyncio
async def test_updates_ema_and_writes_state_without_slope(bt):
    """First run (no previous EMA) updates the filter and writes state, no slope."""
    bt.room_temperature_ema = None
    bt._slope_periodic_last_ts = None
    bt.clock = FakeClock(monotonic_value=1000.0)
    with patch(_EMA, _filter_update(20.5)):
        await BetterThermostat._async_update_ema_periodic(bt)
    assert bt.temperature_slope is None
    assert bt._slope_periodic_last_ts == 1000.0
    bt.async_write_ha_state.assert_called_once()


@pytest.mark.asyncio
async def test_computes_slope_from_ema_change(bt):
    """With a previous EMA and timestamp, the slope is (Δema / Δt_min)."""
    bt.room_temperature_ema = 20.0
    bt._slope_periodic_last_ts = 1000.0  # 600 s before "now"
    bt.clock = FakeClock(monotonic_value=1600.0)
    with patch(_EMA, MagicMock(return_value=21.0)):
        await BetterThermostat._async_update_ema_periodic(bt)
    # Δt = 600 s = 10 min, Δema = 1.0 K  ->  slope = 0.1 K/min
    assert bt.temperature_slope == pytest.approx(0.1)
    assert bt._slope_periodic_last_ts == 1600.0


@pytest.mark.asyncio
async def test_tiny_interval_skips_slope(bt):
    """A sub-0.1-minute interval does not produce a slope (avoids noise/div issues)."""
    bt.room_temperature_ema = 20.0
    bt._slope_periodic_last_ts = 1599.0  # 1 s before "now"
    bt.clock = FakeClock(monotonic_value=1600.0)
    with patch(_EMA, MagicMock(return_value=21.0)):
        await BetterThermostat._async_update_ema_periodic(bt)
    assert bt.temperature_slope is None
    assert bt._slope_periodic_last_ts == 1600.0


@pytest.mark.asyncio
async def test_ema_error_is_caught(bt):
    """An error from the EMA update is swallowed (tick must not crash)."""
    bt.room_temperature_ema = 20.0
    bt._slope_periodic_last_ts = 1000.0
    bt.clock = FakeClock(monotonic_value=1600.0)
    with patch(_EMA, MagicMock(side_effect=RuntimeError("boom"))):
        await BetterThermostat._async_update_ema_periodic(bt)
    # No slope written, no crash
    assert bt.temperature_slope is None
    bt.async_write_ha_state.assert_not_called()


@pytest.mark.asyncio
async def test_a_tick_that_moves_no_published_value_writes_nothing(bt):
    """The filter and slope stay on their published steps: no state write."""
    bt.room_temperature_ema = 20.0
    bt.room_temperature_filtered = 20.0
    bt.temperature_slope = 0.0
    bt._slope_periodic_last_ts = 1000.0
    bt.clock = FakeClock(monotonic_value=1060.0)
    with patch(_EMA, _filter_update(20.000_001)):
        await BetterThermostat._async_update_ema_periodic(bt)
    assert bt.temperature_slope == pytest.approx(0.000_001)
    bt.async_write_ha_state.assert_not_called()


@pytest.mark.asyncio
async def test_a_slope_step_alone_writes_the_state(bt):
    """A slope that moves one published step writes, though the filter holds."""
    bt.room_temperature_ema = 20.0
    bt.room_temperature_filtered = 20.0
    bt.temperature_slope = 0.0
    bt._slope_periodic_last_ts = 1000.0
    bt.clock = FakeClock(monotonic_value=1060.0)
    with patch(_EMA, _filter_update(20.0002)):
        await BetterThermostat._async_update_ema_periodic(bt)
    assert bt.room_temperature_filtered == 20.0
    bt.async_write_ha_state.assert_called_once()


@pytest.mark.asyncio
async def test_a_filter_step_alone_writes_the_state(bt):
    """A filter that moves one hundredth writes, though the slope holds."""
    bt.room_temperature_ema = 20.0
    bt.room_temperature_filtered = 20.0
    bt.temperature_slope = 0.01
    bt._slope_periodic_last_ts = 1000.0
    bt.clock = FakeClock(monotonic_value=1060.0)
    with patch(_EMA, _filter_update(20.01)):
        await BetterThermostat._async_update_ema_periodic(bt)
    assert bt.temperature_slope == pytest.approx(0.01)
    bt.async_write_ha_state.assert_called_once()
