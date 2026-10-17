"""The deCONZ and Tado adapters around their offset service.

Both ecosystems write the offset through their own service call, so the
rest of the adapter surface is thin: setpoint and mode writes go out as
plain climate service calls, the valve channel does not exist, and the
offset read tolerates a TRV that is unavailable or publishes garbage.
"""

import logging
from unittest.mock import AsyncMock, MagicMock

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfTemperature
from homeassistant.core import State
import pytest

from custom_components.better_thermostat.adapters import deconz, tado
from custom_components.better_thermostat.adapters.base import DeviceChannels
from custom_components.better_thermostat.trv import Trv
from tests.factories import ThermostatStandIn

ENTITY_ID = "climate.trv"
SERVICE_ADAPTERS = (deconz, tado)
# The attribute each ecosystem publishes its current offset in.
OFFSET_ATTRIBUTE = {deconz: "offset", tado: "offset_celsius"}


def _adapter_id(adapter):
    """Name an adapter module by its ecosystem, for readable test ids."""
    return adapter.__name__.rsplit(".", 1)[-1]


def _host(state=None, unit=UnitOfTemperature.CELSIUS):
    """Build a thermostat whose TRV reads ``state`` and records service calls.

    Parameters
    ----------
    state : State or None
        The state the TRV's climate entity reports, or None for none.
    unit : UnitOfTemperature
        The unit system Home Assistant runs in.

    Returns
    -------
    ThermostatStandIn
        A stand-in for the Better Thermostat climate entity instance.
    """
    host = ThermostatStandIn()
    host.device_name = "Test BT"
    host.context = None
    host.hass = MagicMock()
    host.hass.config.units.temperature_unit = unit
    host.hass.states.get = MagicMock(
        side_effect=lambda requested: state if requested == ENTITY_ID else None
    )
    host.hass.services.async_call = AsyncMock(return_value=None)
    host.real_trvs = {ENTITY_ID: Trv(entity_id=ENTITY_ID)}
    return host


class TestTheOffsetChannelIsProbedOnTheEntity:
    """Probing reports the offset channel the ecosystem actually exposes."""

    @pytest.mark.asyncio
    async def test_a_deconz_trv_without_an_offset_attribute_has_no_channel(self):
        """A deCONZ head that publishes no offset cannot be calibrated."""
        host = _host(State(ENTITY_ID, "heat", {"temperature": 21.0}))

        assert await deconz.get_info(host, ENTITY_ID) == DeviceChannels(
            offset_write=False, valve_write=False
        )

    @pytest.mark.asyncio
    async def test_a_tado_trv_always_offers_the_offset_service(self):
        """Tado's offset rides on its own service, whatever the entity shows."""
        host = _host(None)

        assert await tado.get_info(host, ENTITY_ID) == DeviceChannels(
            offset_write=True, valve_write=False
        )


@pytest.mark.parametrize("adapter", SERVICE_ADAPTERS, ids=_adapter_id)
class TestTheThinChannelsGoOutAsClimateCalls:
    """Setpoint and mode writes are the plain climate service calls."""

    @pytest.mark.asyncio
    async def test_init_issues_nothing(self, adapter):
        """The adapter has no per-entity setup and touches no device."""
        host = _host(State(ENTITY_ID, "heat"))

        assert await adapter.init(host, ENTITY_ID) is None
        host.hass.services.async_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_setpoint_goes_out_in_the_system_unit(self, adapter):
        """A 21 °C request reaches a Fahrenheit installation as 69.8 °F."""
        host = _host(
            State(ENTITY_ID, "heat", {"temperature": 68.0}),
            unit=UnitOfTemperature.FAHRENHEIT,
        )

        await adapter.set_temperature(host, ENTITY_ID, 21.0)

        call = host.hass.services.async_call.await_args
        assert call.args[:2] == ("climate", "set_temperature")
        assert call.args[2]["entity_id"] == ENTITY_ID
        assert call.args[2]["temperature"] == pytest.approx(69.8)

    @pytest.mark.asyncio
    async def test_the_mode_goes_out_normalized(self, adapter):
        """A mode spelled as the enum's repr reaches the TRV as the enum."""
        host = _host(State(ENTITY_ID, "off"))

        await adapter.set_hvac_mode(host, ENTITY_ID, "HVACMode.HEAT")

        call = host.hass.services.async_call.await_args
        assert call.args[:2] == ("climate", "set_hvac_mode")
        assert call.args[2] == {"entity_id": ENTITY_ID, "hvac_mode": HVACMode.HEAT}

    @pytest.mark.asyncio
    async def test_a_valve_write_is_a_no_op(self, adapter):
        """Neither ecosystem has a valve channel, so nothing goes out."""
        host = _host(State(ENTITY_ID, "heat"))

        assert await adapter.set_valve(host, ENTITY_ID, 40.0) is None
        host.hass.services.async_call.assert_not_awaited()


@pytest.mark.parametrize("adapter", SERVICE_ADAPTERS, ids=_adapter_id)
class TestTheOffsetReadToleratesAnUnreadableTrv:
    """An offset that cannot be read answers zero instead of raising."""

    @pytest.mark.parametrize("state", [STATE_UNAVAILABLE, STATE_UNKNOWN])
    @pytest.mark.asyncio
    async def test_an_unavailable_trv_reads_zero(self, adapter, state):
        """A stale attribute of an unavailable TRV is not taken for its offset."""
        host = _host(State(ENTITY_ID, state, {OFFSET_ATTRIBUTE[adapter]: 300}))

        assert await adapter.get_calibration_offset(host, ENTITY_ID) == 0.0

    @pytest.mark.asyncio
    async def test_a_missing_trv_reads_zero(self, adapter):
        """A TRV the state machine does not know reads as zero offset."""
        host = _host(None)

        assert await adapter.get_calibration_offset(host, ENTITY_ID) == 0.0

    @pytest.mark.asyncio
    async def test_a_non_numeric_offset_reads_zero_and_warns(self, adapter, caplog):
        """Garbage in the offset attribute is logged and read as zero."""
        host = _host(State(ENTITY_ID, "heat", {OFFSET_ATTRIBUTE[adapter]: "n/a"}))

        with caplog.at_level(logging.WARNING):
            assert await adapter.get_calibration_offset(host, ENTITY_ID) == 0.0

        assert "Could not convert calibration offset 'n/a'" in caplog.text
