"""Every adapter answers a calibration offset write with a plain boolean.

The delegate keys the confirmation watchdog and the recorded intent on that
answer, so it has to separate "the write went out" from "this device has no
offset channel". A written offset cannot carry that: the legitimate value
0.0 reads the same as a device that wrote nothing.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import State
import pytest

from custom_components.better_thermostat.adapters import (
    base,
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


class TestOffsetBoundsComeFromTheCalibrationEntity:
    """The bounds an entity-backed adapter reports are the entity's own.

    The calibration control clamps its request against these, so an adapter
    that answers from a table instead of from the discovered entity lets the
    control aim at offsets the device will never take.
    """

    @pytest.mark.parametrize("adapter", ENTITY_ADAPTERS)
    @pytest.mark.parametrize(
        ("getter", "expected"),
        [("get_min_offset", -5.0), ("get_max_offset", 5.0), ("get_offset_step", 0.5)],
    )
    @pytest.mark.asyncio
    async def test_the_entity_attributes_are_what_is_reported(
        self, adapter, getter, expected
    ):
        """Each bound is read off the calibration entity that was discovered."""
        mock_self = _mock_self()

        assert await getattr(adapter, getter)(mock_self, ENTITY_ID) == expected


def _mock_self_with_select(options=SELECT_OPTIONS, reported="0.0k"):
    """Build a thermostat whose calibration entity is a select.

    Parameters
    ----------
    options : list of str
        Options the select entity offers, as it publishes them.
    reported : str
        The option the select currently reports as its state.

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
        State(SELECT_CALIBRATION_ENTITY, reported, {"options": list(options)})
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


def _called_service(mock_self):
    """Return the (domain, service) pair the recorded service call addressed."""
    return mock_self.hass.services.async_call.await_args.args[:2]


class TestSelectOffsetRecordsWhatItSelected:
    """A select write records the offset the chosen option carries.

    Discovery accepts a calibration helper in the ``number`` or the ``select``
    domain, so every adapter that writes through a discovered entity can be
    handed a select and has to address it as one. A select ignores
    ``number.set_value``, so a write sent there never reaches the device while
    still reporting success, and the offset control never converges.

    The confirmation compares the device's report against the recorded
    command, so a command that never went on the wire would leave the two
    permanently apart and re-assert the write every cycle.

    The service adapters are absent from this axis on purpose: their offset
    rides on the ecosystem's own service call and never addresses an entity.
    """

    @pytest.mark.parametrize("adapter", ENTITY_ADAPTERS)
    @pytest.mark.asyncio
    async def test_the_write_addresses_the_select_service(self, adapter):
        """The option goes out through the service a select answers to."""
        mock_self = _mock_self_with_select()

        await adapter.set_offset(mock_self, ENTITY_ID, -2.0)

        assert _called_service(mock_self) == ("select", "select_option")

    @pytest.mark.parametrize("adapter", ENTITY_ADAPTERS)
    @pytest.mark.asyncio
    async def test_a_request_between_options_reaches_the_closest_one(self, adapter):
        """The option nearest the request is what the device is told."""
        mock_self = _mock_self_with_select()

        result = await adapter.set_offset(mock_self, ENTITY_ID, -2.0)

        assert result is True
        assert _selected_option(mock_self) == "-3.0k"

    @pytest.mark.parametrize("adapter", ENTITY_ADAPTERS)
    @pytest.mark.asyncio
    async def test_the_snapped_option_is_the_recorded_command(self, adapter):
        """The command is the snapped option's value, not the request."""
        mock_self = _mock_self_with_select()

        await adapter.set_offset(mock_self, ENTITY_ID, -2.0)

        assert mock_self.real_trvs[ENTITY_ID].last_calibration == -3.0

    @pytest.mark.parametrize("adapter", ENTITY_ADAPTERS)
    @pytest.mark.asyncio
    async def test_the_adapter_leaves_the_requested_intent_alone(self, adapter):
        """Recording the intent stays the delegate's business."""
        mock_self = _mock_self_with_select()

        await adapter.set_offset(mock_self, ENTITY_ID, -2.0)

        assert mock_self.real_trvs[ENTITY_ID].last_calibration_requested is None

    @pytest.mark.parametrize("adapter", ENTITY_ADAPTERS)
    @pytest.mark.asyncio
    async def test_an_offered_option_is_recorded_as_it_is(self, adapter):
        """A request the select offers verbatim needs no correction."""
        mock_self = _mock_self_with_select()

        await adapter.set_offset(mock_self, ENTITY_ID, -3.0)

        assert _selected_option(mock_self) == "-3.0k"
        assert mock_self.real_trvs[ENTITY_ID].last_calibration == -3.0

    @pytest.mark.parametrize("adapter", ENTITY_ADAPTERS)
    @pytest.mark.asyncio
    async def test_the_option_format_decides_the_command(self, adapter):
        """An option carries one decimal, so that is what was commanded."""
        mock_self = _mock_self_with_select(options=["-2.3k", "-2.2k"])

        await adapter.set_offset(mock_self, ENTITY_ID, -2.26)

        assert _selected_option(mock_self) == "-2.3k"
        assert mock_self.real_trvs[ENTITY_ID].last_calibration == -2.3

    @pytest.mark.parametrize("adapter", ENTITY_ADAPTERS)
    @pytest.mark.asyncio
    async def test_the_request_is_held_to_what_the_options_offer(self, adapter):
        """A request beyond the offered range reaches the outermost option."""
        mock_self = _mock_self_with_select()

        await adapter.set_offset(mock_self, ENTITY_ID, -12.0)

        assert _selected_option(mock_self) == "-6.0k"
        assert mock_self.real_trvs[ENTITY_ID].last_calibration == -6.0


