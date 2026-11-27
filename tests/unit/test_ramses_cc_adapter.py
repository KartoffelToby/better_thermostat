"""Tests for the RAMSES RF Better Thermostat adapter."""

from __future__ import annotations

from collections.abc import Iterator
import math
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import Context, State
from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.better_thermostat.adapters import ramses_cc
from custom_components.better_thermostat.adapters.types import AdapterHost, TrvAdapter
from custom_components.better_thermostat.utils.retry import WriteWithheldError
from tests.factories import make_entity_registry, make_registry_entry

ENTITY_ID = "climate.controller_42_012345_foo"
ZONE_ATTRS = {"zone_index": 0, "config": {"min_temp": 5, "max_temp": 35}}


@pytest.fixture
def adapter_env() -> Iterator[tuple[AdapterHost, MagicMock, MagicMock]]:
    hass = MagicMock()
    hass.states.get.return_value = State(ENTITY_ID, "heat", ZONE_ATTRS)
    hass.services.has_service.return_value = True
    hass.services.async_call = AsyncMock()

    registry = make_entity_registry(
        make_registry_entry(ENTITY_ID, platform="ramses_cc")
    )
    host = cast(AdapterHost, SimpleNamespace(hass=hass, context=Context()))

    with patch.object(ramses_cc.er, "async_get", return_value=registry):
        yield host, hass, registry


async def test_adapter_contract_and_capabilities(adapter_env):
    host, _, _ = adapter_env
    assert isinstance(ramses_cc, TrvAdapter)
    info = await ramses_cc.get_info(host, ENTITY_ID)
    assert not info.offset_write and not info.valve_write
    assert not ramses_cc.CAPABILITIES.offset_write
    assert not ramses_cc.CAPABILITIES.valve_write


async def test_heat_mode_does_not_send_25_degrees(adapter_env):
    host, hass, _ = adapter_env
    await ramses_cc.set_hvac_mode(host, ENTITY_ID, "heat")
    hass.services.async_call.assert_not_awaited()


async def test_off_sends_permanent_override_at_zone_minimum(adapter_env):
    host, hass, _ = adapter_env
    await ramses_cc.set_hvac_mode(host, ENTITY_ID, "off")
    hass.services.async_call.assert_awaited_once_with(
        "ramses_cc",
        "set_zone_mode",
        {"entity_id": ENTITY_ID, "mode": "permanent_override", "setpoint": 5.0},
        blocking=True,
        context=host.context,
    )


@pytest.mark.parametrize("temperature", [5.0, 13.5, 31.5, 35.0])
async def test_temperature_sends_permanent_override(adapter_env, temperature):
    host, hass, _ = adapter_env
    await ramses_cc.set_temperature(host, ENTITY_ID, temperature)
    hass.services.async_call.assert_awaited_once_with(
        "ramses_cc",
        "set_zone_mode",
        {"entity_id": ENTITY_ID, "mode": "permanent_override", "setpoint": temperature},
        blocking=True,
        context=host.context,
    )


@pytest.mark.parametrize("mode", ["auto", "cool", "unknown"])
async def test_unsupported_mode_rejected_without_write(adapter_env, mode):
    host, hass, _ = adapter_env
    with pytest.raises(HomeAssistantError) as exc:
        await ramses_cc.set_hvac_mode(host, ENTITY_ID, mode)
    assert exc.value.translation_key == "ramses_unsupported_hvac_mode"
    hass.services.async_call.assert_not_awaited()


@pytest.mark.parametrize("state", ["unavailable", "unknown"])
async def test_unavailable_zone_defers_writes(adapter_env, state):
    host, hass, _ = adapter_env
    hass.states.get.return_value = State(ENTITY_ID, state, ZONE_ATTRS)
    with pytest.raises(WriteWithheldError):
        await ramses_cc.set_temperature(host, ENTITY_ID, 18.0)
    hass.services.async_call.assert_not_awaited()


@pytest.mark.parametrize(
    "attrs", [{}, {"config": {"min_temp": 5, "max_temp": 35}}, {"zone_index": 0}]
)
async def test_invalid_zone_metadata_rejected(adapter_env, attrs):
    host, hass, _ = adapter_env
    hass.states.get.return_value = State(ENTITY_ID, "heat", attrs)
    with pytest.raises(HomeAssistantError):
        await ramses_cc.set_temperature(host, ENTITY_ID, 18.0)
    hass.services.async_call.assert_not_awaited()


@pytest.mark.parametrize("bounds", [(10, 5), (5, math.inf), ("bad", 35)])
async def test_invalid_zone_bounds_rejected(adapter_env, bounds):
    host, hass, _ = adapter_env
    low, high = bounds
    hass.states.get.return_value = State(
        ENTITY_ID,
        "heat",
        {"zone_index": 0, "config": {"min_temp": low, "max_temp": high}},
    )
    with pytest.raises(HomeAssistantError) as exc:
        await ramses_cc.set_temperature(host, ENTITY_ID, 18.0)
    assert exc.value.translation_key == "ramses_invalid_zone_bounds"
    hass.services.async_call.assert_not_awaited()


@pytest.mark.parametrize("temperature", [4.5, 35.5, math.nan, math.inf])
async def test_invalid_setpoints_rejected(adapter_env, temperature):
    host, hass, _ = adapter_env
    with pytest.raises(HomeAssistantError) as exc:
        await ramses_cc.set_temperature(host, ENTITY_ID, temperature)
    assert exc.value.translation_key == "ramses_invalid_setpoint"
    hass.services.async_call.assert_not_awaited()


async def test_wrong_integration_rejected(adapter_env):
    host, hass, registry = adapter_env
    registry.entities[ENTITY_ID] = make_registry_entry(ENTITY_ID, platform="other")
    with pytest.raises(HomeAssistantError) as exc:
        await ramses_cc.set_temperature(host, ENTITY_ID, 18.0)
    assert exc.value.translation_key == "ramses_invalid_zone"
    hass.services.async_call.assert_not_awaited()


async def test_missing_ramses_service_rejected(adapter_env):
    host, hass, _ = adapter_env
    hass.services.has_service.return_value = False
    with pytest.raises(HomeAssistantError) as exc:
        await ramses_cc.set_temperature(host, ENTITY_ID, 18.0)
    assert exc.value.translation_key == "ramses_service_unavailable"
    hass.services.async_call.assert_not_awaited()


async def test_service_failure_propagates(adapter_env):
    host, hass, _ = adapter_env
    hass.services.async_call.side_effect = HomeAssistantError("send failed")
    with pytest.raises(HomeAssistantError, match="send failed"):
        await ramses_cc.set_temperature(host, ENTITY_ID, 18.0)


async def test_unsupported_write_channels_do_nothing(adapter_env):
    host, hass, _ = adapter_env
    assert await ramses_cc.set_valve(host, ENTITY_ID, 50.0) is None
    assert await ramses_cc.set_calibration_offset(host, ENTITY_ID, 1.0) is False
    hass.services.async_call.assert_not_awaited()
