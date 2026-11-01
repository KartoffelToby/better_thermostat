"""The Tado adapter keeps every calibration offset inside the range Tado's API accepts.

Tado's cloud API rejects an offset beyond ±9.99 K with HTTP 422
``min.notmet``. The device then keeps reporting its old offset, the delegate
reads that as a diverged command and writes again every cycle, which drains
the account's API quota.
"""

from unittest.mock import AsyncMock, MagicMock

from homeassistant.core import State
import pytest

from custom_components.better_thermostat.adapters import tado
from custom_components.better_thermostat.trv import Trv

ENTITY_ID = "climate.tado_trv"
TADO_OFFSET_LIMIT = 9.99


def _mock_self():
    """Build a thermostat whose service calls are recorded, not executed.

    Returns
    -------
    MagicMock
        A stand-in for the Better Thermostat climate entity instance.
    """
    mock_self = MagicMock()
    mock_self.device_name = "Test BT"
    mock_self.context = None
    mock_self.hass = MagicMock()
    mock_self.hass.services.async_call = AsyncMock()
    mock_self.hass.states.get = lambda _requested: State(
        ENTITY_ID, "heat", {"offset_celsius": 0.0}
    )
    mock_self.real_trvs = {ENTITY_ID: Trv(entity_id=ENTITY_ID)}
    return mock_self


@pytest.mark.asyncio
async def test_declared_range_is_the_api_range():
    """The declared limits are the ones Tado's API enforces, not ±10."""
    mock_self = _mock_self()

    assert await tado.get_min_offset(mock_self, ENTITY_ID) == -TADO_OFFSET_LIMIT
    assert await tado.get_max_offset(mock_self, ENTITY_ID) == TADO_OFFSET_LIMIT


@pytest.mark.parametrize(
    ("requested", "written"),
    [
        (-10.0, -TADO_OFFSET_LIMIT),
        (-14.5, -TADO_OFFSET_LIMIT),
        (10.0, TADO_OFFSET_LIMIT),
        (12.0, TADO_OFFSET_LIMIT),
        (-9.99, -9.99),
        (-3.25, -3.25),
    ],
)
@pytest.mark.asyncio
async def test_write_stays_inside_the_api_range(requested, written):
    """An offset beyond the limit reaches the service clamped to it.

    The recorded command equals what went on the wire, so the delegate
    compares the device's report against a value the device could take.
    """
    mock_self = _mock_self()

    await tado.set_offset(mock_self, ENTITY_ID, requested)

    mock_self.hass.services.async_call.assert_awaited_once()
    service_data = mock_self.hass.services.async_call.call_args.args[2]
    assert service_data["offset"] == written
    assert mock_self.real_trvs[ENTITY_ID].last_calibration == written
