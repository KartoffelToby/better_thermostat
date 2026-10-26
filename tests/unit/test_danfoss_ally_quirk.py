"""Tests for the Danfoss Ally quirk and the white-label devices it drives.

Zigbee2MQTT exposes ``external_measured_room_sensor`` on the Danfoss Ally,
the Popp 701721 and the Hive UK7004240. The device regulates on the room
temperature written there and forgets it after a timeout, so Better
Thermostat keeps it current without writing more often than the device asks.
"""

import importlib
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError
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

ALLY_MODULE = "custom_components.better_thermostat.model_fixes.014G2461"
ally = importlib.import_module(ALLY_MODULE)

ENTITY = "climate.trv1"
DEVICE = "dev1"
ROOM_SENSOR = "sensor.room_temperature"
EXTERNAL_INPUT = "number.trv1_external_measured_room_sensor"
RADIATOR_COVERED = "switch.trv1_radiator_covered"
WINDOW_OPEN_EXTERNAL = "switch.trv1_window_open_external"

# The input as Zigbee2MQTT publishes it: the raw attribute, in hundredths of
# a degree Celsius, with -8000 standing for no value.
Z2M_INPUT_ATTRIBUTES = {"min": -8000, "max": 3500, "step": 1, "mode": "auto"}

ALLY_MODELS = ["014G2461", "014G2463", "701721", "UK7004240"]


def _entry(entity_id, device_id=DEVICE):
    """A registry entry as Zigbee2MQTT registers it, without a translation key."""
    return make_registry_entry(entity_id, device_id=device_id)


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
    input_state="-8000",
    input_attributes=None,
    radiator_covered=None,
    with_input=True,
    room_sensor_device="room_sensor_device",
    calibration_mode="no_calibration",
):
    """A BT stand-in whose only TRV is a Danfoss Ally on Zigbee2MQTT."""
    mock_self = ThermostatStandIn()
    mock_self.device_name = "test_thermostat"
    mock_self.context = MagicMock()
    mock_self.sensor_entity_id = ROOM_SENSOR
    mock_self.hass.services.async_call = AsyncMock()
    advanced: TrvAdvanced = {}
    if calibration_mode is not None:
        advanced["calibration_mode"] = calibration_mode
    mock_self.real_trvs = {
        ENTITY: trv_record(entity_id=ENTITY, model="701721", advanced=advanced)
    }

    entries = [
        _entry(ENTITY),
        _entry(WINDOW_OPEN_EXTERNAL),
        _entry(ROOM_SENSOR, device_id=room_sensor_device),
    ]
    if with_input:
        entries.append(_entry(EXTERNAL_INPUT))
    if radiator_covered is not None:
        entries.append(_entry(RADIATOR_COVERED))
    registry = make_entity_registry(*entries)
    monkeypatch.setattr(ally.er, "async_get", lambda hass: registry)

    states = {
        EXTERNAL_INPUT: State(
            EXTERNAL_INPUT,
            input_state,
            Z2M_INPUT_ATTRIBUTES if input_attributes is None else input_attributes,
        )
    }
    if radiator_covered is not None:
        states[RADIATOR_COVERED] = State(RADIATOR_COVERED, radiator_covered)
    mock_self.hass.states.get.side_effect = states.get
    mock_self._states = states

    clock = _Clock()
    monkeypatch.setattr(ally, "monotonic", clock)
    mock_self._clock = clock
    return mock_self


def _written_values(mock_self):
    """Every value the quirk wrote to the input, in order."""
    payloads = []
    for call in mock_self.hass.services.async_call.await_args_list:
        domain, service, data = call.args[:3]
        assert call.kwargs == {"blocking": True, "context": mock_self.context}
        assert (domain, service, data["entity_id"]) == (
            "number",
            "set_value",
            EXTERNAL_INPUT,
        )
        payloads.append(data["value"])
    return payloads


def _device_holds(mock_self, value):
    """Report ``value`` as what the input holds, as Zigbee2MQTT does after a write."""
    current = mock_self._states[EXTERNAL_INPUT]
    mock_self._states[EXTERNAL_INPUT] = State(
        EXTERNAL_INPUT, str(value), current.attributes
    )


