"""Tests for the Sonoff TRV-ZBT (TRV Gen2) quirk.

The TRV-ZBT reports model_id ``TRV-ZBT``. It takes the room temperature
through the same external input and sensor selector as the TRVZB, but its
valve numbers (heating_valve_position, idle_valve_position) configure the
device's own controller, so Better Thermostat must never write one of them.
"""

import importlib
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import State
from homeassistant.helpers import device_registry as dr
import pytest

from custom_components.better_thermostat.model_fixes import model_quirks as quirks
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.helpers import find_valve_entity
from tests.factories import ThermostatStandIn, make_entity_registry, make_registry_entry

TRV_ZBT_MODULE = "custom_components.better_thermostat.model_fixes.TRV-ZBT"
trv_zbt = importlib.import_module(TRV_ZBT_MODULE)
trvzb = importlib.import_module("custom_components.better_thermostat.model_fixes.TRVZB")

ENTITY = "climate.trv1"
DEVICE = "dev1"
HEATING_VALVE = "number.trv1_heating_valve_position"
IDLE_VALVE = "number.trv1_idle_valve_position"
EXTERNAL_INPUT = "number.trv1_external_temperature_input"
SENSOR_SELECT = "select.trv1_temperature_sensor_select"


def _entry(entity_id):
    """A registry entry on the TRV's device, named as Zigbee2MQTT names it.

    Zigbee2MQTT entities reach Home Assistant through MQTT discovery and
    carry no translation key, so only their ids name them.
    """
    return make_registry_entry(entity_id, device_id=DEVICE)


def _trv_zbt_entries():
    """The TRV-ZBT's climate entity and the numbers and selector next to it."""
    return [
        _entry(ENTITY),
        _entry(HEATING_VALVE),
        _entry(IDLE_VALVE),
        _entry(EXTERNAL_INPUT),
        _entry(SENSOR_SELECT),
    ]


def _make_self(selector_state="internal"):
    """A BT stand-in whose only TRV is a TRV-ZBT."""
    mock_self = ThermostatStandIn()
    mock_self.device_name = "test_thermostat"
    mock_self.context = MagicMock()
    mock_self.hass.services.async_call = AsyncMock()
    mock_self.real_trvs = {ENTITY: Trv(entity_id=ENTITY, model="TRV-ZBT")}
    mock_self._registry = make_entity_registry(*_trv_zbt_entries())

    mock_self.hass.states.get.return_value = State(
        SENSOR_SELECT,
        selector_state,
        {"options": ["internal", "external", "external_2", "external_3"]},
    )
    return mock_self


def _service_payloads(mock_self):
    """Every (domain, service, data) the quirk dispatched, in order."""
    return [
        call.args[:3] for call in mock_self.hass.services.async_call.await_args_list
    ]


class TestModelDetection:
    """The model_id the device registry reports picks the TRV-ZBT module."""

    def test_the_model_names_its_own_module(self):
        """No alias redirects the TRV-ZBT to the TRVZB module."""
        assert quirks.get_model_quirks_name("TRV-ZBT") == "TRV-ZBT"

    @pytest.mark.asyncio
    async def test_the_loader_imports_the_trv_zbt_module(self):
        """The hyphen survives the sanitising and the module imports.

        The import is real, so a file the loader cannot reach would land on
        the default module instead.
        """
        mock_self = ThermostatStandIn()
        mock_self.device_name = "test_thermostat"

        async def _import(_hass, name):
            return importlib.import_module(name)

        with patch.object(quirks, "async_import_module", side_effect=_import):
            module = await quirks.load_model_quirks(mock_self, "TRV-ZBT", ENTITY)

        assert module is trv_zbt
        assert module.__name__ == TRV_ZBT_MODULE


class TestExternalTemperature:
    """The room temperature reaches the input the TRV-ZBT regulates on."""

    @pytest.mark.asyncio
    async def test_the_room_temperature_is_written_and_the_selector_follows(
        self, monkeypatch
    ):
        """The input gets the value on its 0.1 grid, then the selector is set."""
        mock_self = _make_self(selector_state="internal")
        monkeypatch.setattr(trvzb.er, "async_get", lambda hass: mock_self._registry)

        assert await trv_zbt.maybe_set_external_temperature(mock_self, ENTITY, 21.46)

        assert _service_payloads(mock_self) == [
            ("number", "set_value", {"entity_id": EXTERNAL_INPUT, "value": 21.5}),
            (
                "select",
                "select_option",
                {"entity_id": SENSOR_SELECT, "option": "external"},
            ),
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("room", "written"), [(-3.0, 0.0), (120.0, 99.9)], ids=["below", "above"]
    )
    async def test_a_value_outside_the_input_range_is_clamped(
        self, monkeypatch, room, written
    ):
        """The input accepts 0 to 99.9 degrees and nothing beyond."""
        mock_self = _make_self(selector_state="external")
        monkeypatch.setattr(trvzb.er, "async_get", lambda hass: mock_self._registry)

        assert await trv_zbt.maybe_set_external_temperature(mock_self, ENTITY, room)

        assert _service_payloads(mock_self) == [
            ("number", "set_value", {"entity_id": EXTERNAL_INPUT, "value": written})
        ]


class TestNoValveWrite:
    """No Better Thermostat position reaches heating or idle valve position."""

    def test_the_module_offers_no_valve_channel(self):
        """Without an override the quirk is no valve channel of its own."""
        assert not hasattr(trv_zbt, "override_set_valve")
        assert not hasattr(trv_zbt, "has_valve_channel")
        assert quirks.quirk_writes_valve(trv_zbt) is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", ["TRV-ZBT", "SONOFF TRV-ZBT"])
    async def test_the_trvzb_valve_write_skips_the_trv_zbt(self, monkeypatch, model):
        """Called anyway, the TRVZB valve write declines without a service call.

        A model string that also names the vendor passes the TRVZB's Sonoff
        match, so it is the explicit TRV-ZBT check that turns it down.
        """
        mock_self = _make_self()
        mock_self.real_trvs[ENTITY].model = model
        monkeypatch.setattr(trvzb.er, "async_get", lambda hass: mock_self._registry)

        assert (
            await trvzb.maybe_set_sonoff_valve_percent(mock_self, ENTITY, 40) is False
        )
        assert trvzb.has_valve_channel(mock_self, ENTITY) is False
        mock_self.hass.services.async_call.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("model_id", "found"),
        [("TRV-ZBT", None), ("trv-zbt", None), ("OTHER-TRV", HEATING_VALVE)],
    )
    async def test_the_valve_lookup_turns_down_only_the_trv_zbt(self, model_id, found):
        """The adapter finds no valve entity on a TRV-ZBT, in any letter case.

        The same entities on a device of another model are still found,
        so the exclusion follows the model and not the entity names.
        """
        bt = ThermostatStandIn()
        device = dr.DeviceEntry(
            config_entry_id="entry", model_id=model_id, identifiers={("mqtt", DEVICE)}
        )
        registry = make_entity_registry(*_trv_zbt_entries())

        with (
            patch(
                "custom_components.better_thermostat.utils.helpers.er.async_get",
                return_value=registry,
            ),
            patch(
                "custom_components.better_thermostat.utils.helpers.dr.async_get"
            ) as dev_reg,
        ):
            dev_reg.return_value.async_get.return_value = device
            result = await find_valve_entity(bt, ENTITY)

        assert (result or {}).get("entity_id") == found
