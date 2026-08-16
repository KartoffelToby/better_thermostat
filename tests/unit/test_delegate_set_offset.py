"""The delegate's set_offset separates the intent from the command.

``last_calibration_requested`` is the offset asked for; the adapter
records the value it actually put on the wire in ``last_calibration``.
Only a write the adapter accepted counts as either.

Both records, and the True the caller arms its confirmation watchdog on,
follow the adapter's boolean answer: only a write that went out counts.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.better_thermostat.adapters.delegate import set_offset
from custom_components.better_thermostat.trv import Trv

ENTITY_ID = "climate.trv"
# The retry decorator doubles a one-second base delay per attempt.
RETRY_BACKOFF_S = [1.0, 2.0, 4.0, 8.0, 16.0]
# The decorator spreads each delay by up to 20 % in either direction.
RETRY_JITTER = 0.2


@pytest.fixture
def bt():
    """Mock thermostat whose adapter accepts every offset write."""
    mock = MagicMock()
    mock.device_name = "Test BT"
    mock.hass = MagicMock()
    trv = Trv(entity_id=ENTITY_ID, local_calibration_min=-3.0)
    trv.adapter = MagicMock()
    trv.adapter.set_offset = AsyncMock(return_value=True)
    mock.real_trvs = {ENTITY_ID: trv}
    return mock


@pytest.mark.asyncio
async def test_accepted_write_records_the_requested_offset(bt):
    """A successful write reports success and remembers what was asked for."""
    result = await set_offset(bt, ENTITY_ID, -2.0)

    assert result is True
    assert bt.real_trvs[ENTITY_ID].last_calibration_requested == -2.0


@pytest.mark.asyncio
async def test_failed_write_records_nothing(bt):
    """An adapter raising on every retry leaves the intent unrecorded.

    A recorded intent would suppress the retry on the next cycle for a
    write that never left the house. The backoff between the attempts is
    recorded rather than slept through, so the retry schedule is asserted
    without spending it.
    """
    bt.real_trvs[ENTITY_ID].adapter.set_offset = AsyncMock(
        side_effect=ConnectionError("boom")
    )
    delays = []

    async def _record_delay(seconds):
        delays.append(seconds)

    with patch("asyncio.sleep", new=_record_delay):
        result = await set_offset(bt, ENTITY_ID, -2.0)

    assert result is False
    assert bt.real_trvs[ENTITY_ID].last_calibration_requested is None
    assert (
        bt.real_trvs[ENTITY_ID].adapter.set_offset.await_count
        == len(RETRY_BACKOFF_S) + 1
    )
    assert len(delays) == len(RETRY_BACKOFF_S)
    for actual, base in zip(delays, RETRY_BACKOFF_S, strict=True):
        assert base * (1 - RETRY_JITTER) <= actual <= base * (1 + RETRY_JITTER)


@pytest.mark.asyncio
async def test_device_without_an_offset_channel_records_nothing(bt):
    """An adapter that wrote nothing arms neither the gate nor the record."""
    bt.real_trvs[ENTITY_ID].adapter.set_offset = AsyncMock(return_value=False)

    result = await set_offset(bt, ENTITY_ID, -2.0)

    assert result is False
    assert bt.real_trvs[ENTITY_ID].last_calibration_requested is None


@pytest.mark.asyncio
async def test_a_written_zero_offset_is_still_a_write(bt):
    """The answer, not the value written, decides what counts as a command."""
    result = await set_offset(bt, ENTITY_ID, 0.0)

    assert result is True
    assert bt.real_trvs[ENTITY_ID].last_calibration_requested == 0.0


@pytest.mark.asyncio
async def test_clamped_write_keeps_intent_and_command_apart(bt):
    """An adapter clamping to its minimum records both values."""

    async def _clamping_set_offset(_self, entity_id, offset):
        trv = bt.real_trvs[entity_id]
        trv.last_calibration = max(offset, trv.local_calibration_min)
        return True

    bt.real_trvs[ENTITY_ID].adapter.set_offset = AsyncMock(
        side_effect=_clamping_set_offset
    )

    await set_offset(bt, ENTITY_ID, -5.0)

    assert bt.real_trvs[ENTITY_ID].last_calibration == -3.0
    assert bt.real_trvs[ENTITY_ID].last_calibration_requested == -5.0
