"""Tests for the Shelly BLU TRV quirk.

With its own thermostat enabled, the Shelly BLU TRV regulates on a room
temperature from outside, which Home Assistant's Shelly integration exposes
as the ``external_temperature`` number on the TRV's device. A TRV that never
gets one reports ``ext_temp_missing``, so Better Thermostat keeps the number
current with the room temperature it regulates on.
"""

import importlib
import logging
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
import pytest

from custom_components.better_thermostat.model_fixes import (
    default as default_quirk,
    model_quirks as quirks,
)
from custom_components.better_thermostat.model_fixes.types import (
    ExternalTemperatureQuirk,
    InitialTweakQuirk,
    ModelQuirks,
)
from custom_components.better_thermostat.utils.entry_schema import TrvAdvanced
from tests.factories import (
    ThermostatStandIn,
    make_entity_registry,
    make_registry_entry,
    trv_record,
)

BLU_TRV_MODULE = "custom_components.better_thermostat.model_fixes.SBTR-001AEU"
blu_trv = importlib.import_module(BLU_TRV_MODULE)

ENTITY = "climate.blu_trv"
DEVICE = "blu_trv_device"
ROOM_SENSOR = "sensor.room_temperature"
EXTERNAL_TEMPERATURE = "number.blu_trv_external_temperature"
VALVE_POSITION = "number.blu_trv_valve_position"

# The number as the Shelly integration publishes it on a Celsius system.
CELSIUS_ATTRIBUTES = {
    "min": -50.0,
    "max": 50.0,
    "step": 0.1,
    "mode": "box",
    "unit_of_measurement": "°C",
}

# The model id and the model name the Shelly integration registers.
BLU_TRV_MODELS = ["SBTR-001AEU", "Shelly BLU TRV"]


def _shelly_entry(entity_id, key, *, disabled_by=None, device_id=DEVICE):
    """A registry entry as the Shelly integration registers a BLU TRV number."""
    return make_registry_entry(
        entity_id,
        unique_id=f"aa:bb:cc:dd:ee:ff-blutrv:200-{key}",
        platform="shelly",
        device_id=device_id,
        translation_key=key,
        disabled_by=disabled_by,
    )


class _Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _make_self(
    monkeypatch,
    *,
    number_state="unknown",
    number_attributes=None,
    with_number=True,
    number_disabled_by=None,
    room_sensor_device="room_sensor_device",
    calibration_mode="no_calibration",
    calibration="target_temp_based",
):
    """A BT stand-in whose only TRV is a Shelly BLU TRV behind a gateway."""
    mock_self = ThermostatStandIn()
    mock_self.device_name = "test_thermostat"
    mock_self.context = MagicMock()
    mock_self.sensor_entity_id = ROOM_SENSOR
    mock_self.hass.services.async_call = AsyncMock()
    advanced: TrvAdvanced = {"calibration": calibration}
    if calibration_mode is not None:
        advanced["calibration_mode"] = calibration_mode
    mock_self.real_trvs = {
        ENTITY: trv_record(entity_id=ENTITY, model="SBTR-001AEU", advanced=advanced)
    }

    entries = [
        make_registry_entry(ENTITY, platform="shelly", device_id=DEVICE),
        _shelly_entry(VALVE_POSITION, "valve_position"),
        make_registry_entry(ROOM_SENSOR, device_id=room_sensor_device),
    ]
    if with_number:
        entries.append(
            _shelly_entry(
                EXTERNAL_TEMPERATURE,
                "external_temperature",
                disabled_by=number_disabled_by,
            )
        )
    registry = make_entity_registry(*entries)
    monkeypatch.setattr(blu_trv.er, "async_get", lambda hass: registry)
    mock_self._registry = registry

    states = {
        EXTERNAL_TEMPERATURE: State(
            EXTERNAL_TEMPERATURE,
            number_state,
            CELSIUS_ATTRIBUTES if number_attributes is None else number_attributes,
        )
    }
    mock_self.hass.states.get.side_effect = states.get
    mock_self._states = states

    clock = _Clock()
    monkeypatch.setattr(blu_trv, "monotonic", clock)
    mock_self._clock = clock
    return mock_self


