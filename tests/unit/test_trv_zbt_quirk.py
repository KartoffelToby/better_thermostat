"""Tests for the Sonoff TRV-ZBT (TRV Gen2) quirk.

The TRV-ZBT reports model_id ``TRV-ZBT``. It takes the room temperature
through the same external input and sensor selector as the TRVZB. Its
valve takes the opening the device holds while heating and the one it holds
while idle; Better Thermostat writes its position into both.
"""

import importlib
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import State
from homeassistant.helpers import device_registry as dr
import pytest

from custom_components.better_thermostat.model_fixes import model_quirks as quirks
from custom_components.better_thermostat.utils.helpers import find_valve_entity
from tests.factories import (
    ThermostatStandIn,
    make_entity_registry,
    make_registry_entry,
    trv_record,
)

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
    mock_self.sensor_entity_id = "sensor.room_temperature"
    mock_self.hass.services.async_call = AsyncMock()
    mock_self.real_trvs = {ENTITY: trv_record(entity_id=ENTITY, model="TRV-ZBT")}
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


ZHA_OPENING = "number.trv1_valve_opening_degree"
ZHA_CLOSING = "number.trv1_valve_closing_degree"


def _zha_entries():
    """The TRV-ZBT as ZHA registers it: the TRVZB names on both numbers."""
    return [
        _entry(ENTITY),
        make_registry_entry(
            ZHA_OPENING, device_id=DEVICE, translation_key="valve_opening_degree"
        ),
        make_registry_entry(
            ZHA_CLOSING, device_id=DEVICE, translation_key="valve_closing_degree"
        ),
    ]


def _number_writes(mock_self):
    """Every number value the quirk wrote, as (entity_id, value), in order."""
    return [
        (data["entity_id"], data["value"])
        for domain, service, data in _service_payloads(mock_self)
        if (domain, service) == ("number", "set_value")
    ]


class TestValveWrite:
    """The position reaches the heating and the idle opening alike."""

    def test_the_module_is_a_valve_channel(self):
        """The quirk drives the valve, so the adapter offers direct control."""
        assert quirks.quirk_writes_valve(trv_zbt) is True

    @pytest.mark.asyncio
    async def test_zigbee2mqtt_numbers_both_take_the_position(self, monkeypatch):
        """heating_valve_position and idle_valve_position both get 40."""
        mock_self = _make_self()
        monkeypatch.setattr(trvzb.er, "async_get", lambda hass: mock_self._registry)

        assert trv_zbt.has_valve_channel(mock_self, ENTITY) is True
        assert await trv_zbt.override_set_valve(mock_self, ENTITY, 40) is True

        assert _number_writes(mock_self) == [(HEATING_VALVE, 40), (IDLE_VALVE, 40)]

    @pytest.mark.asyncio
    async def test_zha_closing_degree_takes_the_position_not_its_complement(
        self, monkeypatch
    ):
        """On the TRV-ZBT the attribute ZHA names closing degree is an opening."""
        mock_self = _make_self()
        mock_self._registry = make_entity_registry(*_zha_entries())
        monkeypatch.setattr(trvzb.er, "async_get", lambda hass: mock_self._registry)

        assert await trv_zbt.override_set_valve(mock_self, ENTITY, 40) is True

        assert _number_writes(mock_self) == [(ZHA_OPENING, 40), (ZHA_CLOSING, 40)]

    @pytest.mark.asyncio
    async def test_the_trvzb_keeps_the_complement_on_its_closing_degree(
        self, monkeypatch
    ):
        """The same ZHA numbers on a TRVZB still take 40 and 60."""
        mock_self = _make_self()
        mock_self.real_trvs[ENTITY].model = "TRVZB"
        mock_self._registry = make_entity_registry(*_zha_entries())
        monkeypatch.setattr(trvzb.er, "async_get", lambda hass: mock_self._registry)

        assert await trvzb.maybe_set_sonoff_valve_percent(mock_self, ENTITY, 40)

        assert _number_writes(mock_self) == [(ZHA_OPENING, 40), (ZHA_CLOSING, 60)]

    @pytest.mark.asyncio
    async def test_a_close_goes_out_without_the_trvzb_bump(self, monkeypatch):
        """Closing from 60 to 20 writes 20 once, with no opening step before it."""
        mock_self = _make_self()
        mock_self.real_trvs[ENTITY].last_valve_percent = 60
        monkeypatch.setattr(trvzb.er, "async_get", lambda hass: mock_self._registry)

        assert await trv_zbt.override_set_valve(mock_self, ENTITY, 20) is True

        assert _number_writes(mock_self) == [(HEATING_VALVE, 20), (IDLE_VALVE, 20)]

    def test_the_readings_name_both_numbers(self, monkeypatch):
        """Both numbers are compared against the position that was written."""
        mock_self = _make_self()
        monkeypatch.setattr(trvzb.er, "async_get", lambda hass: mock_self._registry)

        readings = trv_zbt.valve_readings(mock_self, ENTITY, 35)

        assert [r.expected_value for r in readings] == [35.0, 35.0]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("model_id", "model"),
        [
            ("TRV-ZBT", None),
            (None, "TRV-ZBT (Thermostatic radiator valve)"),
            ("OTHER-TRV", None),
        ],
    )
    async def test_the_valve_lookup_finds_the_heating_opening(self, model_id, model):
        """Direct valve control reads the valve back from the heating opening.

        The idle number is listed first, so a lookup that ranked both as a
        valve position would take it.
        """
        bt = ThermostatStandIn()
        device = dr.DeviceEntry(
            config_entry_id="entry",
            model_id=model_id,
            model=model,
            identifiers={("mqtt", DEVICE)},
        )
        registry = make_entity_registry(
            _entry(ENTITY), _entry(IDLE_VALVE), _entry(HEATING_VALVE)
        )

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

        assert result is not None
        assert result["entity_id"] == HEATING_VALVE
        assert result["writable"] is True
