"""What model quirks and the offset channel read off the device they drive.

A quirk reads the device's own report (its temperature, its mode select)
and the offset channel reads the grid its entity
offers. Each reading has to come out the same whatever unit the system runs
in, whichever of its identifiers a user renamed, and whatever grid the
entity publishes.
"""

import importlib
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import UnitOfTemperature
from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.unit_conversion import TemperatureConverter
import pytest

from custom_components.better_thermostat.adapters import base, delegate, generic
from custom_components.better_thermostat.model_fixes import SPZB0001
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.helpers import round_by_step

ENTITY_ID = "climate.trv"
CALIBRATION_ENTITY = "select.trv_local_temperature_calibration"

BOOSTING_QUIRKS = {
    "TS0601": importlib.import_module(
        "custom_components.better_thermostat.model_fixes.TS0601"
    ),
    "TS0601_thermostat": importlib.import_module(
        "custom_components.better_thermostat.model_fixes.TS0601_thermostat"
    ),
    "SEA801-Zigbee_SEA802-Zigbee": importlib.import_module(
        "custom_components.better_thermostat.model_fixes.SEA801-Zigbee_SEA802-Zigbee"
    ),
}

# Setpoint the TRV lands on for a 21 °C request while its sensor reads
# 20 °C, which is within 1.5 K below the request.
BOOSTED = {
    "TS0601": 22.5,
    "TS0601_thermostat": 22.5,
    "SEA801-Zigbee_SEA802-Zigbee": 21.5,
}


def _host(unit=UnitOfTemperature.CELSIUS, state=None, advanced=None):
    host = MagicMock()
    host.device_name = "Test BT"
    host.context = None
    host.hass = MagicMock()
    host.hass.config.units.temperature_unit = unit
    host.hass.states.get = MagicMock(return_value=state)
    host.hass.services.async_call = AsyncMock(return_value=None)
    trv = Trv.from_legacy_dict(ENTITY_ID, {})
    trv.advanced = advanced or {}
    host.real_trvs = {ENTITY_ID: trv}
    return host


def _in_system_unit(celsius, unit):
    if unit == UnitOfTemperature.CELSIUS:
        return celsius
    return TemperatureConverter.convert(
        celsius, UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT
    )


class TestTheSetpointBoostReadsTheTrvInCelsius:
    """The minimum-gap boost compares two Celsius temperatures."""

    @pytest.mark.parametrize("name", sorted(BOOSTING_QUIRKS))
    @pytest.mark.parametrize(
        "unit", [UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT]
    )
    def test_a_setpoint_just_above_the_trv_is_boosted(self, name, unit):
        """A Fahrenheit system reports the TRV's temperature in °F."""
        state = State(
            ENTITY_ID, "heat", {"current_temperature": _in_system_unit(20.0, unit)}
        )
        host = _host(unit=unit, state=state)

        answer = BOOSTING_QUIRKS[name].fix_target_temperature_calibration(
            host, ENTITY_ID, 21.0
        )

        assert answer == pytest.approx(BOOSTED[name])

    @pytest.mark.parametrize("name", sorted(BOOSTING_QUIRKS))
    @pytest.mark.parametrize(
        "unit", [UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT]
    )
    def test_a_setpoint_below_the_trv_is_left_alone(self, name, unit):
        """The boost only lifts a setpoint that asks for heat."""
        state = State(
            ENTITY_ID, "heat", {"current_temperature": _in_system_unit(22.0, unit)}
        )
        host = _host(unit=unit, state=state)

        answer = BOOSTING_QUIRKS[name].fix_target_temperature_calibration(
            host, ENTITY_ID, 21.0
        )

        assert answer == pytest.approx(21.0)


