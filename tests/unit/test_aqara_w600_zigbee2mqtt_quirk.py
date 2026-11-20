"""The Aqara W600 under Zigbee2MQTT is driven by the SRTS-A01 quirk.

Zigbee2MQTT reports the W600 with the model id WT-A03E. The head exposes the
same ``sensor`` selector and ``external_temperature_input`` number as the
SRTS-A01, without translation keys, so the quirk has to find them by their
unique ids.
"""

import importlib
from unittest.mock import patch

import pytest

from custom_components.better_thermostat.model_fixes import model_quirks as quirks
from tests.factories import ThermostatStandIn, make_entity_registry, make_registry_entry

srts_a01 = importlib.import_module(
    "custom_components.better_thermostat.model_fixes.SRTS-A01"
)

W600_MODEL = "WT-A03E"
TRV = "climate.w600"
SELECTOR = "select.w600_sensor"
INPUT = "number.w600_external_temperature_input"
IEEE = "0x54ef4410017f78d9"


class TestModelDetection:
    """The model id Zigbee2MQTT reports loads the SRTS-A01 module."""

    def test_the_model_resolves_onto_the_srts_a01_module(self):
        """WT-A03E has no module of its own."""
        assert quirks.get_model_quirks_name(W600_MODEL) == "SRTS-A01"

    @pytest.mark.asyncio
    async def test_the_loader_imports_the_srts_a01_module(self):
        """The import is real, so a path the loader cannot reach lands on default."""
        host = ThermostatStandIn()
        host.device_name = "test_thermostat"

        async def _import(_hass, name):
            return importlib.import_module(name)

        with patch.object(quirks, "async_import_module", side_effect=_import):
            module = await quirks.load_model_quirks(host, W600_MODEL, TRV)

        assert module is srts_a01

    def test_the_matter_product_id_keeps_its_own_module(self):
        """A W600 under Matter still resolves onto its own module."""
        assert quirks.get_model_quirks_name("18437") == "18437"


class TestEntityLookup:
    """The quirk finds the W600's entities as Zigbee2MQTT registers them."""

    @pytest.fixture
    def host(self, monkeypatch):
        """A host whose registry holds the W600 entities without translation keys."""
        registry = make_entity_registry(
            make_registry_entry(TRV, unique_id=f"{IEEE}_climate_zigbee2mqtt"),
            make_registry_entry(
                SELECTOR,
                unique_id=f"{IEEE}_sensor_zigbee2mqtt",
                original_name="Temperature source",
            ),
            make_registry_entry(
                INPUT,
                unique_id=f"{IEEE}_external_temperature_input_zigbee2mqtt",
                original_name="External temperature input",
            ),
        )
        monkeypatch.setattr(srts_a01.er, "async_get", lambda hass: registry)
        host = ThermostatStandIn()
        host.device_name = "test_thermostat"
        return host

    def test_the_sensor_selector_is_found(self, host):
        """The selector is found by the ``sensor`` fragment of its unique id."""
        assert srts_a01._sensor_selector(host, TRV) == SELECTOR

    def test_the_external_temperature_input_is_found(self, host):
        """The input is found by the ``external_temperature_input`` fragment."""
        assert srts_a01._input_number(host, TRV) == INPUT
