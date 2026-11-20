"""Model names Zigbee2MQTT and ZHA give a device load the module that drives it.

Zigbee2MQTT reports the model of its definition as the model id, and the
white-label model in its place when it recognises a white label by
fingerprint. ZHA reports the model string the device sends itself. A name
without an alias has no module of its own and lands on the default quirks.
"""

import importlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.better_thermostat.model_fixes import model_quirks as quirks
from custom_components.better_thermostat.utils.const import GENERIC_MODEL
from tests.factories import ThermostatStandIn, trv_record

ENTITY_ID = "climate.trv"
_PACKAGE = "custom_components.better_thermostat.model_fixes"

TV02 = importlib.import_module(f"{_PACKAGE}.TV02-Zigbee")

# Model name as the integration reports it, and the module that drives it.
ALIASES = [
    # Zigbee2MQTT's name for the Moes BHT-002-GCLZB, and its Moes white label.
    ("BHT-002", "BHT-002-GCLZB"),
    ("BHT-002/BHT-006", "BHT-002-GCLZB"),
    # Zigbee2MQTT white labels of TS0601_thermostat.
    ("07732L", "TS0601_thermostat"),
    ("P5630S", "TS0601_thermostat"),
    ("HY368", "TS0601_thermostat"),
    # Zigbee2MQTT white labels of TV02-Zigbee.
    ("ZTRV-ZX-TV01-MS", "TV02-Zigbee"),
    ("TV01-ZB", "TV02-Zigbee"),
    ("HA-08_THERMO", "TV02-Zigbee"),
    ("TV06", "TV02-Zigbee"),
    ("ZTRV-ZX-TV02", "TV02-Zigbee"),
    ("ETT-8", "TV02-Zigbee"),
    # Zigbee2MQTT white label of SEA801-Zigbee/SEA802-Zigbee.
    ("H1Z", "SEA801-Zigbee_SEA802-Zigbee"),
    # ZHA's names for the Bosch heads and room thermostats and the Aqara E1.
    ("RBSH-TRV0-ZB-EU", "BTH-RA"),
    ("RBSH-TRV1-ZB-EU", "BTH-RA"),
    ("RBSH-RTH0-BAT-ZB-EU", "BTH-RM"),
    ("RBSH-RTH0-ZB-EU", "BTH-RM230Z"),
    ("lumi.airrtc.agl001", "SRTS-A01"),
]
ALIAS_IDS = [model for model, _ in ALIASES]


async def _load(model):
    """Run the loader for ``model`` with a real import."""
    host = ThermostatStandIn()
    host.device_name = "test_thermostat"

    async def _import(_hass, name):
        return importlib.import_module(name)

    with patch.object(quirks, "async_import_module", side_effect=_import):
        return await quirks.load_model_quirks(host, model, ENTITY_ID)


class TestTheAliasesResolve:
    """Each reported name loads the module of the device it belongs to."""

    @pytest.mark.parametrize(("model", "module"), ALIASES, ids=ALIAS_IDS)
    def test_the_name_resolves_onto_the_module(self, model, module):
        """The alias names the module, not another alias."""
        assert quirks.get_model_quirks_name(model) == module

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("model", "module"), ALIASES, ids=ALIAS_IDS)
    async def test_the_loader_imports_the_module(self, model, module):
        """The import is real, so a module name that does not import lands on default."""
        loaded = await _load(model)

        assert loaded is importlib.import_module(f"{_PACKAGE}.{module}")

    def test_every_alias_names_a_module_that_exists(self):
        """A target that is itself only an alias would load the default quirks."""
        for model, module in quirks._QUIRK_MODULE_ALIASES.items():
            assert module not in quirks._QUIRK_MODULE_ALIASES, model
            importlib.import_module(f"{_PACKAGE}.{module}")


def _tv02_host(model):
    """A Better Thermostat stand-in driving one TRV of ``model``."""
    host = ThermostatStandIn()
    host.device_name = "Test BT"
    host.context = None
    host.in_maintenance = False
    host.hass = MagicMock()
    host.hass.services.async_call = AsyncMock()
    trv = trv_record(entity_id=ENTITY_ID)
    trv.model = model
    host.real_trvs = {ENTITY_ID: trv}
    return host


def _services_called(host):
    """The service names the host was asked for, in order."""
    return [call.args[1] for call in host.hass.services.async_call.await_args_list]


class TestTheTv02WhiteLabelsTakeTheManualPreset:
    """A white label of the TV02-Zigbee runs on the same converter.

    Its schedule overwrites a setpoint just as the TV02-Zigbee's does, so it
    is taken off the schedule the same way.
    """

    @pytest.mark.parametrize("model", ["TV02-Zigbee", "TV01-ZB", "ZTRV-ZX-TV02"])
    @pytest.mark.asyncio
    async def test_a_setpoint_write_selects_the_manual_preset_first(self, model):
        """The preset goes out ahead of the setpoint."""
        host = _tv02_host(model)

        assert await TV02.override_set_temperature(host, ENTITY_ID, 21.0) is True
        assert _services_called(host) == ["set_preset_mode", "set_temperature"]

    @pytest.mark.parametrize("model", ["TV02-Zigbee", "TV01-ZB", "ZTRV-ZX-TV02"])
    @pytest.mark.asyncio
    async def test_a_mode_write_selects_the_manual_preset_after_it(self, model):
        """The preset follows a mode that heats."""
        host = _tv02_host(model)

        assert await TV02.override_set_hvac_mode(host, ENTITY_ID, "heat") is True
        assert _services_called(host) == ["set_hvac_mode", "set_preset_mode"]

    @pytest.mark.asyncio
    async def test_a_model_without_the_alias_gets_no_preset(self):
        """A model the module does not answer for keeps its own schedule."""
        host = _tv02_host(GENERIC_MODEL)

        assert await TV02.override_set_temperature(host, ENTITY_ID, 21.0) is True
        assert _services_called(host) == ["set_temperature"]
