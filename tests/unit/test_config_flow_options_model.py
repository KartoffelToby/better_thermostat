"""Tests for model resolution in the options flow.

Swapping the configured thermostat of an existing entry to an entity without a
device-registry device (a ``generic_thermostat``, for example) sends the
options flow down the "new TRV" branch, where the model has to be resolved from
scratch. Model resolution falls back to the flow's own ``model`` attribute, so
the options flow must carry one and ``get_device_model`` must tolerate callers
that do not.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import CONF_NAME
from homeassistant.core import State
import pytest

from custom_components.better_thermostat.config_flow import (
    ConfigFlow,
    OptionsFlowHandler,
)
from custom_components.better_thermostat.utils.const import CONF_HEATER, CONF_SENSOR
from custom_components.better_thermostat.utils.helpers import get_device_model

GENERIC_TRV = "climate.generic_thermostat"
STORED_TRV = "climate.stored_trv"


class _CallerWithoutModel:
    """Duck-typed ``get_device_model`` caller that carries no model attribute."""

    def __init__(self, hass):
        self.hass = hass
        self.device_name = "Living Room"


class _CallerWithModel(_CallerWithoutModel):
    """Duck-typed ``get_device_model`` caller that carries a configured model."""

    def __init__(self, hass, model):
        super().__init__(hass)
        self.model = model


def _make_config_entry():
    entry = MagicMock()
    entry.data = {
        CONF_NAME: "Living Room",
        CONF_HEATER: [
            {"trv": STORED_TRV, "integration": "mqtt", "model": "TRVZB", "advanced": {}}
        ],
        CONF_SENSOR: "sensor.living_room_temperature",
    }
    return entry


def _make_hass():
    hass = MagicMock()
    hass.states.get.return_value = State(
        GENERIC_TRV, "heat", {"hvac_modes": ["heat", "off"]}
    )
    return hass


def _make_adapter():
    adapter = MagicMock()
    adapter.get_info = AsyncMock(
        return_value={"support_offset": False, "support_valve": False}
    )
    return adapter


def _patch_empty_registries():
    """Patch both registries so the entity resolves to no device."""
    entity_registry = MagicMock()
    entity_registry.async_get.return_value = None
    return (
        patch(
            "custom_components.better_thermostat.utils.helpers.er.async_get",
            return_value=entity_registry,
        ),
        patch(
            "custom_components.better_thermostat.utils.helpers.dr.async_get",
            return_value=MagicMock(),
        ),
    )


def _submission():
    return {
        CONF_NAME: "Living Room",
        CONF_HEATER: [GENERIC_TRV],
        CONF_SENSOR: "sensor.living_room_temperature",
    }


@pytest.mark.asyncio
async def test_options_flow_swap_to_generic_thermostat_resolves_generic_model():
    """Swapping to a device-less thermostat advances the flow with model 'generic'."""
    flow = OptionsFlowHandler(_make_config_entry())
    flow.hass = _make_hass()
    patch_er, patch_dr = _patch_empty_registries()

    with (
        patch_er,
        patch_dr,
        patch(
            "custom_components.better_thermostat.config_flow.load_adapter",
            autospec=True,
            return_value=_make_adapter(),
        ),
    ):
        result = await flow.async_step_user(_submission())

    assert result["type"] == "form"
    assert result["step_id"] == "advanced"
    assert [trv["trv"] for trv in flow.trv_bundle] == [GENERIC_TRV]
    assert flow.trv_bundle[0]["model"] == "generic"


@pytest.mark.asyncio
async def test_config_flow_swap_to_generic_thermostat_resolves_generic_model():
    """The create flow resolves the same model for a device-less thermostat."""
    flow = ConfigFlow()
    flow.hass = _make_hass()
    patch_er, patch_dr = _patch_empty_registries()

    with (
        patch_er,
        patch_dr,
        patch(
            "custom_components.better_thermostat.config_flow.load_adapter",
            autospec=True,
            return_value=_make_adapter(),
        ),
    ):
        result = await flow.async_step_user(_submission())

    assert result["type"] == "form"
    assert result["step_id"] == "advanced"
    assert flow.trv_bundle[0]["model"] == "generic"


def test_options_flow_handler_starts_without_a_model():
    """The options flow exposes the same initial model attribute as the config flow."""
    assert OptionsFlowHandler(_make_config_entry()).model is None
    assert ConfigFlow().model is None


@pytest.mark.asyncio
async def test_get_device_model_without_model_attribute_returns_generic():
    """A caller that carries no model attribute falls through to 'generic'."""
    caller = _CallerWithoutModel(MagicMock())
    patch_er, patch_dr = _patch_empty_registries()

    with patch_er, patch_dr:
        assert await get_device_model(caller, GENERIC_TRV) == "generic"


@pytest.mark.asyncio
async def test_get_device_model_prefers_configured_model_over_generic():
    """A caller with a configured model keeps it when the registry knows nothing."""
    caller = _CallerWithModel(MagicMock(), "TRVZB")
    patch_er, patch_dr = _patch_empty_registries()

    with patch_er, patch_dr:
        assert await get_device_model(caller, STORED_TRV) == "TRVZB"


@pytest.mark.asyncio
async def test_get_device_model_ignores_non_string_configured_model():
    """A configured model of the wrong type is not used as a fallback."""
    caller = _CallerWithModel(MagicMock(), 42)
    patch_er, patch_dr = _patch_empty_registries()

    with patch_er, patch_dr:
        assert await get_device_model(caller, STORED_TRV) == "generic"
