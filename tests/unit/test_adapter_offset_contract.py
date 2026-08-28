"""Every adapter answers a calibration offset write with a plain boolean.

The delegate keys the confirmation watchdog and the recorded intent on that
answer, so it has to separate "the write went out" from "this device has no
offset channel". A written offset cannot carry that: the legitimate value
0.0 reads the same as a device that wrote nothing.

The reads around that write answer to the same rule. Startup stores the
range and the granularity an adapter reports on the TRV record, and every
later write is clamped against them, so the three getters have one return
type and one answer for a device that declares nothing.
"""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import State
import pytest

from custom_components.better_thermostat.adapters import (
    deconz,
    generic,
    mqtt,
    tado,
    zwave_js,
)
from custom_components.better_thermostat.adapters.base import (
    wait_for_calibration_entity_or_timeout,
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


# The reads that fill the calibration bounds at startup.
BOUND_GETTERS = ("get_offset_step", "get_min_offset", "get_max_offset")


def _stateless(calibration_entity=CALIBRATION_ENTITY):
    """Build a thermostat whose state machine knows no entity at all."""
    thermostat = _mock_self(calibration_entity=calibration_entity)
    thermostat.hass.states.get = lambda requested: None
    return thermostat


class TestTheBoundsAreOneInterface:
    """The three getters answer the same type, whatever the ecosystem.

    Startup writes what they answer onto the TRV record, which declares
    plain numbers, and the offset write clamps against them. An adapter
    that answers ``None`` hands the shell a value it has to repair before
    anything can be computed with it, and one that answers an ``int``
    hands the next arithmetic a different type than its neighbour does.
    """

    @pytest.mark.parametrize("adapter", ENTITY_ADAPTERS + SERVICE_ADAPTERS)
    @pytest.mark.parametrize("getter", BOUND_GETTERS)
    @pytest.mark.asyncio
    async def test_a_bound_read_off_a_device_is_a_float(self, adapter, getter):
        """A TRV whose calibration entity reports gets a float."""
        assert isinstance(
            await getattr(adapter, getter)(_mock_self(), ENTITY_ID), float
        )

    @pytest.mark.parametrize("adapter", ENTITY_ADAPTERS + SERVICE_ADAPTERS)
    @pytest.mark.parametrize("getter", BOUND_GETTERS)
    @pytest.mark.asyncio
    async def test_an_undeclared_bound_is_a_float_too(self, adapter, getter):
        """A TRV that declares nothing gets a number, not an absence."""
        undiscovered = _stateless(calibration_entity=None)
        undiscovered.hass.states.get = _reject_anything_but_an_entity_id

        assert isinstance(
            await getattr(adapter, getter)(undiscovered, ENTITY_ID), float
        )

    @pytest.mark.parametrize("adapter", ENTITY_ADAPTERS + SERVICE_ADAPTERS)
    @pytest.mark.asyncio
    async def test_the_bounds_span_an_interval(self, adapter):
        """The lower bound is below the upper one, so the clamp holds."""
        thermostat = _stateless()

        assert await adapter.get_min_offset(
            thermostat, ENTITY_ID
        ) < await adapter.get_max_offset(thermostat, ENTITY_ID)

    @pytest.mark.parametrize("adapter", ENTITY_ADAPTERS + SERVICE_ADAPTERS)
    @pytest.mark.asyncio
    async def test_the_step_is_a_usable_grid(self, adapter):
        """A step of zero or less would divide the rounding by nothing."""
        assert await adapter.get_offset_step(_stateless(), ENTITY_ID) > 0


class TestTheUndeclaredBoundsAreOneTable:
    """An entity that declares no range means the same in every ecosystem.

    Which adapter found a calibration entity says nothing about the device
    behind it, so the answer for an entity that publishes no range may not
    depend on it. Two adapters disagreeing here give the same hardware two
    different clamps.
    """

    @pytest.mark.parametrize("adapter", ENTITY_ADAPTERS)
    @pytest.mark.parametrize("getter", BOUND_GETTERS)
    @pytest.mark.asyncio
    async def test_a_stateless_entity_answers_alike_everywhere(self, adapter, getter):
        """No state, so no published range: one answer for all of them."""
        assert await getattr(adapter, getter)(_stateless(), ENTITY_ID) == await getattr(
            generic, getter
        )(_stateless(), ENTITY_ID)

    @pytest.mark.parametrize("adapter", ENTITY_ADAPTERS)
    @pytest.mark.parametrize("getter", BOUND_GETTERS)
    @pytest.mark.asyncio
    async def test_an_undiscovered_entity_answers_alike_everywhere(
        self, adapter, getter
    ):
        """A TRV without a calibration entity is the same case again."""
        undiscovered = _stateless(calibration_entity=None)
        undiscovered.hass.states.get = _reject_anything_but_an_entity_id
        reference = _stateless(calibration_entity=None)
        reference.hass.states.get = _reject_anything_but_an_entity_id

        assert await getattr(adapter, getter)(undiscovered, ENTITY_ID) == await getattr(
            generic, getter
        )(reference, ENTITY_ID)


class TestTheBoundsOfASelectComeFromItsOptions:
    """A select names its range by the options it offers.

    Discovery accepts a select as a calibration entity for every
    ecosystem, and a select publishes no ``min`` and no ``max`` at all, so
    an adapter that only reads those two attributes reports a range the
    device never named and the shell then clamps against it.

    Home Assistant passes an entity's attributes on as the integration
    published them, so the options are not guaranteed to be strings and
    not guaranteed to carry a number each. Both are read the way the write
    path reads them, which parses every option it can and ignores the
    rest; a read that assumed otherwise would raise out of the startup
    pass that fills the bounds and leave the TRV without any.
    """

    @pytest.mark.parametrize("adapter", ENTITY_ADAPTERS)
    @pytest.mark.parametrize(
        ("getter", "expected"), [("get_min_offset", -6.0), ("get_max_offset", 6.0)]
    )
    @pytest.mark.asyncio
    async def test_the_options_name_the_range(self, adapter, getter, expected):
        """The offered options are the range, in every ecosystem."""
        mock_self = _mock_self_with_select()

        assert await getattr(adapter, getter)(mock_self, ENTITY_ID) == expected

    @pytest.mark.parametrize("adapter", ENTITY_ADAPTERS)
    @pytest.mark.parametrize(
        ("getter", "expected"), [("get_min_offset", -6.0), ("get_max_offset", 6.0)]
    )
    @pytest.mark.asyncio
    async def test_options_published_as_numbers_still_name_the_range(
        self, adapter, getter, expected
    ):
        """An option that is not a string is read for its number anyway."""
        mock_self = _mock_self_with_select(options=[-6, -3, 0, 3, 6])

        assert await getattr(adapter, getter)(mock_self, ENTITY_ID) == expected

    @pytest.mark.parametrize("adapter", ENTITY_ADAPTERS)
    @pytest.mark.parametrize(
        ("getter", "expected"), [("get_min_offset", -3.0), ("get_max_offset", 3.0)]
    )
    @pytest.mark.asyncio
    async def test_an_option_without_a_number_is_left_out(
        self, adapter, getter, expected
    ):
        """The options that do carry one still name the range."""
        mock_self = _mock_self_with_select(options=["off", "-3.0k", "3.0k"])

        assert await getattr(adapter, getter)(mock_self, ENTITY_ID) == expected

    @pytest.mark.parametrize("adapter", ENTITY_ADAPTERS)
    @pytest.mark.parametrize(
        ("getter", "expected"), [("get_min_offset", -10.0), ("get_max_offset", 10.0)]
    )
    @pytest.mark.asyncio
    async def test_options_that_name_no_range_fall_back_to_the_default(
        self, adapter, getter, expected
    ):
        """A select of named modes declares no offset range at all."""
        mock_self = _mock_self_with_select(options=["off", "auto"])

        assert await getattr(adapter, getter)(mock_self, ENTITY_ID) == expected


class TestAServiceWriteStaysInsideTheDeclaredRange:
    """An ecosystem that names its own range writes inside it.

    deCONZ and Tado report a fixed range instead of discovering one, and
    startup stores exactly that as the TRV's calibration bounds. A write
    that left it would contradict the bounds the same adapter reported a
    moment earlier.
    """

    @pytest.mark.parametrize("adapter", SERVICE_ADAPTERS)
    @pytest.mark.parametrize("requested", [-99.0, 99.0])
    @pytest.mark.asyncio
    async def test_a_request_beyond_the_range_reaches_the_bound(
        self, adapter, requested
    ):
        """What goes out is the nearest offset the ecosystem accepts."""
        mock_self = _mock_self()
        bound = await (
            adapter.get_min_offset(mock_self, ENTITY_ID)
            if requested < 0
            else adapter.get_max_offset(mock_self, ENTITY_ID)
        )

        await adapter.set_offset(mock_self, ENTITY_ID, requested)

        assert mock_self.real_trvs[ENTITY_ID].last_calibration == bound


_FIND_CALIBRATION = (
    "custom_components.better_thermostat.adapters.generic.find_local_calibration_entity"
)
_WAIT_FOR_CALIBRATION = (
    "custom_components.better_thermostat.adapters.generic."
    "wait_for_calibration_entity_or_timeout"
)
_FIND_VALVE = (
    "custom_components.better_thermostat.adapters.valve_entity.find_valve_entity"
)


class TestATrvWithoutACalibrationEntityIsNamedAtStartup:
    """Discovery that finds nothing is reported, and nothing is waited for.

    These TRVs are configured for local calibration, so one without an
    entity to write to has no channel at all and its owner has to be told
    which TRV it is. The wait that follows a successful discovery takes an
    entity id, so running it on the absence only reports the absence a
    second time and costs the startup budget nothing but time.
    """

    @pytest.mark.parametrize("adapter", ENTITY_ADAPTERS)
    @pytest.mark.asyncio
    async def test_the_trv_is_named_and_no_wait_is_started(self, adapter, caplog):
        """One warning names the TRV; the wait is not entered."""
        mock_self = _mock_self(calibration_entity=None)
        waiting = AsyncMock()

        with (
            caplog.at_level(logging.WARNING),
            patch(_FIND_VALVE, AsyncMock(return_value=None)),
            patch(_FIND_CALIBRATION, AsyncMock(return_value=None)),
            patch(_WAIT_FOR_CALIBRATION, waiting),
        ):
            await adapter.init(mock_self, ENTITY_ID)

        waiting.assert_not_awaited()
        assert f"no local calibration entity found for '{ENTITY_ID}'" in caplog.text

    @pytest.mark.parametrize("adapter", ENTITY_ADAPTERS)
    @pytest.mark.asyncio
    async def test_a_discovered_entity_is_waited_for(self, adapter, caplog):
        """The TRV that has one is not reported as missing it."""
        mock_self = _mock_self(calibration_entity=None)
        waiting = AsyncMock()

        with (
            caplog.at_level(logging.WARNING),
            patch(_FIND_VALVE, AsyncMock(return_value=None)),
            patch(_FIND_CALIBRATION, AsyncMock(return_value=CALIBRATION_ENTITY)),
            patch(_WAIT_FOR_CALIBRATION, waiting),
        ):
            await adapter.init(mock_self, ENTITY_ID)

        waiting.assert_awaited_once_with(mock_self, ENTITY_ID, CALIBRATION_ENTITY)
        assert "no local calibration entity found" not in caplog.text

    @pytest.mark.asyncio
    async def test_the_wait_itself_refuses_an_absent_entity(self, caplog):
        """The helper the adapters share holds the same line.

        It polls an entity id, and an absent entity gives it nothing to
        poll, so it says so instead of spending the startup budget on six
        rounds of waiting for an entity that was never found.
        """
        mock_self = _mock_self(calibration_entity=None)

        with caplog.at_level(logging.WARNING):
            await wait_for_calibration_entity_or_timeout(mock_self, ENTITY_ID, None)

        assert f"calibration_entity is None for '{ENTITY_ID}'" in caplog.text
        mock_self.hass.services.async_call.assert_not_awaited()
