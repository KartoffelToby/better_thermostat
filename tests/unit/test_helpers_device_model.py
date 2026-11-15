"""Model detection through the device registry (helpers.get_device_model).

Priority is ``model_id`` > ``model`` before parentheses > ``model`` > the
configured model > ``"generic"``. The registry lookup is best effort: when it
cannot be performed the fallback chain still yields a name, and the reason is
recorded at debug level.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from homeassistant.helpers import device_registry as dr
import pytest

from custom_components.better_thermostat.utils.helpers import get_device_model
from tests.factories import ThermostatStandIn, make_entity_registry, make_registry_entry

_HELPERS = "custom_components.better_thermostat.utils.helpers"


def _bt() -> MagicMock:
    """Build the caller surface get_device_model reads."""
    bt = ThermostatStandIn()
    bt.hass = MagicMock()
    bt.device_name = "Test BT"
    return bt


def _registries(device: object | None):
    """Patch the entity and device registries to resolve to *device*."""
    entity_reg = make_entity_registry(
        make_registry_entry("climate.trv", device_id="dev1")
    )
    dev_reg = MagicMock()
    dev_reg.async_get.return_value = device
    return (
        patch(f"{_HELPERS}.er.async_get", return_value=entity_reg),
        patch(f"{_HELPERS}.dr.async_get", return_value=dev_reg),
    )


def _device(**kwargs) -> dr.DeviceEntry:
    """Build a device-registry entry with the fields the lookup reads."""
    fields = {
        "manufacturer": "Sonoff",
        "model": None,
        "model_id": None,
        "name": "Valve",
        "identifiers": {("mqtt", "0x1234")},
    }
    fields.update(kwargs)
    return dr.DeviceEntry(config_entry_id="entry", **fields)


@pytest.mark.asyncio
async def test_model_id_wins():
    """A device that reports model_id is identified by it."""
    er_patch, dr_patch = _registries(_device(model_id="TRVZB", model="TRV (Sonoff)"))
    with er_patch, dr_patch:
        assert await get_device_model(_bt(), "climate.trv") == "TRVZB"


@pytest.mark.asyncio
async def test_model_before_parentheses():
    """Without model_id the model up to the description is used."""
    er_patch, dr_patch = _registries(_device(model="TS0601 _TZE284 (Beok)"))
    with er_patch, dr_patch:
        assert await get_device_model(_bt(), "climate.trv") == "TS0601 _TZE284"


@pytest.mark.asyncio
async def test_unknown_device_falls_back_to_configured_model():
    """An unresolvable device falls back to the configured model."""
    er_patch, dr_patch = _registries(None)
    with er_patch, dr_patch:
        assert (
            await get_device_model(_bt(), "climate.trv", configured_model="TRVZB")
            == "TRVZB"
        )


@pytest.mark.asyncio
async def test_registry_failure_is_traced_and_falls_back(caplog):
    """An unreachable registry yields "generic" and names the entity."""
    with (
        caplog.at_level(logging.DEBUG, logger=_HELPERS),
        patch(f"{_HELPERS}.er.async_get", side_effect=RuntimeError("no registry")),
    ):
        assert await get_device_model(_bt(), "climate.trv") == "generic"
    assert "device registry lookup for climate.trv failed" in caplog.text
    assert any(record.exc_info for record in caplog.records)


@pytest.mark.asyncio
async def test_successful_lookup_is_not_traced_as_a_failure(caplog):
    """A registry hit reports the device it found, not a failure."""
    er_patch, dr_patch = _registries(_device(model_id="TRVZB"))
    with caplog.at_level(logging.DEBUG, logger=_HELPERS), er_patch, dr_patch:
        assert await get_device_model(_bt(), "climate.trv") == "TRVZB"
    assert "failed" not in caplog.text
    assert "identifiers=[('mqtt', '0x1234')]" in caplog.text


# IEEE addresses as each integration registers them: ZHA as the Zigbee
# connection and as its identifier, Zigbee2MQTT inside its identifier, deCONZ
# at the start of its identifier.
_COMET_REGISTRATIONS = {
    "zha": {
        "connections": {(dr.CONNECTION_ZIGBEE, "f4:ce:36:99:47:67:b8:a3")},
        "identifiers": {("zha", "f4:ce:36:99:47:67:b8:a3")},
    },
    "zigbee2mqtt": {"identifiers": {("mqtt", "zigbee2mqtt_0xf4ce36994767b8a3")}},
    "deconz": {"identifiers": {("deconz", "f4:ce:36:99:47:67:b8:a3-01-0201")}},
}
_SPIRIT_REGISTRATIONS = {
    "zha": {
        "connections": {(dr.CONNECTION_ZIGBEE, "00:15:8d:00:05:3d:2d:21")},
        "identifiers": {("zha", "00:15:8d:00:05:3d:2d:21")},
    },
    "zigbee2mqtt": {"identifiers": {("mqtt", "zigbee2mqtt_0x00158d00053d2d21")}},
}


class TestTheCometIsToldApartFromTheSpirit:
    """Both report SPZB0001; the IEEE address block tells them apart."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("integration", list(_COMET_REGISTRATIONS))
    async def test_a_comet_address_answers_as_the_comet(self, integration):
        """ZHA names the model, Zigbee2MQTT the model id; both resolve."""
        registration = _COMET_REGISTRATIONS[integration]
        field = "model_id" if integration == "zigbee2mqtt" else "model"
        device = _device(
            manufacturer="Eurotronic", **{field: "SPZB0001"}, **registration
        )
        er_patch, dr_patch = _registries(device)
        with er_patch, dr_patch:
            assert await get_device_model(_bt(), "climate.trv") == "COZB0001"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("integration", list(_SPIRIT_REGISTRATIONS))
    async def test_a_spirit_address_stays_the_spirit(self, integration):
        """The predecessor's block keeps its own model."""
        device = _device(
            manufacturer="Eurotronic",
            model="SPZB0001",
            **_SPIRIT_REGISTRATIONS[integration],
        )
        er_patch, dr_patch = _registries(device)
        with er_patch, dr_patch:
            assert await get_device_model(_bt(), "climate.trv") == "SPZB0001"

    @pytest.mark.asyncio
    async def test_a_spirit_without_an_address_stays_the_spirit(self):
        """A registry entry naming no address leaves the reported model."""
        device = _device(
            manufacturer="Eurotronic", model="SPZB0001", identifiers={("x", "valve")}
        )
        er_patch, dr_patch = _registries(device)
        with er_patch, dr_patch:
            assert await get_device_model(_bt(), "climate.trv") == "SPZB0001"

    @pytest.mark.asyncio
    async def test_another_model_from_the_comet_block_keeps_its_model(self):
        """The block belongs to Nordic, so only SPZB0001 is rewritten."""
        device = _device(model="CoZB_dha", **_COMET_REGISTRATIONS["zha"])
        er_patch, dr_patch = _registries(device)
        with er_patch, dr_patch:
            assert await get_device_model(_bt(), "climate.trv") == "CoZB_dha"
