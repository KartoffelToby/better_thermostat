"""A write to a TRV the thermostat does not hold is a bug, not a device failure.

The delegate answers False when a device refuses a write, and the caller
re-derives the value on its next cycle. A TRV missing from ``real_trvs`` is
nothing a later cycle can fix, so it surfaces as the ``KeyError`` it is instead
of passing for a refused write.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.climate.const import HVACMode
import pytest

from custom_components.better_thermostat.adapters.delegate import (
    set_calibration_offset,
    set_hvac_mode,
)
from custom_components.better_thermostat.trv import Trv
from tests.factories import ThermostatStandIn

KNOWN_TRV = "climate.known"
UNKNOWN_TRV = "climate.unknown"


@pytest.fixture
def bt():
    """Mock thermostat holding a single TRV whose adapter accepts every write."""
    mock = ThermostatStandIn()
    mock.device_name = "Test BT"
    mock.hass = MagicMock()
    trv = Trv(entity_id=KNOWN_TRV)
    trv.adapter = MagicMock()
    trv.adapter.set_hvac_mode = AsyncMock()
    trv.adapter.set_calibration_offset = AsyncMock(return_value=True)
    mock.real_trvs = {KNOWN_TRV: trv}
    return mock


@pytest.mark.asyncio
async def test_a_mode_write_to_an_unknown_trv_raises(bt):
    """The mode write names the missing TRV instead of answering False."""
    with pytest.raises(KeyError, match=UNKNOWN_TRV):
        await set_hvac_mode(bt, UNKNOWN_TRV, HVACMode.HEAT)
    bt.real_trvs[KNOWN_TRV].adapter.set_hvac_mode.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_offset_write_to_an_unknown_trv_raises(bt):
    """The offset write names the missing TRV instead of answering False."""
    with (
        patch(
            "custom_components.better_thermostat.adapters.delegate."
            "calibration_entity_disabled",
            return_value=False,
        ),
        pytest.raises(KeyError, match=UNKNOWN_TRV),
    ):
        await set_calibration_offset(bt, UNKNOWN_TRV, 1.0)
    bt.real_trvs[KNOWN_TRV].adapter.set_calibration_offset.assert_not_awaited()