def _written_values(mock_self):
    """Every value the quirk wrote to the number, in order."""
    payloads = []
    for call in mock_self.hass.services.async_call.await_args_list:
        domain, service, data = call.args[:3]
        assert call.kwargs == {"blocking": True, "context": mock_self.context}
        assert (domain, service, data["entity_id"]) == (
            "number",
            "set_value",
            EXTERNAL_TEMPERATURE,
        )
        payloads.append(data["value"])
    return payloads


def _number_holds(mock_self, value):
    """Report ``value`` as what the number holds, as Home Assistant does after a write."""
    current = mock_self._states[EXTERNAL_TEMPERATURE]
    mock_self._states[EXTERNAL_TEMPERATURE] = State(
        EXTERNAL_TEMPERATURE, str(value), current.attributes
    )


class TestModelDetection:
    """Both names the Shelly integration gives the TRV load this module."""

    def test_the_model_name_resolves_onto_the_model_id(self):
        """A device entry that carries only the model name finds the module."""
        assert quirks.get_model_quirks_name("Shelly BLU TRV") == "SBTR-001AEU"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", BLU_TRV_MODELS)
    async def test_the_loader_imports_the_blu_trv_module(self, model):
        """The import is real, so a path the loader cannot reach lands on default."""
        mock_self = ThermostatStandIn()
        mock_self.device_name = "test_thermostat"

        async def _import(_hass, name):
            return importlib.import_module(name)

        with patch.object(quirks, "async_import_module", side_effect=_import):
            module = await quirks.load_model_quirks(mock_self, model, ENTITY)

        assert module is blu_trv

    def test_the_module_offers_the_external_temperature_write(self):
        """The dispatch only writes the room temperature to such a module."""
        module: object = blu_trv
        assert isinstance(module, ModelQuirks)
        assert isinstance(module, ExternalTemperatureQuirk)

    def test_every_other_hook_is_the_default_one(self):
        """Apart from the room temperature the TRV runs as before."""
        module: object = blu_trv
        assert isinstance(module, InitialTweakQuirk)
        assert blu_trv.initial_tweak is default_quirk.initial_tweak
        assert (
            blu_trv.trv_state_unknown_as_available
            is default_quirk.trv_state_unknown_as_available
        )
        assert (
            blu_trv.VALVE_MAINTENANCE_INTERVAL_HOURS
            == default_quirk.VALVE_MAINTENANCE_INTERVAL_HOURS
        )
        assert blu_trv.fix_local_calibration is default_quirk.fix_local_calibration
        assert (
            blu_trv.fix_target_temperature_calibration
            is default_quirk.fix_target_temperature_calibration
        )
        assert (
            blu_trv.override_set_temperature is default_quirk.override_set_temperature
        )
        assert blu_trv.override_set_hvac_mode is default_quirk.override_set_hvac_mode