class TestModelDetection:
    """Each model Zigbee2MQTT reports for the device family loads this module."""

    @pytest.mark.parametrize("model", ALLY_MODELS)
    def test_the_model_resolves_onto_the_danfoss_ally_module(self, model):
        """The white-label models have no module of their own."""
        assert quirks.get_model_quirks_name(model) == "014G2461"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", ALLY_MODELS)
    async def test_the_loader_imports_the_danfoss_ally_module(self, model):
        """The import is real, so a path the loader cannot reach lands on default."""
        mock_self = ThermostatStandIn()
        mock_self.device_name = "test_thermostat"

        async def _import(_hass, name):
            return importlib.import_module(name)

        with patch.object(quirks, "async_import_module", side_effect=_import):
            module = await quirks.load_model_quirks(mock_self, model, ENTITY)

        assert module is ally

    def test_the_module_offers_the_external_temperature_write(self):
        """The dispatch only writes the room temperature to such a module."""
        module: object = ally
        assert isinstance(module, ModelQuirks)
        assert isinstance(module, ExternalTemperatureQuirk)

    def test_every_other_hook_is_the_default_one(self):
        """Apart from the room temperature the device runs as before."""
        module: object = ally
        assert isinstance(module, InitialTweakQuirk)
        assert ally.initial_tweak is default_quirk.initial_tweak
        assert (
            ally.trv_state_unknown_as_available
            is default_quirk.trv_state_unknown_as_available
        )
        assert (
            ally.VALVE_MAINTENANCE_INTERVAL_HOURS
            == default_quirk.VALVE_MAINTENANCE_INTERVAL_HOURS
        )

    @pytest.mark.asyncio
    async def test_setpoint_and_mode_writes_are_left_to_the_adapter(self):
        """The required hooks answer as the default quirks do."""
        mock_self = ThermostatStandIn()
        assert ally.fix_local_calibration is default_quirk.fix_local_calibration
        assert (
            ally.fix_target_temperature_calibration
            is default_quirk.fix_target_temperature_calibration
        )
        assert await ally.override_set_temperature(mock_self, ENTITY, 21.0) is False
        assert await ally.override_set_hvac_mode(mock_self, ENTITY, "heat") is False


