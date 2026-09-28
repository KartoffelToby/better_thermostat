"""The cooler receives its setpoint on its own grid, and its echo is known.

A cooler only holds setpoints on the grid it publishes. The cooling target is
kept at full precision and rounded once, where the command leaves for the
device. The value that goes out is recorded as the cooler channel's write
before the service call is awaited, because the device can report it back
while the call is still in flight.
"""

from unittest.mock import AsyncMock, Mock

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import UnitOfTemperature
from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.better_thermostat.utils.controlling import control_cooler

from .test_control_cooler import (
    _make_cooler_state,
    _make_mock_self,
    _make_range_cooler_state,
)


def _hass(system_unit, cooler_state):
    hass = Mock()
    hass.config.units.temperature_unit = system_unit
    hass.services = Mock()
    hass.services.async_call = AsyncMock()
    hass.states.get.return_value = cooler_state
    return hass


def _payload(hass):
    calls = [
        c
        for c in hass.services.async_call.call_args_list
        if c.args[1] == "set_temperature"
    ]
    assert len(calls) == 1
    return calls[0].args[2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("system_unit", "reported", "step", "target", "expected"),
    [
        pytest.param(UnitOfTemperature.CELSIUS, 27.0, 0.5, 24.3, 24.5, id="c-half"),
        pytest.param(UnitOfTemperature.CELSIUS, 27.0, 1.0, 24.3, 24.0, id="c-whole"),
        pytest.param(UnitOfTemperature.CELSIUS, 27.0, 0.5, 23.2, 23.0, id="c-down"),
        # 24.3 °C is 75.74 °F.
        pytest.param(UnitOfTemperature.FAHRENHEIT, 80.0, 1.0, 24.3, 76.0, id="f-whole"),
        pytest.param(UnitOfTemperature.FAHRENHEIT, 80.0, 0.5, 24.3, 75.5, id="f-half"),
    ],
)
async def test_the_cooling_setpoint_lands_on_the_coolers_grid(
    system_unit, reported, step, target, expected
):
    """A cooling target between two grid points goes out as the nearer one."""
    hass = _hass(
        system_unit, _make_cooler_state(temperature=reported, target_temp_step=step)
    )
    mock_self = _make_mock_self(hass, cur_temp=27.0, bt_target_cooltemp=target)

    await control_cooler(mock_self)

    assert _payload(hass)["temperature"] == pytest.approx(expected, abs=1e-9)


@pytest.mark.asyncio
async def test_both_bounds_of_a_range_land_on_the_coolers_grid():
    """A range cooler receives both bounds on its grid."""
    hass = _hass(
        UnitOfTemperature.CELSIUS,
        _make_range_cooler_state(
            target_temp_high=28.0, target_temp_low=18.0, target_temp_step=0.5
        ),
    )
    mock_self = _make_mock_self(
        hass, cur_temp=27.0, bt_target_cooltemp=24.3, bt_target_temp=20.2
    )

    await control_cooler(mock_self)

    payload = _payload(hass)
    assert payload["target_temp_high"] == pytest.approx(24.5)
    assert payload["target_temp_low"] == pytest.approx(20.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("system_unit", "reported", "step", "sent_celsius"),
    [
        pytest.param(UnitOfTemperature.CELSIUS, 27.0, 0.5, 24.5, id="celsius"),
        # 76 °F is 24.44 °C.
        pytest.param(UnitOfTemperature.FAHRENHEIT, 80.0, 1.0, 24.444, id="fahrenheit"),
    ],
)
async def test_the_write_is_known_while_its_call_is_in_flight(
    system_unit, reported, step, sent_celsius
):
    """A report of the write that arrives during the call finds it recorded."""
    hass = _hass(
        system_unit, _make_cooler_state(temperature=reported, target_temp_step=step)
    )
    mock_self = _make_mock_self(hass, cur_temp=27.0, bt_target_cooltemp=24.3)
    seen_during_call = []

    async def service_call(domain, service, data, **kwargs):
        if service == "set_temperature":
            seen_during_call.append(mock_self.last_sent_cooler_temp)

    hass.services.async_call.side_effect = service_call

    await control_cooler(mock_self)

    assert seen_during_call == [pytest.approx(sent_celsius, abs=1e-3)]


@pytest.mark.asyncio
async def test_a_failed_write_leaves_the_previous_one_recorded():
    """A call that raises is not recorded as the cooler channel's write."""
    hass = _hass(
        UnitOfTemperature.CELSIUS,
        _make_cooler_state(state=HVACMode.COOL, temperature=27.0, target_temp_step=0.5),
    )
    mock_self = _make_mock_self(
        hass,
        cur_temp=27.0,
        bt_target_cooltemp=24.3,
        last_sent_cooler_temp=22.0,
        last_sent_cooler_temp_ts=1.0,
    )

    async def service_call(domain, service, data, **kwargs):
        if service == "set_temperature":
            raise HomeAssistantError("rejected")

    hass.services.async_call.side_effect = service_call

    await control_cooler(mock_self)

    assert (mock_self.last_sent_cooler_temp, mock_self.last_sent_cooler_temp_ts) == (
        22.0,
        1.0,
    )