class TestTheEurotronicModeSelectIsFoundByAnyOfItsNames:
    """SPZB0001 finds the TRV mode select by entity id, unique id or name."""

    @pytest.mark.parametrize(
        ("select_id", "unique_id", "original_name"),
        [
            ("select.trv_trv_mode", "0x1234_mode", "Mode"),
            ("select.wohnzimmer_modus", "0x1234_trv_mode", "Mode"),
            ("select.wohnzimmer_modus", "0x1234_mode", "Trv mode"),
        ],
        ids=["entity_id", "unique_id", "original_name"],
    )
    @pytest.mark.asyncio
    async def test_the_mode_is_written(self, select_id, unique_id, original_name):
        """A renamed select still carries the name the device gave it."""
        climate_entry = MagicMock(
            entity_id=ENTITY_ID,
            domain="climate",
            device_id="device1",
            unique_id="0x1234",
            original_name="TRV",
            disabled_by=None,
        )
        select_entry = MagicMock(
            entity_id=select_id,
            domain="select",
            device_id="device1",
            unique_id=unique_id,
            original_name=original_name,
            disabled_by=None,
        )
        registry = MagicMock()
        registry.async_get.return_value = climate_entry
        registry.entities.values.return_value = [climate_entry, select_entry]
        host = _host(state=State(select_id, "2"))

        with patch.object(SPZB0001.er, "async_get", lambda hass: registry):
            answered = await SPZB0001.check_operation_mode(host, ENTITY_ID, "1")

        assert answered is True
        host.hass.services.async_call.assert_awaited_once_with(
            "select",
            "select_option",
            {"entity_id": select_id, "option": "1"},
            blocking=True,
            context=None,
        )


def _select_host(options):
    state = State(CALIBRATION_ENTITY, "0.0k", {"options": options})
    host = _host(state=state)
    host.real_trvs[ENTITY_ID].local_temperature_calibration_entity = CALIBRATION_ENTITY
    return host


class TestASelectCalibrationEntityPublishesItsOptionGrid:
    """The offset step of a select is the spacing of the options it offers."""

    @pytest.mark.parametrize(
        ("options", "step"),
        [
            (["-1.0k", "-0.5k", "0.0k", "0.5k", "1.0k", "1.5k"], 0.5),
            (["-3.0k", "0.0k", "3.0k"], 3.0),
            (["1.0k", "-0.2k", "0.0k", "0.2k"], 0.2),
        ],
        ids=["half", "three", "unsorted_fifth"],
    )
    @pytest.mark.asyncio
    async def test_the_step_is_the_option_spacing(self, options, step):
        """Rounding the offset to 1.0 would drop what a finer grid offers."""
        host = _select_host(options)

        assert await generic.get_offset_step(host, ENTITY_ID) == pytest.approx(step)

    @pytest.mark.parametrize("options", [[], ["0.0k"], ["on", "off"]])
    @pytest.mark.asyncio
    async def test_a_select_without_a_grid_answers_the_default(self, options):
        """Fewer than two numeric options carry no spacing."""
        host = _select_host(options)

        assert await generic.get_offset_step(host, ENTITY_ID) == 1.0

    @pytest.mark.asyncio
    async def test_a_half_kelvin_request_reaches_the_device(self):
        """1.5 K on a 0.5 K grid is written as 1.5 K, not rounded to 1.0 K."""
        host = _select_host(["-1.0k", "-0.5k", "0.0k", "0.5k", "1.0k", "1.5k"])
        host.real_trvs[ENTITY_ID].adapter = generic
        step = await delegate.get_offset_step(host, ENTITY_ID)

        await generic.set_offset(host, ENTITY_ID, round_by_step(1.5, step))

        host.hass.services.async_call.assert_awaited_once()
        assert host.hass.services.async_call.await_args.args[2]["option"] == "1.5k"


class TestTheForcedZeroCalibrationWaitsForItsAnswer:
    """The zero offset written after the startup wait reports a refusal."""

    @pytest.mark.parametrize(
        "calibration_entity",
        ["number.trv_local_temperature_calibration", CALIBRATION_ENTITY],
    )
    @pytest.mark.asyncio
    async def test_the_write_blocks(self, calibration_entity):
        """Only a blocking call raises the device's refusal to the caller."""
        host = _host()

        await base._write_zero_calibration(host, calibration_entity, None)

        assert host.hass.services.async_call.await_args.kwargs["blocking"] is True

    @pytest.mark.asyncio
    async def test_a_refused_zero_is_logged(self, hass, caplog):
        """A refused zero calibration leaves a trace in the log."""

        async def refuse(call):
            raise HomeAssistantError("refused")

        hass.services.async_register("number", "set_value", refuse)
        host = _host()
        host.hass = hass

        with patch.object(base.asyncio, "sleep", new=AsyncMock()):
            await base.wait_for_calibration_entity_or_timeout(
                host, ENTITY_ID, "number.trv_local_temperature_calibration"
            )
        await hass.async_block_till_done()

        assert "Failed to set calibration to 0" in caplog.text
