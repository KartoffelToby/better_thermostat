"""Tests for the Z-Wave JS adapter and the ZWA021 model quirks.

These tests assert two safety properties that hold without access to real
hardware:

1. The ZWA021 quirk only engages its manufacturer-specific valve mode when the
   TRV is configured for direct valve control; in every other case it declines
   and the standard climate path is used.
2. The adapter reports valve support only for a writable valve helper and
   degrades to generic offset/plain behaviour otherwise.
"""

import importlib
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import State
import pytest

from custom_components.better_thermostat.utils.const import CalibrationType

quirk = importlib.import_module(
    "custom_components.better_thermostat.model_fixes.ZWA021"
)
adapter = importlib.import_module(
    "custom_components.better_thermostat.adapters.zwave_js"
)


def _make_self(calibration=None):
    """Create a mock BetterThermostat with a spied service-call layer."""
    mock_self = MagicMock()
    mock_self.device_name = "test_thermostat"
    mock_self.context = MagicMock()
    mock_self.hass.services.async_call = AsyncMock()
    mock_self.real_trvs = {
        "climate.trv1": {
            "advanced": {"calibration": calibration},
            "local_temperature_calibration_entity": None,
            "valve_position_entity": None,
            "valve_position_writable": None,
            "calibration": 0,
            "last_hvac_mode": None,
            "last_calibration": None,
        }
    }
    return mock_self


class TestZWA021HvacOverride:
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


class TestZWA021Passthroughs:
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


class TestZWA021SetValve:
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


class TestAdapterGetInfo:
    """get_info reports capabilities strictly from the discovered entities."""

    @pytest.mark.asyncio
    async def test_no_entities_degrades_to_plain(self):
        """Without offset or valve entities the adapter looks like generic."""
        mock_self = _make_self()
        with (
            patch.object(
                adapter, "find_local_calibration_entity", AsyncMock(return_value=None)
            ),
            patch.object(adapter, "find_valve_entity", AsyncMock(return_value=None)),
        ):
            info = await adapter.get_info(mock_self, "climate.trv1")
        assert info == {"support_offset": False, "support_valve": False}

    @pytest.mark.asyncio
    async def test_readonly_valve_is_not_supported(self):
        """A read-only valve helper does not enable valve support."""
        mock_self = _make_self()
        with (
            patch.object(
                adapter, "find_local_calibration_entity", AsyncMock(return_value=None)
            ),
            patch.object(
                adapter,
                "find_valve_entity",
                AsyncMock(
                    return_value={"entity_id": "number.trv1_valve", "writable": False}
                ),
            ),
        ):
            info = await adapter.get_info(mock_self, "climate.trv1")
        assert info["support_valve"] is False

    @pytest.mark.asyncio
    async def test_quirk_model_reports_valve_without_number_entity(self):
        """A ZWA021 reports valve support although it exposes no number helper."""
        mock_self = _make_self()
        with (
            patch.object(
                adapter, "find_local_calibration_entity", AsyncMock(return_value=None)
            ),
            patch.object(adapter, "find_valve_entity", AsyncMock(return_value=None)),
            patch.object(adapter, "get_device_model", AsyncMock(return_value="ZWA021")),
        ):
            info = await adapter.get_info(mock_self, "climate.trv1")
        assert info["support_valve"] is True

    @pytest.mark.asyncio
    async def test_other_model_without_valve_stays_unsupported(self):
        """An unknown model without a writable helper reports no valve support."""
        mock_self = _make_self()
        with (
            patch.object(
                adapter, "find_local_calibration_entity", AsyncMock(return_value=None)
            ),
            patch.object(adapter, "find_valve_entity", AsyncMock(return_value=None)),
            patch.object(
                adapter, "get_device_model", AsyncMock(return_value="MT02650")
            ),
        ):
            info = await adapter.get_info(mock_self, "climate.trv1")
        assert info["support_valve"] is False

    @pytest.mark.asyncio
    async def test_writable_valve_and_offset_supported(self):
        """A writable valve helper plus a calibration entity enables both."""
        mock_self = _make_self()
        with (
            patch.object(
                adapter,
                "find_local_calibration_entity",
                AsyncMock(return_value="number.trv1_calibration"),
            ),
            patch.object(
                adapter,
                "find_valve_entity",
                AsyncMock(
                    return_value={"entity_id": "number.trv1_valve", "writable": True}
                ),
            ),
        ):
            info = await adapter.get_info(mock_self, "climate.trv1")
        assert info == {"support_offset": True, "support_valve": True}


class TestAdapterSetValve:
    """set_valve scales onto the number entity and honours read-only helpers."""

    @pytest.mark.asyncio
    async def test_skips_readonly_helper(self):
        """A read-only valve helper is skipped without any service call."""
        mock_self = _make_self()
        mock_self.real_trvs["climate.trv1"]["valve_position_writable"] = False
        await adapter.set_valve(mock_self, "climate.trv1", 50)
        mock_self.hass.services.async_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_scales_percentage_onto_entity_range(self):
        """50 % maps onto the midpoint of a 0..99 valve, snapped to step."""
        mock_self = _make_self()
        mock_self.real_trvs["climate.trv1"]["valve_position_writable"] = True
        mock_self.real_trvs["climate.trv1"]["valve_position_entity"] = (
            "number.trv1_valve"
        )
        state = State("number.trv1_valve", "50", {"min": 0, "max": 99, "step": 1})
        mock_self.hass.states.get = MagicMock(return_value=state)

        await adapter.set_valve(mock_self, "climate.trv1", 50)

        mock_self.hass.services.async_call.assert_awaited_once()
        args, _ = mock_self.hass.services.async_call.call_args
        assert args[0] == "number"
        assert args[2]["entity_id"] == "number.trv1_valve"
        # 0 + 0.5 * 99 = 49.5 -> snapped to step 1 -> 50 (round-half-to-even)
        assert args[2]["value"] == pytest.approx(50)

    @pytest.mark.asyncio
    async def test_out_of_range_input_is_clamped(self):
        """An over-100 % request never exceeds the entity's max value."""
        mock_self = _make_self()
        mock_self.real_trvs["climate.trv1"]["valve_position_writable"] = True
        mock_self.real_trvs["climate.trv1"]["valve_position_entity"] = (
            "number.trv1_valve"
        )
        state = State("number.trv1_valve", "50", {"min": 0, "max": 99, "step": 1})
        mock_self.hass.states.get = MagicMock(return_value=state)

        await adapter.set_valve(mock_self, "climate.trv1", 150)

        args, _ = mock_self.hass.services.async_call.call_args
        assert args[2]["value"] == pytest.approx(99)
