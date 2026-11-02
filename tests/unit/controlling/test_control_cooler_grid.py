"""The cooler receives its setpoint on its own grid, and its echo is known.

A cooler only holds setpoints on the grid it publishes. The cooling target is
kept at full precision and rounded once, where the command leaves for the
device. The value that goes out is recorded as the cooler channel's write
before the service call is awaited, because the device can report it back
while the call is still in flight.
"""

from homeassistant.const import UnitOfTemperature
from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.better_thermostat.utils.controlling import control_cooler
from custom_components.better_thermostat.utils.helpers import (
    SentCommand,
    cooler_send_cache,
    last_sent_cooler_temperature,
)

from .test_control_cooler import _make_cooler_setup, _range_attributes, _service_calls


def _payload(mock_hass):
    calls = _service_calls(mock_hass, "set_temperature")
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
        pytest.param(UnitOfTemperature.FAHRENHEIT, 80.5, 0.5, 24.3, 75.5, id="f-half"),
        # A cooler that publishes every temperature in whole degrees reports a
        # half-degree setpoint rounded, so it is sent whole degrees.
        pytest.param(
            UnitOfTemperature.FAHRENHEIT, 80.0, 0.5, 24.3, 76.0, id="f-half-shown-whole"
        ),
    ],
)
async def test_the_cooling_setpoint_lands_on_the_coolers_grid(
    system_unit, reported, step, target, expected
):
    """A cooling target between two grid points goes out as the nearer one."""
    mock_self, mock_hass, _ = _make_cooler_setup(
        cooler_attributes={"temperature": reported, "target_temp_step": step},
        system_unit=system_unit,
        room_temperature=27.0,
        cool_target_temperature=target,
    )

    await control_cooler(mock_self)

    assert _payload(mock_hass)["temperature"] == pytest.approx(expected, abs=1e-9)


@pytest.mark.asyncio
async def test_both_bounds_of_a_range_land_on_the_coolers_grid():
    """A range cooler receives both bounds on its grid."""
    mock_self, mock_hass, _ = _make_cooler_setup(
        cooler_attributes=_range_attributes(
            target_temp_high=28.0, target_temp_low=18.0, target_temp_step=0.5
        ),
        room_temperature=27.0,
        cool_target_temperature=24.3,
        heat_target_temperature=20.2,
    )

    await control_cooler(mock_self)

    payload = _payload(mock_hass)
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
    mock_self, mock_hass, _ = _make_cooler_setup(
        cooler_attributes={"temperature": reported, "target_temp_step": step},
        system_unit=system_unit,
        room_temperature=27.0,
        cool_target_temperature=24.3,
    )
    seen_during_call = []

    async def service_call(domain, service, data, **kwargs):
        if service == "set_temperature":
            seen_during_call.append(last_sent_cooler_temperature(mock_self))

    mock_hass.services.async_call.side_effect = service_call

    await control_cooler(mock_self)

    assert seen_during_call == [pytest.approx(sent_celsius, abs=1e-3)]


@pytest.mark.asyncio
async def test_a_failed_write_leaves_the_previous_one_recorded():
    """A call that raises is not recorded as the cooler channel's write."""
    mock_self, mock_hass, _ = _make_cooler_setup(
        cooler_attributes={"temperature": 27.0, "target_temp_step": 0.5},
        room_temperature=27.0,
        cool_target_temperature=24.3,
    )
    previous = SentCommand(22.0, mock_self.clock.monotonic() - 10_000.0)
    cooler_send_cache(mock_self)["temperature"] = previous

    async def service_call(domain, service, data, **kwargs):
        if service == "set_temperature":
            raise HomeAssistantError("rejected")

    mock_hass.services.async_call.side_effect = service_call

    await control_cooler(mock_self)

    assert cooler_send_cache(mock_self)["temperature"] == previous


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "expected"),
    [
        # 24.4 °C is 75.92 °F, 22.0 °C is 71.6 °F.
        pytest.param(24.4, 76.0, id="up"),
        pytest.param(22.0, 72.0, id="down"),
    ],
)
async def test_a_fahrenheit_cooler_without_a_step_gets_whole_degrees(target, expected):
    """A °F cooler that publishes no step is sent whole degrees Fahrenheit."""
    mock_self, mock_hass, _ = _make_cooler_setup(
        cooler_attributes={"temperature": 80.0},
        system_unit=UnitOfTemperature.FAHRENHEIT,
        room_temperature=27.0,
        cool_target_temperature=target,
    )

    await control_cooler(mock_self)

    assert _payload(mock_hass)["temperature"] == pytest.approx(expected, abs=1e-9)
