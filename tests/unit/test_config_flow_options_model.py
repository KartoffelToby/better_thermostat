"""Tests for model resolution in the options flow.

Swapping the configured thermostat of an existing entry to an entity without a
device-registry device (a ``generic_thermostat``, for example) sends the
options flow down the "new TRV" branch, where the model has to be resolved from
scratch. Neither flow has a configured model to hand to ``get_device_model``,
so that fallback is optional.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.climate import ClimateEntityFeature
from homeassistant.const import CONF_NAME
from homeassistant.core import State
import pytest

from custom_components.better_thermostat.config_flow import (
    ConfigFlow,
    OptionsFlowHandler,
)
from custom_components.better_thermostat.utils.const import (
    CONF_TEMPERATURE_SENSOR,
    CONF_THERMOSTAT,
)
from custom_components.better_thermostat.utils.helpers import get_device_model
from tests.factories import make_entity_registry

GENERIC_TRV = "climate.generic_thermostat"
STORED_TRV = "climate.stored_trv"


class _Caller:
    """Duck-typed ``get_device_model`` caller."""

    def __init__(self, hass):
        self.hass = hass
        self.device_name = "Living Room"


def _make_config_entry():
    entry = MagicMock()
    entry.data = {
        CONF_NAME: "Living Room",
        CONF_THERMOSTAT: [
            {"trv": STORED_TRV, "integration": "mqtt", "model": "TRVZB", "advanced": {}}
        ],
        CONF_TEMPERATURE_SENSOR: "sensor.living_room_temperature",
    }
    return entry


def _make_hass():
    hass = MagicMock()
    hass.states.get.return_value = State(
        GENERIC_TRV,
        "heat",
        {
            "hvac_modes": ["heat", "off"],
            "supported_features": ClimateEntityFeature.TARGET_TEMPERATURE,
        },
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
    entity_registry = make_entity_registry()
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
        CONF_THERMOSTAT: [GENERIC_TRV],
        CONF_TEMPERATURE_SENSOR: "sensor.living_room_temperature",
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
    assert [trv.entity_id for trv in flow.trv_bundle] == [GENERIC_TRV]
    assert flow.trv_bundle[0].stored["model"] == "generic"


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
    assert flow.trv_bundle[0].stored["model"] == "generic"


@pytest.mark.asyncio
async def test_get_device_model_without_model_attribute_returns_generic():
    """A caller without a configured model falls through to 'generic'."""
    caller = _Caller(MagicMock())
    patch_er, patch_dr = _patch_empty_registries()

    with patch_er, patch_dr:
        assert await get_device_model(caller, GENERIC_TRV) == "generic"


@pytest.mark.asyncio
async def test_get_device_model_prefers_configured_model_over_generic():
    """A caller with a configured model keeps it when the registry knows nothing."""
    caller = _Caller(MagicMock())
    patch_er, patch_dr = _patch_empty_registries()

    with patch_er, patch_dr:
        assert (
            await get_device_model(caller, STORED_TRV, configured_model="TRVZB")
            == "TRVZB"
        )


@pytest.mark.asyncio
async def test_get_device_model_ignores_non_string_configured_model():
    """A configured model of the wrong type is not used as a fallback."""
    caller = _Caller(MagicMock())
    patch_er, patch_dr = _patch_empty_registries()

    with patch_er, patch_dr:
        assert (
            await get_device_model(caller, STORED_TRV, configured_model=42) == "generic"
        )
