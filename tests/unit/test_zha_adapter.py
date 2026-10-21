"""The ZHA adapter offers a valve channel exactly where a quirk writes one.

A ZHA quirk publishes the valve opening in one of two shapes: as a sensor
that only reports it, which is what ZHA's own ``pi_heating_demand`` sensor
is for the Bosch Radiator Thermostat II, or as a number entity that also
takes writes. Only the number is a valve channel. The registry entries
below carry the shapes ZHA registers, so the lookup runs as it does in
Home Assistant.
"""

import importlib
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import State
import pytest

from custom_components.better_thermostat.adapters import delegate, generic, zha
from custom_components.better_thermostat.adapters.base import DeviceChannels
from custom_components.better_thermostat.trv import Trv
from tests.factories import ThermostatStandIn, make_entity_registry, make_registry_entry

ENTITY_ID = "climate.bosch_trv"
DEVICE_ID = "bosch_trv_device"
CONFIG_ENTRY_ID = "zha_config_entry"
IEEE = "00:11:22:33:44:55:66:77"

VALVE_SENSOR = "sensor.bosch_trv_pi_heating_demand"
VALVE_NUMBER = "number.bosch_trv_x_valve_position"
CALIBRATION_NUMBER = "number.bosch_trv_local_temperature_calibration"


def _entry(entity_id, unique_id, *, translation_key=None, original_name=None):
    """Return a registry entry of the Bosch TRV's ZHA device."""
    return make_registry_entry(
        entity_id,
        unique_id=unique_id,
        platform="zha",
        config_entry_id=CONFIG_ENTRY_ID,
        device_id=DEVICE_ID,
        translation_key=translation_key,
        original_name=original_name,
    )


TRV_ENTRY = _entry(ENTITY_ID, f"{IEEE}-1")
# ZHA's thermostat sensor for the attribute the Bosch quirk maps the valve
# opening onto; it reports the opening and takes no writes.
SENSOR_ENTRY = _entry(
    VALVE_SENSOR,
    f"{IEEE}-1-513-pi_heating_demand",
    translation_key="pi_heating_demand",
    original_name="PI heating demand",
)
# A number a quirk adds for the same attribute.
NUMBER_ENTRY = _entry(
    VALVE_NUMBER, f"{IEEE}-1-513-x_valve_position", original_name="X valve position"
)
CALIBRATION_ENTRY = _entry(
    CALIBRATION_NUMBER,
    f"{IEEE}-1-513-local_temperature_calibration",
    translation_key="local_temperature_calibration",
)


def _host():
    """Build a thermostat holding one Bosch TRV served by the ZHA adapter."""
    host = ThermostatStandIn()
    host.device_name = "Test BT"
    host.context = None
    host.hass = MagicMock()
    host.hass.services.async_call = AsyncMock(return_value=None)
    host.real_trvs = {ENTITY_ID: Trv(entity_id=ENTITY_ID, adapter=zha)}
    return host


def _registries(*entries):
    """Patch the entity and device registries to hold ``entries``."""
    device = MagicMock()
    device.identifiers = {("zha", IEEE)}
    device.model_id = "RBSH-TRV0-ZB-EU"
    device_registry = MagicMock()
    device_registry.async_get.return_value = device
    entity_registry = make_entity_registry(TRV_ENTRY, *entries)
    return (
        patch(
            "custom_components.better_thermostat.utils.helpers.er.async_get",
            return_value=entity_registry,
        ),
        patch(
            "custom_components.better_thermostat.utils.helpers.dr.async_get",
            return_value=device_registry,
        ),
    )


async def _channels(*entries):
    host = _host()
    entity_patch, device_patch = _registries(*entries)
    with entity_patch, device_patch:
        return await zha.get_info(host, ENTITY_ID)


async def _initialized(*entries):
    host = _host()
    entity_patch, device_patch = _registries(*entries)
    with (
        entity_patch,
        device_patch,
        patch.object(zha, "discover_calibration_entity", AsyncMock()),
    ):
        await zha.init(host, ENTITY_ID)
    return host, host.real_trvs[ENTITY_ID]


class TestTheChannelsFollowTheQuirk:
    """get_info reports a valve channel only for a number taking writes."""

    async def test_a_valve_sensor_alone_is_no_valve_channel(self):
        """The Bosch TRV under ZHA's own quirk reports its opening only."""
        assert await _channels(SENSOR_ENTRY) == DeviceChannels(
            offset_write=False, valve_write=False
        )

    async def test_a_valve_number_is_a_valve_channel(self):
        """A quirk that adds a valve number makes direct valve control available."""
        assert (await _channels(SENSOR_ENTRY, NUMBER_ENTRY)).valve_write is True

    async def test_the_offset_follows_the_calibration_entity(self):
        """A calibration number makes the offset channel available."""
        assert await _channels(CALIBRATION_ENTRY) == DeviceChannels(
            offset_write=True, valve_write=False
        )

    async def test_a_device_without_helpers_offers_nothing(self):
        """Without helper entities the TRV is driven through its setpoint."""
        assert await _channels() == DeviceChannels(
            offset_write=False, valve_write=False
        )


class TestInitAdoptsTheValveNumber:
    """init stores the valve entity the writes then go to."""

    async def test_the_number_is_adopted_over_the_sensor(self):
        """Where both exist, the number is the valve entity."""
        _, trv = await _initialized(SENSOR_ENTRY, NUMBER_ENTRY)
        assert trv.valve_position_entity == VALVE_NUMBER
        assert trv.valve_position_writable is True
        assert trv.capabilities().supports_valve_write is True

    async def test_a_sensor_leaves_the_trv_without_valve_writes(self):
        """A sensor is stored as read-only and offers no valve write."""
        _, trv = await _initialized(SENSOR_ENTRY)
        assert trv.valve_position_entity == VALVE_SENSOR
        assert trv.valve_position_writable is False
        assert trv.capabilities().supports_valve_write is False


class TestSetValve:
    """set_valve writes onto the grid the valve number publishes."""

    async def test_the_position_goes_to_the_valve_number(self):
        """A 40 % request lands on the number as 40."""
        host, _ = await _initialized(NUMBER_ENTRY)
        host.hass.states.get = MagicMock(
            return_value=State(VALVE_NUMBER, "0", {"min": 0, "max": 100, "step": 1})
        )

        await zha.set_valve(host, ENTITY_ID, 40)

        host.hass.services.async_call.assert_awaited_once()
        domain, service, data = host.hass.services.async_call.call_args.args
        assert (domain, service) == ("number", "set_value")
        assert data == {"entity_id": VALVE_NUMBER, "value": pytest.approx(40)}

    async def test_a_valve_sensor_takes_no_write(self):
        """A valve sensor that only reports the opening receives no position."""
        host, _ = await _initialized(SENSOR_ENTRY)

        await zha.set_valve(host, ENTITY_ID, 40)

        host.hass.services.async_call.assert_not_awaited()


class TestTheLoaderPicksTheZhaAdapter:
    """A ZHA TRV gets this adapter, not the generic fallback."""

    async def test_zha_resolves_onto_this_module(self):
        """The integration name is the adapter module's name."""
        thermostat = ThermostatStandIn()
        thermostat.device_name = "Test BT"
        imports = AsyncMock(
            side_effect=lambda _hass, name: importlib.import_module(name)
        )
        with patch.object(delegate, "async_import_module", imports):
            adapter = await delegate.load_adapter(thermostat, "zha", ENTITY_ID)

        assert adapter is zha
        assert adapter is not generic
