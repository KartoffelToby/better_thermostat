"""Every adapter answers a calibration offset write with a plain boolean.

The delegate keys the confirmation watchdog and the recorded intent on that
answer, so it has to separate "the write went out" from "this device has no
offset channel". A written offset cannot carry that: the legitimate value
0.0 reads the same as a device that wrote nothing.
"""

from unittest.mock import AsyncMock, MagicMock

from homeassistant.core import State
import pytest

from custom_components.better_thermostat.adapters import (
    deconz,
    generic,
    mqtt,
    tado,
    zwave_js,
)
from custom_components.better_thermostat.trv import Trv

ENTITY_ID = "climate.trv"
CALIBRATION_ENTITY = "number.trv_local_temperature_calibration"

# Adapters that write through a discovered number entity, and therefore have
# an unsupported path when discovery found none.
ENTITY_ADAPTERS = (generic, mqtt, zwave_js)
# Adapters whose offset rides on the ecosystem's own service call.
SERVICE_ADAPTERS = (deconz, tado)


def _mock_self(calibration_entity=CALIBRATION_ENTITY):
    """Build a thermostat whose service calls are recorded, not executed.

    Parameters
    ----------
    calibration_entity : str or None
        Entity ID of the discovered calibration number entity, or None to
        model a TRV for which discovery found none.

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
    mock_self.hass.states.get = lambda requested: (
        State(CALIBRATION_ENTITY, "0.0", {"min": -5.0, "max": 5.0, "step": 0.5})
        if requested == CALIBRATION_ENTITY
        else State(ENTITY_ID, "heat", {"offset": 0.0, "offset_celsius": 0.0})
    )
    trv = Trv(entity_id=ENTITY_ID)
    trv.local_temperature_calibration_entity = calibration_entity
    trv.local_calibration_min = -5.0
    trv.local_calibration_max = 5.0
    mock_self.real_trvs = {ENTITY_ID: trv}
    return mock_self


class TestOffsetWriteReportsTrue:
    """A write that went out is reported as one, whatever value it carried."""

    @pytest.mark.parametrize("adapter", ENTITY_ADAPTERS + SERVICE_ADAPTERS)
    @pytest.mark.parametrize("offset", [0.0, -2.5])
    @pytest.mark.asyncio
    async def test_write_reports_true(self, adapter, offset):
        """The answer is True, not the offset that was written."""
        mock_self = _mock_self()

        result = await adapter.set_offset(mock_self, ENTITY_ID, offset)

        assert result is True
        assert mock_self.hass.services.async_call.await_count == 1

    @pytest.mark.parametrize("adapter", ENTITY_ADAPTERS + SERVICE_ADAPTERS)
    @pytest.mark.asyncio
    async def test_written_offset_is_recorded_as_the_command(self, adapter):
        """The value that went on the wire is kept apart from the answer."""
        mock_self = _mock_self()

        await adapter.set_offset(mock_self, ENTITY_ID, -2.5)

        assert mock_self.real_trvs[ENTITY_ID].last_calibration == -2.5


class TestNoOffsetChannelReportsFalse:
    """A TRV without a calibration entity gets no write and no false claim."""

    @pytest.mark.parametrize("adapter", ENTITY_ADAPTERS)
    @pytest.mark.asyncio
    async def test_missing_calibration_entity_reports_false(self, adapter):
        """Nothing is written and nothing is claimed to have been."""
        mock_self = _mock_self(calibration_entity=None)

        result = await adapter.set_offset(mock_self, ENTITY_ID, -2.0)

        assert result is False
        mock_self.hass.services.async_call.assert_not_awaited()
        assert mock_self.real_trvs[ENTITY_ID].last_calibration is None