class TestTheRoomTemperatureReachesTheInput:
    """The room temperature is written in the scale the input publishes."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("room", "written"),
        [
            (20.1, 2010.0),
            (21.456, 2146.0),
            (19.994, 1999.0),
            (40.0, 3500.0),
            (-90.0, -7999.0),
        ],
        ids=["tenths", "hundredths", "rounded-down", "clamped-to-max", "above-min"],
    )
    async def test_the_raw_input_takes_hundredths_of_a_degree(
        self, monkeypatch, room, written
    ):
        """Zigbee2MQTT publishes the raw attribute: 21 °C is written as 2100.

        Its minimum, -8000, stands for no value, so nothing lower than -7999
        is written.
        """
        mock_self = _make_self(monkeypatch)

        assert await ally.maybe_set_external_temperature(mock_self, ENTITY, room)

        assert _written_values(mock_self) == [written]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("unit", "maximum", "written"),
        [("°C", 35.0, 20.1), ("°F", 95.0, pytest.approx(68.18))],
        ids=["celsius", "fahrenheit"],
    )
    async def test_an_input_in_degrees_takes_degrees_of_its_unit(
        self, monkeypatch, unit, maximum, written
    ):
        """An input whose range fits degrees gets the reading in its own unit."""
        mock_self = _make_self(
            monkeypatch,
            input_state="unknown",
            input_attributes={
                "min": -10.0,
                "max": maximum,
                "unit_of_measurement": unit,
            },
        )

        assert await ally.maybe_set_external_temperature(mock_self, ENTITY, 20.14)

        assert _written_values(mock_self) == [written]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("room", "written"),
        [(20.1, 2010.0), (40.0, 3500.0), (-90.0, -7999.0)],
        ids=["inside", "above", "below"],
    )
    async def test_an_input_without_a_state_takes_the_zigbee2mqtt_range(
        self, monkeypatch, room, written
    ):
        """Without published bounds the input counts in hundredths up to 35 °C.

        The lowest value written stays above -8000, which the device reads
        as no value at all.
        """
        mock_self = _make_self(monkeypatch)
        del mock_self._states[EXTERNAL_INPUT]

        assert await ally.maybe_set_external_temperature(mock_self, ENTITY, room)

        assert _written_values(mock_self) == [written]

    @pytest.mark.asyncio
    async def test_an_input_in_degrees_without_a_unit_takes_celsius(self, monkeypatch):
        """A temperature without a declared unit is a Celsius temperature."""
        mock_self = _make_self(
            monkeypatch, input_state="unknown", input_attributes={"max": 35.0}
        )

        assert await ally.maybe_set_external_temperature(mock_self, ENTITY, 20.14)

        assert _written_values(mock_self) == [20.1]

    @pytest.mark.asyncio
    async def test_a_room_sensor_on_the_trv_itself_is_not_written_back(
        self, monkeypatch
    ):
        """The TRV's own reading fed back into it would be its own measurement."""
        mock_self = _make_self(monkeypatch, room_sensor_device=DEVICE)

        assert await ally.maybe_set_external_temperature(mock_self, ENTITY, 20.1) is (
            False
        )
        mock_self.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_device_without_the_input_gets_no_write(self, monkeypatch):
        """Nothing is written to any other number of the device."""
        mock_self = _make_self(monkeypatch, with_input=False)

        assert await ally.maybe_set_external_temperature(mock_self, ENTITY, 20.1) is (
            False
        )
        mock_self.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_trv_without_a_device_gets_no_write(self, monkeypatch):
        """Without a device there is no input to find."""
        mock_self = _make_self(monkeypatch)
        monkeypatch.setattr(
            ally.er,
            "async_get",
            lambda hass: make_entity_registry(_entry(ENTITY, None)),
        )

        assert await ally.maybe_set_external_temperature(mock_self, ENTITY, 20.1) is (
            False
        )
        mock_self.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reading", ["warm", float("inf"), float("nan")])
    async def test_a_reading_that_is_no_finite_number_is_not_written(
        self, monkeypatch, reading
    ):
        """The input takes finite numbers only."""
        mock_self = _make_self(monkeypatch)

        assert (
            await ally.maybe_set_external_temperature(mock_self, ENTITY, reading)
            is False
        )
        mock_self.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_refused_write_is_retried_on_the_next_reading(self, monkeypatch):
        """A write the device did not take counts as no write."""
        mock_self = _make_self(monkeypatch)
        mock_self.hass.services.async_call.side_effect = [
            HomeAssistantError("device asleep"),
            None,
        ]

        assert await ally.maybe_set_external_temperature(mock_self, ENTITY, 20.1) is (
            False
        )
        assert await ally.maybe_set_external_temperature(mock_self, ENTITY, 20.1)

        assert _written_values(mock_self) == [2010.0, 2010.0]


class TestOnlyAPassedThroughTargetGetsTheValue:
    """The device corrects for its sensor bias itself once it has the value."""

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

        assert await ally.maybe_set_external_temperature(mock_self, ENTITY, 20.1) is (
            False
        )
        assert _written_values(mock_self) == []


