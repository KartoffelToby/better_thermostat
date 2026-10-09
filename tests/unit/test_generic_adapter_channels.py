"""The generic adapter's offset read, select write, mode write and valve stub.

A generic TRV calibrates through a discovered ``number`` or ``select``
entity. A select only accepts an option it offers, so the write snaps the
request onto the offered grid and records the value of the option that went
out; an entity that publishes no usable grid gets the Kelvin spelling of the
request. The zero offset forced after the startup wait picks an option the
select offers in the same way.
"""

import logging
from unittest.mock import AsyncMock, MagicMock

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import UnitOfTemperature
from homeassistant.core import State
import pytest

from custom_components.better_thermostat.adapters import base, generic
from custom_components.better_thermostat.trv import Trv
from tests.factories import ThermostatStandIn

ENTITY_ID = "climate.trv"
SELECT_ENTITY = "select.trv_local_temperature_calibration"
NUMBER_ENTITY = "number.trv_local_temperature_calibration"


def _host(calibration_entity=SELECT_ENTITY, calibration_state=None):
    """Build a thermostat whose calibration entity reads ``calibration_state``.

    Parameters
    ----------
    calibration_entity : str or None
        Entity ID discovery found for the TRV's local calibration.
    calibration_state : State or None
        The state that entity reports, or None when it reports none.

    Returns
    -------
    ThermostatStandIn
        A stand-in for the Better Thermostat climate entity instance.
    """
    host = ThermostatStandIn()
    host.device_name = "Test BT"
    host.context = None
    host.hass = MagicMock()
    host.hass.config.units.temperature_unit = UnitOfTemperature.CELSIUS
    host.hass.states.get = MagicMock(
        side_effect=lambda requested: (
            calibration_state if requested == calibration_entity else None
        )
    )
    host.hass.services.async_call = AsyncMock(return_value=None)
    host.real_trvs = {
        ENTITY_ID: Trv(
            entity_id=ENTITY_ID, local_temperature_calibration_entity=calibration_entity
        )
    }
    return host


def _written_option(host):
    """The option the recorded select call carried."""
    call = host.hass.services.async_call.await_args
    assert call.args[:2] == ("select", "select_option")
    return call.args[2]["option"]


class TestTheOffsetReadToleratesGarbage:
    """A calibration entity whose state is no number reads as zero."""

    @pytest.mark.asyncio
    async def test_a_non_numeric_state_reads_zero_and_warns(self, caplog):
        """A select resting on a non-numeric option is logged and read as zero."""
        host = _host(
            calibration_state=State(SELECT_ENTITY, "auto", {"options": ["auto"]})
        )

        with caplog.at_level(logging.WARNING):
            assert await generic.get_calibration_offset(host, ENTITY_ID) == 0.0

        assert "Could not convert calibration offset 'auto'" in caplog.text

    @pytest.mark.asyncio
    async def test_a_kelvin_option_reads_as_its_number(self):
        """The control reading of the same select: ``-1.5k`` reads -1.5."""
        host = _host(
            calibration_state=State(SELECT_ENTITY, "-1.5k", {"options": ["-1.5k"]})
        )

        assert await generic.get_calibration_offset(host, ENTITY_ID) == -1.5


class TestASelectWriteSendsAnOfferedOption:
    """The option that goes out and the command recorded for it agree."""

    @pytest.mark.asyncio
    async def test_a_select_without_a_state_gets_the_kelvin_spelling(self):
        """No state, no grid: the request goes out as the entity's spelling.

        The domain comes from the entity ID, so the write still takes the
        select service rather than ``number.set_value``.
        """
        host = _host(calibration_state=None)

        assert await generic.set_calibration_offset(host, ENTITY_ID, 1.5) is True

        assert _written_option(host) == "1.5k"
        assert host.real_trvs[ENTITY_ID].last_calibration == 1.5

    @pytest.mark.parametrize(
        ("requested", "option", "commanded"),
        [(0.4, "0.0k", 0.0), (0.7, "1.0k", 1.0), (-0.8, "-1.0k", -1.0)],
    )
    @pytest.mark.asyncio
    async def test_a_non_numeric_option_is_never_a_snap_target(
        self, requested, option, commanded
    ):
        """An ``auto`` option among numeric ones is skipped, not chosen or fatal."""
        host = _host(
            calibration_state=State(
                SELECT_ENTITY, "0.0k", {"options": ["auto", "-1.0k", "0.0k", "1.0k"]}
            )
        )

        await generic.set_calibration_offset(host, ENTITY_ID, requested)

        assert _written_option(host) == option
        assert host.real_trvs[ENTITY_ID].last_calibration == commanded

    @pytest.mark.asyncio
    async def test_a_select_without_numeric_options_gets_the_rounded_request(self):
        """Nothing to snap onto: the one-decimal spelling goes out and is recorded.

        The recorded command is the rounded value the option carries, so the
        confirmation compares the device's report against what was sent.
        """
        host = _host(
            calibration_state=State(SELECT_ENTITY, "on", {"options": ["on", "off"]})
        )

        await generic.set_calibration_offset(host, ENTITY_ID, 1.04)

        assert _written_option(host) == "1.0k"
        assert host.real_trvs[ENTITY_ID].last_calibration == 1.0


class TestTheModeAndValveChannels:
    """The climate mode write and the absent valve channel."""

    @pytest.mark.asyncio
    async def test_the_mode_goes_out_normalised(self):
        """The mode is written to the TRV's climate entity in HA's spelling."""
        host = _host(calibration_entity=None)

        await generic.set_hvac_mode(host, ENTITY_ID, "heat")

        host.hass.services.async_call.assert_awaited_once()
        assert host.hass.services.async_call.await_args.args[:3] == (
            "climate",
            "set_hvac_mode",
            {"entity_id": ENTITY_ID, "hvac_mode": HVACMode.HEAT},
        )

    @pytest.mark.parametrize("error", [TypeError("bad"), ValueError("refused")])
    @pytest.mark.asyncio
    async def test_every_error_from_the_mode_call_reaches_the_caller(self, error):
        """A refused mode raises, so the caller retries it instead of caching it."""
        host = _host(calibration_entity=None)
        host.hass.services.async_call = AsyncMock(side_effect=error)

        with pytest.raises(type(error)):
            await generic.set_hvac_mode(host, ENTITY_ID, "heat")

    @pytest.mark.asyncio
    async def test_a_valve_write_is_a_no_op(self):
        """A generic TRV has no valve channel, so nothing goes out."""
        host = _host(calibration_entity=NUMBER_ENTITY)

        assert await generic.set_valve(host, ENTITY_ID, 40.0) is None
        host.hass.services.async_call.assert_not_awaited()


class TestTheForcedZeroPicksAnOfferedOption:
    """The zero written after the startup wait is an option the select offers."""

    @pytest.mark.asyncio
    async def test_the_zero_option_is_found_past_non_numeric_ones(self):
        """``auto`` is skipped and the select's own spelling of zero is sent."""
        host = _host()
        state = State(
            SELECT_ENTITY, "unknown", {"options": ["auto", "-1k", "0k", "1k"]}
        )

        await base._write_zero_calibration(host, SELECT_ENTITY, state)

        assert _written_option(host) == "0k"

    @pytest.mark.asyncio
    async def test_a_select_without_a_zero_option_gets_the_kelvin_spelling(self):
        """A grid that offers no zero falls back to ``0.0k``."""
        host = _host()
        state = State(SELECT_ENTITY, "unknown", {"options": ["auto", "-1k", "1k"]})

        await base._write_zero_calibration(host, SELECT_ENTITY, state)

        assert _written_option(host) == "0.0k"