class TestSelectOffsetIsReadBackAsKelvin:
    """The offset a select reports is read as the number its option carries.

    The confirmation and the calibration control both read this value back. An
    adapter that cannot parse the Kelvin suffix reads every select as 0.0 and
    keeps re-asserting a write the device already carries out.
    """

    @pytest.mark.parametrize("adapter", ENTITY_ADAPTERS)
    @pytest.mark.asyncio
    async def test_the_reported_option_is_read_as_its_offset(self, adapter):
        """The device's own option is what the reading reports."""
        mock_self = _mock_self_with_select(reported="-3.0k")

        assert await adapter.get_current_offset(mock_self, ENTITY_ID) == -3.0


class TestForcedZeroAddressesTheEntitysOwnDomain:
    """A calibration helper that never showed up is zeroed through its own service.

    The startup wait gives up after its retries and writes a zero to nudge the
    entity into reporting. That write is fire-and-forget, so a request sent to
    the wrong domain leaves no trace at all: the helper keeps its old offset
    and nothing says so.
    """

    @pytest.mark.parametrize(
        ("calibration_entity", "expected_service", "expected_payload"),
        [
            (
                CALIBRATION_ENTITY,
                ("number", "set_value"),
                {"entity_id": CALIBRATION_ENTITY, "value": 0},
            ),
            (
                SELECT_CALIBRATION_ENTITY,
                ("select", "select_option"),
                {"entity_id": SELECT_CALIBRATION_ENTITY, "option": "0.0k"},
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_the_zero_goes_out_through_the_matching_service(
        self, calibration_entity, expected_service, expected_payload
    ):
        """Each domain is addressed through the service it answers to."""
        mock_self = _mock_self_with_select()
        mock_self.hass.states.get = lambda requested: State(
            calibration_entity, "unavailable", {"options": list(SELECT_OPTIONS)}
        )

        with patch(
            "custom_components.better_thermostat.adapters.base.asyncio.sleep",
            AsyncMock(),
        ):
            await base.wait_for_calibration_entity_or_timeout(
                mock_self, ENTITY_ID, calibration_entity
            )

        assert _called_service(mock_self) == expected_service
        assert mock_self.hass.services.async_call.await_args.args[2] == expected_payload

    @pytest.mark.asyncio
    async def test_a_select_without_options_is_told_the_kelvin_spelling_of_zero(self):
        """With no option list to go by, zero Kelvin is spelled out."""
        mock_self = _mock_self_with_select()
        mock_self.hass.states.get = lambda requested: None

        with patch(
            "custom_components.better_thermostat.adapters.base.asyncio.sleep",
            AsyncMock(),
        ):
            await base.wait_for_calibration_entity_or_timeout(
                mock_self, ENTITY_ID, SELECT_CALIBRATION_ENTITY
            )

        assert _selected_option(mock_self) == "0.0k"

    @pytest.mark.asyncio
    async def test_the_zero_option_the_device_offers_is_the_one_used(self):
        """A device that spells zero its own way is told its own spelling."""
        mock_self = _mock_self_with_select()
        mock_self.hass.states.get = lambda requested: State(
            SELECT_CALIBRATION_ENTITY, "unavailable", {"options": ["-3k", "0k", "3k"]}
        )

        with patch(
            "custom_components.better_thermostat.adapters.base.asyncio.sleep",
            AsyncMock(),
        ):
            await base.wait_for_calibration_entity_or_timeout(
                mock_self, ENTITY_ID, SELECT_CALIBRATION_ENTITY
            )

        assert _selected_option(mock_self) == "0k"


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


def _reject_anything_but_an_entity_id(requested):
    """Stand in for ``StateMachine.get``, which lower-cases what it is given.

    Parameters
    ----------
    requested : object
        Whatever the adapter passed as the entity id.

    Returns
    -------
    None
        No state, for any entity id.

    Raises
    ------
    AttributeError
        When the adapter passed something other than an entity id, exactly as
        Home Assistant's own state machine does.
    """
    if not isinstance(requested, str):
        raise AttributeError(
            f"'{type(requested).__name__}' object has no attribute 'lower'"
        )


class TestNoOffsetChannelIsNeverLookedUp:
    """A TRV without a calibration entity is not looked up in the state machine.

    Discovery leaves the entity id at None when it finds no calibration
    helper, and the state machine takes an entity id. An adapter that hands
    it that None raises instead of answering, and the raise reaches the
    startup read that fills the calibration bounds.
    """

    @pytest.mark.parametrize("adapter", ENTITY_ADAPTERS)
    @pytest.mark.parametrize(
        "getter",
        ["get_current_offset", "get_offset_step", "get_min_offset", "get_max_offset"],
    )
    @pytest.mark.asyncio
    async def test_getter_answers_its_no_channel_default(self, adapter, getter):
        """The answer is the one the adapter gives for an entity without state.

        Both cases mean the same thing to the caller: this TRV offers no
        offset reading, so fall back to the adapter's declared bounds.
        """
        undiscovered = _mock_self(calibration_entity=None)
        undiscovered.hass.states.get = _reject_anything_but_an_entity_id
        stateless = _mock_self()
        stateless.hass.states.get = lambda requested: None

        assert await getattr(adapter, getter)(undiscovered, ENTITY_ID) == await getattr(
            adapter, getter
        )(stateless, ENTITY_ID)