class TestWritesFollowTheDeviceRateLimit:
    """An unchanged temperature is written no more often than the device asks."""

    @staticmethod
    async def _write_then(mock_self, seconds, room):
        """Write 20.1 °C, let ``seconds`` pass, then offer ``room``."""
        assert await ally.maybe_set_external_temperature(mock_self, ENTITY, 20.1)
        _device_holds(mock_self, 2010)
        mock_self._clock.advance(seconds)
        return await ally.maybe_set_external_temperature(mock_self, ENTITY, room)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("room", [20.1, 20.15, 20.05])
    async def test_a_small_change_within_the_refresh_interval_is_not_written(
        self, monkeypatch, room
    ):
        """Below 0.1 °C of change, Auto Offset Mode waits for the refresh."""
        mock_self = _make_self(monkeypatch, radiator_covered="off")

        assert await self._write_then(mock_self, 29 * 60, room) is False
        assert _written_values(mock_self) == [2010.0]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("room", "written"), [(20.2, 2020.0), (20.0, 2000.0)], ids=["up", "down"]
    )
    async def test_a_change_of_a_tenth_of_a_degree_is_written_at_once(
        self, monkeypatch, room, written
    ):
        """The device takes a new value on every 0.1 °C of change."""
        mock_self = _make_self(monkeypatch)

        assert await self._write_then(mock_self, 1, room)
        assert _written_values(mock_self) == [2010.0, written]

    @pytest.mark.asyncio
    async def test_auto_offset_mode_refreshes_an_unchanged_value(self, monkeypatch):
        """Without radiator_covered on, the refresh follows after 30 minutes."""
        mock_self = _make_self(monkeypatch, radiator_covered="off")

        assert await self._write_then(mock_self, 30 * 60, 20.1)
        assert _written_values(mock_self) == [2010.0, 2010.0]

    @pytest.mark.asyncio
    async def test_a_device_without_the_mode_switch_counts_as_auto_offset(
        self, monkeypatch
    ):
        """Auto Offset Mode is the device default, so its interval applies."""
        mock_self = _make_self(monkeypatch)

        assert await self._write_then(mock_self, 6 * 60, 20.1) is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("seconds", [60, 4 * 60, 30 * 60])
    async def test_room_sensor_mode_takes_every_offer(self, monkeypatch, seconds):
        """With radiator_covered on, no offer of an unchanged value is skipped.

        The refresh is offered every 30 minutes and the device drops the
        value after 35, so a skipped offer would leave it running out.
        """
        mock_self = _make_self(monkeypatch, radiator_covered="on")

        assert await self._write_then(mock_self, seconds, 20.1)
        assert _written_values(mock_self) == [2010.0, 2010.0]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reported", ["unavailable", "unknown"])
    async def test_a_mode_switch_without_a_state_counts_as_room_sensor_mode(
        self, monkeypatch, reported
    ):
        """A switch that reports nothing may hide the mode with the short timeout."""
        mock_self = _make_self(monkeypatch, radiator_covered=reported)

        assert await self._write_then(mock_self, 60, 20.1)

    @pytest.mark.asyncio
    async def test_the_state_from_before_the_write_does_not_count_as_dropped(
        self, monkeypatch
    ):
        """Until Zigbee2MQTT publishes the written value, the old one still shows.

        Readings that arrive in that window are not written again for it.
        """
        mock_self = _make_self(monkeypatch)
        assert await ally.maybe_set_external_temperature(mock_self, ENTITY, 20.1)
        mock_self._clock.advance(10)
        assert await ally.maybe_set_external_temperature(mock_self, ENTITY, 20.13) is (
            False
        )
        mock_self._clock.advance(10)
        assert await ally.maybe_set_external_temperature(mock_self, ENTITY, 20.12) is (
            False
        )
        assert _written_values(mock_self) == [2010.0]

    @pytest.mark.asyncio
    async def test_a_device_that_dropped_the_value_gets_it_again(self, monkeypatch):
        """A device back on -8000 has fallen back to its own sensor."""
        mock_self = _make_self(monkeypatch)
        assert await ally.maybe_set_external_temperature(mock_self, ENTITY, 20.1)
        _device_holds(mock_self, -8000)
        mock_self._clock.advance(60)

        assert await ally.maybe_set_external_temperature(mock_self, ENTITY, 20.1)
        assert _written_values(mock_self) == [2010.0, 2010.0]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reported", ["unavailable", "not-a-number"])
    async def test_a_device_that_reports_nothing_keeps_the_rate_limit(
        self, monkeypatch, reported
    ):
        """An input that reports no temperature gives no reason to write again."""
        mock_self = _make_self(monkeypatch)
        assert await ally.maybe_set_external_temperature(mock_self, ENTITY, 20.1)
        _device_holds(mock_self, reported)
        mock_self._clock.advance(60)

        assert await ally.maybe_set_external_temperature(mock_self, ENTITY, 20.1) is (
            False
        )
