"""Tests for the ZVIDAR Z-TRV-V01 model quirks.

These tests assert that the quirk only engages its manufacturer-specific valve
mode when the TRV is configured for direct valve control; in every other case it
declines and the standard climate path is used.
"""

import importlib
from unittest.mock import AsyncMock, MagicMock

from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.const import CalibrationType

quirk = importlib.import_module(
    "custom_components.better_thermostat.model_fixes.Z-TRV-V01"
)


def _make_self(calibration=None, state=None):
    """Create a mock BetterThermostat with a spied service-call layer."""
    mock_self = MagicMock()
    mock_self.device_name = "test_thermostat"
    mock_self.context = MagicMock()
    mock_self.hass.services.async_call = AsyncMock()
    mock_self.hass.states.get = MagicMock(return_value=state)
    mock_self.real_trvs = {
        "climate.trv1": Trv(
            entity_id="climate.trv1", advanced={"calibration": calibration}
        )
    }
    return mock_self


class TestHvacOverride:
    """The manufacturer-specific mode is engaged only for direct valve control."""

    @pytest.mark.asyncio
    async def test_declines_when_not_direct_valve(self):
        """Non valve-based calibration falls through to the standard path."""
        mock_self = _make_self(calibration=CalibrationType.TARGET_TEMP_BASED)

        handled = await quirk.override_set_hvac_mode(mock_self, "climate.trv1", "heat")

        assert handled is False
        mock_self.hass.services.async_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_declines_for_off_even_in_valve_mode(self):
        """OFF uses the standard path so the device closes normally."""
        mock_self = _make_self(calibration=CalibrationType.DIRECT_VALVE_BASED)

        handled = await quirk.override_set_hvac_mode(mock_self, "climate.trv1", "off")

        assert handled is False
        mock_self.hass.services.async_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_engages_manufacturer_mode_for_valve_heat(self):
        """Valve mode + a heating request switches the device into mode 31."""
        mock_self = _make_self(calibration=CalibrationType.DIRECT_VALVE_BASED)

        handled = await quirk.override_set_hvac_mode(mock_self, "climate.trv1", "heat")

        assert handled is True
        mock_self.hass.services.async_call.assert_awaited_once()
        args, _ = mock_self.hass.services.async_call.call_args
        assert args[0] == "zwave_js"
        assert args[2]["entity_id"] == "climate.trv1"
        assert args[2]["property"] == "mode"
        assert args[2]["value"] == "31"
        assert args[2]["command_class"] == "64"

    @pytest.mark.asyncio
    async def test_engaged_mode_is_not_rewritten_while_unknown(self):
        """A device still reading unknown keeps the mode without a fresh write."""
        mock_self = _make_self(
            calibration=CalibrationType.DIRECT_VALVE_BASED,
            state=State("climate.trv1", STATE_UNKNOWN),
        )
        mock_self.real_trvs["climate.trv1"].extra["_z_trv_v01_valve_mode_engaged"] = (
            True
        )

        handled = await quirk.override_set_hvac_mode(mock_self, "climate.trv1", "heat")

        assert handled is True
        mock_self.hass.services.async_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_declines_a_refused_mode_write(self):
        """A refused mode write declines so the standard climate path runs."""
        mock_self = _make_self(calibration=CalibrationType.DIRECT_VALVE_BASED)
        mock_self.hass.services.async_call = AsyncMock(side_effect=HomeAssistantError)

        handled = await quirk.override_set_hvac_mode(mock_self, "climate.trv1", "heat")

        assert handled is False


class TestPassthroughs:
    """The remaining quirks are safe no-ops."""

    def test_fix_calibrations_are_identity(self):
        """The calibration fix helpers return their inputs unchanged."""
        mock_self = _make_self()
        assert quirk.fix_local_calibration(mock_self, "climate.trv1", 1.5) == 1.5
        assert quirk.fix_valve_calibration(mock_self, "climate.trv1", 42) == 42
        assert (
            quirk.fix_target_temperature_calibration(mock_self, "climate.trv1", 21.0)
            == 21.0
        )

    @pytest.mark.asyncio
    async def test_set_temperature_declines(self):
        """The temperature override always declines so the adapter writes it."""
        mock_self = _make_self(calibration=CalibrationType.DIRECT_VALVE_BASED)
        assert (
            await quirk.override_set_temperature(mock_self, "climate.trv1", 21.0)
            is False
        )


class TestSetValve:
    """The valve is driven via the Multilevel Switch command class (0x26)."""

    @pytest.mark.asyncio
    async def test_declines_when_not_direct_valve(self):
        """Outside direct valve control the quirk does not touch the valve."""
        mock_self = _make_self(calibration=CalibrationType.TARGET_TEMP_BASED)

        handled = await quirk.override_set_valve(mock_self, "climate.trv1", 50)

        assert handled is False
        mock_self.hass.services.async_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_writes_multilevel_switch_scaled_to_99(self):
        """100 % maps onto the device's fully-open value of 99."""
        mock_self = _make_self(calibration=CalibrationType.DIRECT_VALVE_BASED)

        handled = await quirk.override_set_valve(mock_self, "climate.trv1", 100)

        assert handled is True
        mock_self.hass.services.async_call.assert_awaited_once()
        args, _ = mock_self.hass.services.async_call.call_args
        assert args[0] == "zwave_js"
        assert args[1] == "set_value"
        assert args[2]["entity_id"] == "climate.trv1"
        assert args[2]["command_class"] == 38
        assert args[2]["property"] == "targetValue"
        assert args[2]["value"] == 99

    @pytest.mark.asyncio
    async def test_closed_valve_writes_zero(self):
        """0 % maps onto a fully closed valve."""
        mock_self = _make_self(calibration=CalibrationType.DIRECT_VALVE_BASED)

        await quirk.override_set_valve(mock_self, "climate.trv1", 0)

        args, _ = mock_self.hass.services.async_call.call_args
        assert args[2]["value"] == 0

    @pytest.mark.asyncio
    async def test_declines_a_refused_valve_write(self):
        """A position that never reached the valve is not one to record."""
        mock_self = _make_self(calibration=CalibrationType.DIRECT_VALVE_BASED)
        mock_self.hass.services.async_call = AsyncMock(side_effect=HomeAssistantError)

        assert await quirk.override_set_valve(mock_self, "climate.trv1", 50) is False
