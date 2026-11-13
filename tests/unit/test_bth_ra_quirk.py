"""The Bosch BTH-RA is driven by the default quirks plus a valve keepalive."""

import importlib
from unittest.mock import patch

import pytest

from custom_components.better_thermostat.model_fixes import (
    default as default_quirk,
    model_quirks as quirks,
)
from custom_components.better_thermostat.model_fixes.types import (
    ModelQuirks,
    ValveKeepaliveQuirk,
)
from tests.factories import ThermostatStandIn

bth_ra = importlib.import_module(
    "custom_components.better_thermostat.model_fixes.BTH-RA"
)

ENTITY = "climate.bosch_trv"
# The model id Zigbee2MQTT reports, and the device name it publishes beside
# it, which is all a release without a model id leaves.
BTH_RA_MODELS = ["BTH-RA", "Radiator thermostat II"]
# The silence after which the head returns to its own controller, and the
# longest a reconcile tick can leave a due keepalive waiting.
FALLBACK_SECONDS = 15 * 60
RECONCILE_TICK_SECONDS = 5 * 60


class TestModelDetection:
    """Both names Zigbee2MQTT gives the head load this module."""

    @pytest.mark.parametrize("model", BTH_RA_MODELS)
    def test_the_model_resolves_onto_the_bth_ra_module(self, model):
        """The device name has no module of its own."""
        assert quirks.get_model_quirks_name(model) == "BTH-RA"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", BTH_RA_MODELS)
    async def test_the_loader_imports_the_bth_ra_module(self, model):
        """The import is real, so a path the loader cannot reach lands on default."""
        host = ThermostatStandIn()
        host.device_name = "test_thermostat"

        async def _import(_hass, name):
            return importlib.import_module(name)

        with patch.object(quirks, "async_import_module", side_effect=_import):
            module = await quirks.load_model_quirks(host, model, ENTITY)

        assert module is bth_ra


class TestTheModuleSurface:
    """The head keeps every default behaviour and gains the keepalive."""

    def test_the_module_keeps_its_valve_demand_alive(self):
        """The keepalive is due early enough for a tick to deliver it in time."""
        module: object = bth_ra
        assert isinstance(module, ModelQuirks)
        assert isinstance(module, ValveKeepaliveQuirk)
        interval = module.VALVE_KEEPALIVE_INTERVAL_SECONDS
        assert 0 < interval + RECONCILE_TICK_SECONDS < FALLBACK_SECONDS

    def test_every_other_hook_is_the_default_one(self):
        """Apart from the keepalive the device runs as it did on the defaults."""
        offered = {name for name in vars(default_quirk._Surface) if name[0] != "_"}
        for name in offered:
            assert getattr(bth_ra, name) is getattr(default_quirk, name), name
