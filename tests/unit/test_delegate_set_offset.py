"""The delegate records the offset it asked for, separate from what was sent.

``last_calibration`` is what the adapter put on the wire after its own clamp to
the device's declared offset range; ``last_calibration_requested`` is the value
that was asked for before that clamp. Keeping the two apart is what lets the
write gate tell a device resting at a declared limit from one that dropped the
write.

Both records, and the True the caller arms its confirmation watchdog on, follow
the adapter's boolean answer: only a write that went out counts.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.better_thermostat.adapters.delegate import set_offset
from custom_components.better_thermostat.trv import Trv

ENTITY_ID = "climate.trv"


@pytest.fixture
def bt():
    """Mock thermostat whose adapter accepts every offset write."""
    mock = MagicMock()
    mock.device_name = "Test BT"
    mock.hass = MagicMock()
    trv = Trv(
        entity_id=ENTITY_ID, local_calibration_min=-3.0, local_calibration_max=3.0
    )
    trv.adapter = MagicMock()
    trv.adapter.set_offset = AsyncMock(return_value=True)
    mock.real_trvs = {ENTITY_ID: trv}
    return mock


@pytest.mark.asyncio
async def test_successful_write_records_the_requested_offset(bt):
    """A write the adapter accepted records the requested value."""
    result = await set_offset(bt, ENTITY_ID, -2.0)

    assert result is True
    assert bt.real_trvs[ENTITY_ID].last_calibration_requested == -2.0


@pytest.mark.asyncio
async def test_failed_write_leaves_the_requested_offset_untouched(bt):
    """A write that raised on every retry records nothing and reports failure."""
    bt.real_trvs[ENTITY_ID].last_calibration_requested = -1.0
    bt.real_trvs[ENTITY_ID].adapter.set_offset = AsyncMock(
        side_effect=RuntimeError("device refused")
    )

    result = await set_offset(bt, ENTITY_ID, -2.0)

    assert result is False
    assert bt.real_trvs[ENTITY_ID].last_calibration_requested == -1.0


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
async def test_clamped_write_keeps_the_pre_clamp_intent(bt):
    """An adapter clamp moves the command, not the recorded intent."""

    async def _clamping_set_offset(_self, entity_id, offset):
        trv = _self.real_trvs[entity_id]
        trv.last_calibration = max(float(trv.local_calibration_min), float(offset))
        return True

    bt.real_trvs[ENTITY_ID].adapter.set_offset = AsyncMock(
        side_effect=_clamping_set_offset
    )

    result = await set_offset(bt, ENTITY_ID, -5.0)

    assert result is True
    assert bt.real_trvs[ENTITY_ID].last_calibration == -3.0
    assert bt.real_trvs[ENTITY_ID].last_calibration_requested == -5.0
