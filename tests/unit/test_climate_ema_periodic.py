"""Branch coverage for BetterThermostat._async_update_ema_periodic.

The periodic EMA tick keeps the external-temperature filter converging when the
sensor is silent, and derives a temperature slope from the EMA change.  These
tests pin the skip conditions and the slope math.
"""

from unittest.mock import MagicMock, patch

import pytest

from custom_components.better_thermostat.climate import BetterThermostat

_CLIMATE = "custom_components.better_thermostat.climate"
_EMA = (
    "custom_components.better_thermostat.events.temperature._update_external_temp_ema"
)


@pytest.fixture
def bt():
    """Minimal BetterThermostat mock for the periodic EMA tick."""
    mock = MagicMock()
    mock.device_name = "Test BT"
    mock.startup_running = False
    mock.last_known_external_temp = 20.0
    mock.external_temp_ema = None
    mock._slope_periodic_last_ts = None
    mock.temp_slope = None
    mock.async_write_ha_state = MagicMock()
    mock.clock = MagicMock()
    return mock


@pytest.mark.asyncio
async def test_skips_while_startup_running(bt):
    """During startup the tick is a no-op (no EMA update, no state write)."""
    bt.startup_running = True
    with patch(_EMA) as ema:
        await BetterThermostat._async_update_ema_periodic(bt)
    ema.assert_not_called()
    bt.async_write_ha_state.assert_not_called()


@pytest.mark.asyncio
async def test_skips_without_last_known_temp(bt):
    """Without a last known external temperature, nothing is updated."""
    bt.last_known_external_temp = None
    with patch(_EMA) as ema:
        await BetterThermostat._async_update_ema_periodic(bt)
    ema.assert_not_called()
    bt.async_write_ha_state.assert_not_called()


@pytest.mark.asyncio
async def test_updates_ema_and_writes_state_without_slope(bt):
    """First run (no previous EMA) updates the filter and writes state, no slope."""
    bt.external_temp_ema = None
    bt._slope_periodic_last_ts = None
    bt.clock.monotonic.return_value = 1000.0
    with patch(_EMA, MagicMock(return_value=20.5)):
        await BetterThermostat._async_update_ema_periodic(bt)
    assert bt.temp_slope is None
    assert bt._slope_periodic_last_ts == 1000.0
    bt.async_write_ha_state.assert_called_once()


@pytest.mark.asyncio
async def test_computes_slope_from_ema_change(bt):
    """With a previous EMA and timestamp, the slope is (Δema / Δt_min)."""
    bt.external_temp_ema = 20.0
    bt._slope_periodic_last_ts = 1000.0  # 600 s before "now"
    bt.clock.monotonic.return_value = 1600.0
    with patch(_EMA, MagicMock(return_value=21.0)):
        await BetterThermostat._async_update_ema_periodic(bt)
    # Δt = 600 s = 10 min, Δema = 1.0 K  ->  slope = 0.1 K/min
    assert bt.temp_slope == pytest.approx(0.1)
    assert bt._slope_periodic_last_ts == 1600.0


@pytest.mark.asyncio
async def test_tiny_interval_skips_slope(bt):
    """A sub-0.1-minute interval does not produce a slope (avoids noise/div issues)."""
    bt.external_temp_ema = 20.0
    bt._slope_periodic_last_ts = 1599.0  # 1 s before "now"
    bt.clock.monotonic.return_value = 1600.0
    with patch(_EMA, MagicMock(return_value=21.0)):
        await BetterThermostat._async_update_ema_periodic(bt)
    assert bt.temp_slope is None
    assert bt._slope_periodic_last_ts == 1600.0


@pytest.mark.asyncio
async def test_ema_error_is_caught(bt):
    """An error from the EMA update is swallowed (tick must not crash)."""
    bt.external_temp_ema = 20.0
    bt._slope_periodic_last_ts = 1000.0
    bt.clock.monotonic.return_value = 1600.0
    with patch(_EMA, MagicMock(side_effect=RuntimeError("boom"))):
        await BetterThermostat._async_update_ema_periodic(bt)
    # No slope written, no crash
    assert bt.temp_slope is None
    bt.async_write_ha_state.assert_not_called()
