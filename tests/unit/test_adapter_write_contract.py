"""Every adapter's write paths answer to the same contract.

Five modules implement the same write surface for five ecosystems, and
the shell reaches them through a duck-typed dispatch that cannot check
any of it. What holds them together is therefore written down here
rather than in a base class:

* a valve write lands inside the bounds the number entity itself
  declares, whatever step grid it publishes;
* the setpoint and mode payloads carry the same domain, service and
  keys, in the system's temperature unit.

A new adapter is covered by all of it the moment it joins ``ADAPTERS``.

What is deliberately not asserted here: that a write is skipped when the
value has not changed. No adapter does that, and none should — the shell
already decides it, once, for every ecosystem.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import UnitOfTemperature
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
VALVE_ENTITY = "number.trv_valve_position"

ADAPTERS = {
    "deconz": deconz,
    "generic": generic,
    "mqtt": mqtt,
    "tado": tado,
    "zwave_js": zwave_js,
}
ADAPTER_IDS = sorted(ADAPTERS)
# The adapters that own a valve channel: they alone ever assign
# ``valve_position_entity``, in their own ``init``.
VALVE_ADAPTERS = ["mqtt", "zwave_js"]


def _thermostat(
    valve_entity=VALVE_ENTITY,
    valve_writable=True,
    valve_bounds=(0.0, 100.0, 1.0),
    unit=UnitOfTemperature.CELSIUS,
):
    """Build a thermostat whose service calls are recorded, not executed.

    Parameters
    ----------
    valve_entity : str or None
        Entity ID of the discovered valve number entity, or None to model
        a TRV for which discovery found none.
    valve_writable : bool or None
        What discovery concluded about that entity.
    valve_bounds : tuple of float
        The ``min``, ``max`` and ``step`` the number entity publishes.
    unit : UnitOfTemperature
        The system's configured temperature unit.

    Returns
    -------
    MagicMock
        A stand-in for the Better Thermostat climate entity instance.
    """
    minimum, maximum, step = valve_bounds
    thermostat = MagicMock()
    thermostat.device_name = "Test BT"
    thermostat.context = None
    thermostat.hass = MagicMock()
    thermostat.hass.services.async_call = AsyncMock()
    thermostat.hass.config.units.temperature_unit = unit
    thermostat.hass.states.get = lambda requested: (
        State(VALVE_ENTITY, "0", {"min": minimum, "max": maximum, "step": step})
        if requested == VALVE_ENTITY
        else State(ENTITY_ID, "heat", {})
    )
    trv = Trv(entity_id=ENTITY_ID)
    trv.valve_position_entity = valve_entity
    trv.valve_position_writable = valve_writable
    thermostat.real_trvs = {ENTITY_ID: trv}
    return thermostat


def _calls(thermostat):
    """Service calls the run recorded, as (domain, service, payload)."""
    return [
        (call.args[0], call.args[1], call.args[2])
        for call in thermostat.hass.services.async_call.await_args_list
    ]


# Grids a number entity can publish, including the ones where the step does
# not divide the range — quantizing there is what pushes a naive scale past
# the declared maximum.
VALVE_GRIDS = [
    (0.0, 100.0, 1.0),
    (0.0, 255.0, 1.0),
    (0.0, 255.0, 10.0),
    (0.0, 254.0, 5.0),
    (0.0, 100.0, 3.0),
    (5.0, 100.0, 1.0),
    (0.0, 100.0, 0.0),
]


class TestTheValveWriteStaysInsideTheDeclaredBounds:
    """What reaches the wire is a value the entity said it accepts."""

    @pytest.mark.parametrize("name", VALVE_ADAPTERS)
    @pytest.mark.parametrize("grid", VALVE_GRIDS, ids=repr)
    @pytest.mark.parametrize("percent", [0, 1, 50, 99, 100])
    @pytest.mark.asyncio
    async def test_a_scaled_request_lands_within_min_and_max(self, name, grid, percent):
        """Scaling and quantizing never leave the entity's own range."""
        minimum, maximum, _step = grid
        thermostat = _thermostat(valve_bounds=grid)

        await ADAPTERS[name].set_valve(thermostat, ENTITY_ID, percent)

        (_domain, _service, payload) = _calls(thermostat)[0]
        assert minimum <= payload["value"] <= maximum

    @pytest.mark.parametrize("name", VALVE_ADAPTERS)
    @pytest.mark.parametrize("percent", [-10, 150])
    @pytest.mark.asyncio
    async def test_a_request_outside_nought_to_hundred_is_clamped(self, name, percent):
        """A percentage out of range is clamped, not scaled past the end."""
        thermostat = _thermostat(valve_bounds=(0.0, 255.0, 1.0))

        await ADAPTERS[name].set_valve(thermostat, ENTITY_ID, percent)

        (_domain, _service, payload) = _calls(thermostat)[0]
        assert payload["value"] == (0.0 if percent < 0 else 255.0)

    @pytest.mark.parametrize("name", VALVE_ADAPTERS)
    @pytest.mark.asyncio
    async def test_a_missing_valve_entity_writes_nothing(self, name):
        """Without a discovered entity there is nothing to address."""
        thermostat = _thermostat(valve_entity=None)

        await ADAPTERS[name].set_valve(thermostat, ENTITY_ID, 50)

        assert _calls(thermostat) == []

    @pytest.mark.parametrize("name", VALVE_ADAPTERS)
    @pytest.mark.asyncio
    async def test_a_read_only_valve_entity_writes_nothing(self, name):
        """A read-only entity is refused at the adapter as well."""
        thermostat = _thermostat(valve_writable=False)

        await ADAPTERS[name].set_valve(thermostat, ENTITY_ID, 50)

        assert _calls(thermostat) == []


