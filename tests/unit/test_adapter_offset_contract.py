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
SELECT_CALIBRATION_ENTITY = "select.trv_local_temperature_calibration"
# A calibration select whose options sit on a 3 K grid, so a request that
# falls between two of them reaches the device as a neighbouring option.
SELECT_OPTIONS = ["-6.0k", "-3.0k", "0.0k", "3.0k", "6.0k"]

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


def _mock_self_with_select(options=SELECT_OPTIONS):
    """Build a thermostat whose calibration entity is a select.

    Parameters
    ----------
    options : list of str
        Options the select entity offers, as it publishes them.

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
        State(SELECT_CALIBRATION_ENTITY, "0.0k", {"options": list(options)})
        if requested == SELECT_CALIBRATION_ENTITY
        else State(ENTITY_ID, "heat", {})
    )
    trv = Trv(entity_id=ENTITY_ID)
    trv.local_temperature_calibration_entity = SELECT_CALIBRATION_ENTITY
    mock_self.real_trvs = {ENTITY_ID: trv}
    return mock_self


def _selected_option(mock_self):
    """Return the option the recorded service call carried."""
    return mock_self.hass.services.async_call.await_args.args[2]["option"]


class TestSelectOffsetRecordsWhatItSelected:
    """A select write records the offset the chosen option carries.

    The confirmation compares the device's report against the recorded
    command, so a command that never went on the wire would leave the two
    permanently apart and re-assert the write every cycle.
    """

    @pytest.mark.asyncio
    async def test_a_request_between_options_reaches_the_closest_one(self):
        """The option nearest the request is what the device is told."""
        mock_self = _mock_self_with_select()

        result = await generic.set_offset(mock_self, ENTITY_ID, -2.0)

        assert result is True
        assert _selected_option(mock_self) == "-3.0k"

    @pytest.mark.asyncio
    async def test_the_snapped_option_is_the_recorded_command(self):
        """The command is the snapped option's value, not the request."""
        mock_self = _mock_self_with_select()

        await generic.set_offset(mock_self, ENTITY_ID, -2.0)

        assert mock_self.real_trvs[ENTITY_ID].last_calibration == -3.0

    @pytest.mark.asyncio
    async def test_the_adapter_leaves_the_requested_intent_alone(self):
        """Recording the intent stays the delegate's business."""
        mock_self = _mock_self_with_select()

        await generic.set_offset(mock_self, ENTITY_ID, -2.0)

        assert mock_self.real_trvs[ENTITY_ID].last_calibration_requested is None

    @pytest.mark.asyncio
    async def test_an_offered_option_is_recorded_as_it_is(self):
        """A request the select offers verbatim needs no correction."""
        mock_self = _mock_self_with_select()

        await generic.set_offset(mock_self, ENTITY_ID, -3.0)

        assert _selected_option(mock_self) == "-3.0k"
        assert mock_self.real_trvs[ENTITY_ID].last_calibration == -3.0

    @pytest.mark.asyncio
    async def test_the_option_format_decides_the_command(self):
        """An option carries one decimal, so that is what was commanded."""
        mock_self = _mock_self_with_select(options=["-2.3k", "-2.2k"])

        await generic.set_offset(mock_self, ENTITY_ID, -2.26)

        assert _selected_option(mock_self) == "-2.3k"
        assert mock_self.real_trvs[ENTITY_ID].last_calibration == -2.3


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


def _deconz_thermostat(reported=0):
    """Build a thermostat whose deCONZ TRV reports ``reported`` as its offset.

    Parameters
    ----------
    reported : int
        The value the deCONZ integration publishes on the climate entity,
        which is the ``config/offset`` of the deCONZ resource itself.

    Returns
    -------
    MagicMock
        A stand-in for the Better Thermostat climate entity instance.
    """
    mock_self = _mock_self(calibration_entity=None)
    mock_self.hass.states.get = lambda requested: State(
        ENTITY_ID, "heat", {"offset": reported}
    )
    return mock_self


def _written_config(mock_self):
    """The payload the recorded deCONZ configure call carried."""
    return mock_self.hass.services.async_call.await_args.args[2]["data"]


class TestTheDeconzOffsetTravelsInHundredthsOfADegree:
    """deCONZ carries a thermostat's offset in hundredths of a degree.

    ``config/offset`` sits in the same resource as ``heatsetpoint`` and the
    measured temperature and shares their encoding, so 250 is 2.5 K. The
    configure service hands the payload to the REST API unaltered, and the
    API scales it down to the 0.1 K steps the device itself takes; a plain
    Kelvin float therefore arrives a hundred times too small and leaves the
    device uncalibrated. The same scale governs the read, because the
    attribute the integration publishes is that resource value.
    """

    @pytest.mark.parametrize(
        ("kelvin", "expected"),
        [(0.0, 0), (0.5, 50), (2.5, 250), (-2.5, -250), (-6.0, -600)],
    )
    @pytest.mark.asyncio
    async def test_the_write_carries_the_offset_deconz_expects(self, kelvin, expected):
        """What goes on the wire is the Kelvin request in deCONZ's units."""
        mock_self = _mock_self()

        await deconz.set_offset(mock_self, ENTITY_ID, kelvin)

        assert _written_config(mock_self) == {"offset": expected}

    @pytest.mark.asyncio
    async def test_the_recorded_command_stays_in_kelvin(self):
        """The scale belongs to the wire, not to the TRV record.

        Every comparison the control cycle makes against ``last_calibration``
        is in Kelvin, so the value stored there is the one that was asked for.
        """
        mock_self = _mock_self()

        await deconz.set_offset(mock_self, ENTITY_ID, -2.0)

        assert mock_self.real_trvs[ENTITY_ID].last_calibration == -2.0

    @pytest.mark.parametrize(
        ("reported", "expected"), [(0, 0.0), (250, 2.5), (-250, -2.5), (-600, -6.0)]
    )
    @pytest.mark.asyncio
    async def test_the_read_answers_in_kelvin(self, reported, expected):
        """A device resting at 2.5 K reports 250 and reads back as 2.5."""
        assert (
            await deconz.get_current_offset(_deconz_thermostat(reported), ENTITY_ID)
            == expected
        )

    @pytest.mark.asyncio
    async def test_the_write_and_the_read_share_one_scale(self):
        """A device echoing the write back reports the offset that was sent.

        The control cycle compares the two every run, so a read and a write
        that disagreed on the scale would leave them permanently apart and
        re-assert the offset for as long as the TRV is calibrated.
        """
        writing = _mock_self()
        await deconz.set_offset(writing, ENTITY_ID, -2.0)
        echoed = _written_config(writing)["offset"]

        assert await deconz.get_current_offset(
            _deconz_thermostat(echoed), ENTITY_ID
        ) == pytest.approx(-2.0)
