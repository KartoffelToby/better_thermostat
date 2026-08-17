"""Tests for the config flow's reads of a device's offered HVAC modes.

A climate entity publishes its capability list in its own spelling, so both
the AUTO probe and the OFF gate must compare normalized. A device naming its
modes ``HVACMode.OFF`` offers OFF just as one naming them ``off`` does.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.climate.const import HVACMode
import pytest

from custom_components.better_thermostat.config_flow import (
    ConfigFlow,
    _trv_supports_auto,
)


def _flow_with_modes(modes):
    """Build a flow whose single TRV reports the given mode list."""
    state = MagicMock()
    state.attributes = {"hvac_modes": modes}

    flow = MagicMock()
    flow.hass.states.get.return_value = state
    return flow


class TestTrvSupportsAuto:
    """The AUTO probe reads the device's own spelling."""

    def test_auto_offered_as_plain_string(self):
        """A plain ``auto`` is recognized."""
        assert _trv_supports_auto(_flow_with_modes(["off", "auto"]), "climate.trv")

    def test_auto_offered_in_the_device_spelling(self):
        """An ``HVACMode.AUTO`` spelling is recognized just the same."""
        assert _trv_supports_auto(
            _flow_with_modes(["HVACMode.OFF", "HVACMode.AUTO"]), "climate.trv"
        )

    def test_auto_absent_in_the_device_spelling(self):
        """A device genuinely without AUTO is still reported as lacking it."""
        assert not _trv_supports_auto(
            _flow_with_modes(["HVACMode.OFF", "HVACMode.HEAT"]), "climate.trv"
        )


def _advanced_flow(modes):
    """Build a ConfigFlow parked on the last TRV of a one-TRV bundle."""
    flow = ConfigFlow()
    flow.hass = MagicMock()
    state = MagicMock()
    state.attributes = {"hvac_modes": modes}
    flow.hass.states.get.return_value = state
    flow.i = 0
    flow.trv_bundle = [{"trv": "climate.trv", "advanced": {}}]
    flow._active_trv_config = flow.trv_bundle[0]
    return flow


async def _run_advanced(flow):
    """Submit the advanced step and return the confirm_type it hands on."""
    confirm = AsyncMock(return_value="confirm")
    with (
        patch(
            "custom_components.better_thermostat.config_flow._prepare_advanced_context",
            new=AsyncMock(
                return_value={
                    "trv_id": "climate.trv",
                    "default_calibration": "target_temp_based",
                    "homematic": False,
                    "has_auto": False,
                    "info": {},
                }
            ),
        ),
        patch(
            "custom_components.better_thermostat.config_flow._normalize_advanced_submission",
            return_value={},
        ),
        patch.object(ConfigFlow, "async_step_confirm", new=confirm),
    ):
        await flow.async_step_advanced({})
    return confirm.await_args[0][1] if len(confirm.await_args[0]) > 1 else None


class TestOffModeGate:
    """The OFF gate reads the device's own spelling."""

    @pytest.mark.asyncio
    async def test_off_offered_as_plain_string_passes_the_gate(self):
        """A plain ``off`` reaches confirm without the no_off_mode notice."""
        assert await _run_advanced(_advanced_flow(["off", "heat"])) is None

    @pytest.mark.asyncio
    async def test_off_offered_in_the_device_spelling_passes_the_gate(self):
        """An ``HVACMode.OFF`` spelling passes the gate just the same."""
        assert (
            await _run_advanced(_advanced_flow(["HVACMode.OFF", "HVACMode.HEAT"]))
            is None
        )

    @pytest.mark.asyncio
    async def test_device_without_off_still_raises_the_notice(self):
        """A device genuinely without OFF still gets the no_off_mode notice."""
        assert await _run_advanced(_advanced_flow(["HVACMode.HEAT"])) == "no_off_mode"


def test_hvac_mode_enum_members_are_recognized():
    """A list holding HVACMode members keeps working."""
    assert _trv_supports_auto(
        _flow_with_modes([HVACMode.OFF, HVACMode.AUTO]), "climate.trv"
    )