class TestTheSetpointPayloadIsTheSameEverywhere:
    """One setpoint call, one shape, whatever the ecosystem."""

    @pytest.mark.parametrize("name", ADAPTER_IDS)
    @pytest.mark.asyncio
    async def test_the_setpoint_rides_on_the_climate_service(self, name):
        """Domain, service and keys do not vary by adapter."""
        thermostat = _thermostat()

        await ADAPTERS[name].set_temperature(thermostat, ENTITY_ID, 21.5)

        assert _calls(thermostat) == [
            (
                "climate",
                "set_temperature",
                {"entity_id": ENTITY_ID, "temperature": 21.5},
            )
        ]

    @pytest.mark.parametrize("name", ADAPTER_IDS)
    @pytest.mark.asyncio
    async def test_the_setpoint_reaches_the_wire_in_the_system_unit(self, name):
        """Better Thermostat holds Celsius; the payload carries the unit."""
        thermostat = _thermostat(unit=UnitOfTemperature.FAHRENHEIT)

        await ADAPTERS[name].set_temperature(thermostat, ENTITY_ID, 20.0)

        (_domain, _service, payload) = _calls(thermostat)[0]
        assert payload["temperature"] == 68.0


class TestTheModePayloadIsTheSameEverywhere:
    """One mode call, one shape, and a normalized mode in it."""

    @pytest.mark.parametrize("name", ADAPTER_IDS)
    @pytest.mark.asyncio
    async def test_the_mode_rides_on_the_climate_service(self, name):
        """Domain, service and keys do not vary by adapter."""
        thermostat = _thermostat()

        with patch("asyncio.sleep", new=AsyncMock()):
            await ADAPTERS[name].set_hvac_mode(thermostat, ENTITY_ID, HVACMode.HEAT)

        assert _calls(thermostat) == [
            (
                "climate",
                "set_hvac_mode",
                {"entity_id": ENTITY_ID, "hvac_mode": HVACMode.HEAT},
            )
        ]

    @pytest.mark.parametrize("name", ADAPTER_IDS)
    @pytest.mark.asyncio
    async def test_a_spelled_out_mode_reaches_the_device_normalized(self, name):
        """The device is told an HVACMode, never an enum's repr."""
        thermostat = _thermostat()

        with patch("asyncio.sleep", new=AsyncMock()):
            await ADAPTERS[name].set_hvac_mode(thermostat, ENTITY_ID, "HVACMode.HEAT")

        (_domain, _service, payload) = _calls(thermostat)[0]
        assert payload["hvac_mode"] == HVACMode.HEAT