class TestTheRoomTemperatureReachesTheNumber:
    """The room temperature is written in the unit and range the number publishes."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("room", "written"),
        [(20.14, 20.1), (21.46, 21.5), (60.0, 50.0), (-70.0, -50.0)],
        ids=["tenths", "rounded-up", "clamped-to-max", "clamped-to-min"],
    )
    async def test_a_celsius_number_takes_tenths_of_a_degree(
        self, monkeypatch, room, written
    ):
        """The number steps in tenths of a degree from -50 to 50 °C."""
        mock_self = _make_self(monkeypatch)

        assert await blu_trv.maybe_set_external_temperature(mock_self, ENTITY, room)

        assert _written_values(mock_self) == [written]

    @pytest.mark.asyncio
    async def test_a_fahrenheit_system_gets_the_reading_in_fahrenheit(
        self, monkeypatch
    ):
        """Home Assistant publishes the number, and checks a write, in the system unit."""
        mock_self = _make_self(
            monkeypatch,
            number_attributes={"min": -58.0, "max": 122.0, "unit_of_measurement": "°F"},
        )

        assert await blu_trv.maybe_set_external_temperature(mock_self, ENTITY, 20.14)

        assert _written_values(mock_self) == [pytest.approx(68.18)]

    @pytest.mark.asyncio
    async def test_a_number_without_a_state_takes_the_celsius_range(self, monkeypatch):
        """Without published bounds the number counts in Celsius from -50 to 50."""
        mock_self = _make_self(monkeypatch)
        del mock_self._states[EXTERNAL_TEMPERATURE]

        assert await blu_trv.maybe_set_external_temperature(mock_self, ENTITY, 60.0)

        assert _written_values(mock_self) == [50.0]

    @pytest.mark.asyncio
    async def test_a_number_registered_without_a_translation_key_is_found(
        self, monkeypatch
    ):
        """The unique id the Shelly integration gives the number names it too."""
        mock_self = _make_self(monkeypatch, with_number=False)
        registry = make_entity_registry(
            *mock_self._registry.entities.values(),
            make_registry_entry(
                EXTERNAL_TEMPERATURE,
                unique_id="aa:bb:cc:dd:ee:ff-blutrv:200-external_temperature",
                platform="shelly",
                device_id=DEVICE,
            ),
        )
        monkeypatch.setattr(blu_trv.er, "async_get", lambda hass: registry)

        assert await blu_trv.maybe_set_external_temperature(mock_self, ENTITY, 20.1)

        assert _written_values(mock_self) == [20.1]

    @pytest.mark.asyncio
    async def test_a_room_sensor_on_the_trv_itself_is_not_written_back(
        self, monkeypatch
    ):
        """The TRV's own reading fed back into it would be its own measurement."""
        mock_self = _make_self(monkeypatch, room_sensor_device=DEVICE)

        assert (
            await blu_trv.maybe_set_external_temperature(mock_self, ENTITY, 20.1)
            is False
        )
        mock_self.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_trv_without_a_device_gets_no_write(self, monkeypatch):
        """Without a device there is no number to find."""
        mock_self = _make_self(monkeypatch)
        monkeypatch.setattr(
            blu_trv.er,
            "async_get",
            lambda hass: make_entity_registry(
                make_registry_entry(ENTITY, platform="shelly", device_id=None)
            ),
        )

        assert (
            await blu_trv.maybe_set_external_temperature(mock_self, ENTITY, 20.1)
            is False
        )
        mock_self.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reading", ["warm", float("inf"), float("nan")])
    async def test_a_reading_that_is_no_finite_number_is_not_written(
        self, monkeypatch, reading
    ):
        """The number takes finite numbers only."""
        mock_self = _make_self(monkeypatch)

        assert (
            await blu_trv.maybe_set_external_temperature(mock_self, ENTITY, reading)
            is False
        )
        mock_self.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_failed_write_is_retried_on_the_next_reading(self, monkeypatch):
        """A write the gateway did not take counts as no write."""
        mock_self = _make_self(monkeypatch)
        mock_self.hass.services.async_call.side_effect = [
            HomeAssistantError("gateway out of reach"),
            None,
        ]

        assert (
            await blu_trv.maybe_set_external_temperature(mock_self, ENTITY, 20.1)
            is False
        )
        assert await blu_trv.maybe_set_external_temperature(mock_self, ENTITY, 20.1)

        assert _written_values(mock_self) == [20.1, 20.1]


class TestOnlyATrvThatRegulatesOnTheValueGetsIt:
    """The TRV corrects its regulation with the value; nothing else may do it twice."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "calibration_mode",
        [None, "default", "heating_power_calibration", "mpc_v2_calibration"],
    )
    async def test_a_calibrating_trv_is_not_written(
        self, monkeypatch, calibration_mode
    ):
        """A calibration on top would correct the same bias a second time."""
        mock_self = _make_self(monkeypatch, calibration_mode=calibration_mode)

        assert (
            await blu_trv.maybe_set_external_temperature(mock_self, ENTITY, 20.1)
            is False
        )
        assert _written_values(mock_self) == []

    @pytest.mark.asyncio
    async def test_a_trv_under_direct_valve_control_is_not_written(
        self, monkeypatch, caplog
    ):
        """Its own thermostat is switched off and reads no room temperature."""
        mock_self = _make_self(
            monkeypatch, calibration="direct_valve_based", with_number=False
        )

        with caplog.at_level(logging.WARNING):
            assert (
                await blu_trv.maybe_set_external_temperature(mock_self, ENTITY, 20.1)
                is False
            )

        assert _written_values(mock_self) == []
        assert caplog.records == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "calibration", ["target_temp_based", "local_calibration_based"]
    )
    async def test_a_trv_passed_the_target_unchanged_is_written(
        self, monkeypatch, calibration
    ):
        """Both setpoint calibration types leave the target alone under no_calibration."""
        mock_self = _make_self(monkeypatch, calibration=calibration)

        assert await blu_trv.maybe_set_external_temperature(mock_self, ENTITY, 20.1)
        assert _written_values(mock_self) == [20.1]


class TestAMissingNumberIsReportedOnce:
    """A TRV that would take the value but has no number for it says so in the log."""

    @staticmethod
    def _warnings(caplog):
        return [
            record
            for record in caplog.records
            if record.levelno == logging.WARNING
            and "no enabled external temperature number" in record.getMessage()
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "number_disabled_by",
        [None, er.RegistryEntryDisabler.USER, er.RegistryEntryDisabler.INTEGRATION],
        ids=["missing", "disabled-by-user", "disabled-by-integration"],
    )
    async def test_the_warning_is_logged_once_and_not_every_cycle(
        self, monkeypatch, caplog, number_disabled_by
    ):
        """Every reading and keepalive asks again; the log hears of it once."""
        mock_self = _make_self(
            monkeypatch,
            with_number=number_disabled_by is not None,
            number_disabled_by=number_disabled_by,
        )

        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                assert (
                    await blu_trv.maybe_set_external_temperature(
                        mock_self, ENTITY, 20.1
                    )
                    is False
                )

        warnings = self._warnings(caplog)
        assert len(warnings) == 1
        assert ENTITY in warnings[0].getMessage()
        assert "ext_temp_missing" in warnings[0].getMessage()
        mock_self.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_calibrating_trv_raises_no_warning(self, monkeypatch, caplog):
        """A TRV that is not written does not miss the number."""
        mock_self = _make_self(
            monkeypatch, with_number=False, calibration_mode="default"
        )

        with caplog.at_level(logging.WARNING):
            await blu_trv.maybe_set_external_temperature(mock_self, ENTITY, 20.1)

        assert self._warnings(caplog) == []

    @pytest.mark.asyncio
    async def test_a_number_that_comes_back_rearms_the_warning(
        self, monkeypatch, caplog
    ):
        """Once the number is enabled it is written; losing it again is reported again."""
        mock_self = _make_self(
            monkeypatch, number_disabled_by=er.RegistryEntryDisabler.USER
        )
        disabled = mock_self._registry
        enabled = make_entity_registry(
            *(
                entry
                for entry in disabled.entities.values()
                if entry.entity_id != EXTERNAL_TEMPERATURE
            ),
            _shelly_entry(EXTERNAL_TEMPERATURE, "external_temperature"),
        )
        current = {"registry": disabled}
        monkeypatch.setattr(blu_trv.er, "async_get", lambda hass: current["registry"])

        with caplog.at_level(logging.WARNING):
            assert not await blu_trv.maybe_set_external_temperature(
                mock_self, ENTITY, 20.1
            )
            current["registry"] = enabled
            assert await blu_trv.maybe_set_external_temperature(mock_self, ENTITY, 20.1)
            current["registry"] = disabled
            assert not await blu_trv.maybe_set_external_temperature(
                mock_self, ENTITY, 20.1
            )

        assert len(self._warnings(caplog)) == 2
        assert _written_values(mock_self) == [20.1]


class TestTheValueIsKeptCurrent:
    """An unchanged temperature is written again before the TRV can give up on it."""

    @staticmethod
    async def _write_then(mock_self, seconds, room):
        """Write 20.1 °C, let ``seconds`` pass, then offer ``room``."""
        assert await blu_trv.maybe_set_external_temperature(mock_self, ENTITY, 20.1)
        _number_holds(mock_self, 20.1)
        mock_self._clock.advance(seconds)
        return await blu_trv.maybe_set_external_temperature(mock_self, ENTITY, room)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("room", [20.1, 20.14, 20.06])
    async def test_an_unchanged_value_offered_again_soon_is_not_written(
        self, monkeypatch, room
    ):
        """A repeated offer within five minutes adds nothing the TRV lacks."""
        mock_self = _make_self(monkeypatch)

        assert await self._write_then(mock_self, 4 * 60, room) is False
        assert _written_values(mock_self) == [20.1]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("room", "written"), [(20.2, 20.2), (20.0, 20.0)], ids=["up", "down"]
    )
    async def test_a_change_of_a_tenth_of_a_degree_is_written_at_once(
        self, monkeypatch, room, written
    ):
        """The number steps in tenths, and every step reaches the TRV."""
        mock_self = _make_self(monkeypatch)

        assert await self._write_then(mock_self, 1, room)
        assert _written_values(mock_self) == [20.1, written]

    @pytest.mark.asyncio
    async def test_every_keepalive_offer_is_written(self, monkeypatch):
        """The keepalive offers every 30 minutes; each offer lands on the TRV.

        The TRV does not publish after how long it drops a value, so the
        refresh stays well inside the keepalive interval.
        """
        mock_self = _make_self(monkeypatch)

        assert await self._write_then(mock_self, 5 * 60, 20.1)
        assert _written_values(mock_self) == [20.1, 20.1]

    @pytest.mark.asyncio
    async def test_the_state_from_before_the_write_does_not_count_as_dropped(
        self, monkeypatch
    ):
        """Until the number shows the written value, the old one still shows."""
        mock_self = _make_self(monkeypatch, number_state="18.0")
        assert await blu_trv.maybe_set_external_temperature(mock_self, ENTITY, 20.1)
        mock_self._clock.advance(10)

        assert (
            await blu_trv.maybe_set_external_temperature(mock_self, ENTITY, 20.12)
            is False
        )
        assert _written_values(mock_self) == [20.1]

    @pytest.mark.asyncio
    async def test_a_value_changed_by_hand_is_overwritten(self, monkeypatch):
        """A number set to something else by hand no longer holds the room temperature."""
        mock_self = _make_self(monkeypatch)
        assert await blu_trv.maybe_set_external_temperature(mock_self, ENTITY, 20.1)
        _number_holds(mock_self, 23.0)
        mock_self._clock.advance(60)

        assert await blu_trv.maybe_set_external_temperature(mock_self, ENTITY, 20.1)
        assert _written_values(mock_self) == [20.1, 20.1]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reported", ["unknown", "unavailable", "not-a-number"])
    async def test_a_number_that_reports_nothing_keeps_the_refresh(
        self, monkeypatch, reported
    ):
        """After a restart the number shows no value; that alone is no reason to write."""
        mock_self = _make_self(monkeypatch)
        assert await blu_trv.maybe_set_external_temperature(mock_self, ENTITY, 20.1)
        _number_holds(mock_self, reported)
        mock_self._clock.advance(60)

        assert (
            await blu_trv.maybe_set_external_temperature(mock_self, ENTITY, 20.1)
            is False
        )
