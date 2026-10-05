"""Tests for the Shelly Better Thermostat adapter."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.better_thermostat.adapters import shelly


ENTITY_ID = "climate.test_shelly_trv"
VALVE_ENTITY_ID = "number.test_shelly_trv_valve"


def _bt():
    bt = SimpleNamespace()
    bt.device_name = "Test BT"
    bt.context = object()
    bt.real_trvs = {
        ENTITY_ID: {
            "valve_position_entity": VALVE_ENTITY_ID,
            "valve_position_writable": True,
        }
    }
    bt.hass = SimpleNamespace()
    bt.hass.states = MagicMock()
    bt.hass.services = SimpleNamespace(async_call=AsyncMock())
    return bt


@pytest.mark.asyncio
async def test_get_info_reports_writable_shelly_valve(monkeypatch):
    """A writable Shelly Number enables Direct Valve support."""
    bt = _bt()
    monkeypatch.setattr(
        shelly,
        "generic_get_info",
        AsyncMock(return_value={"support_offset": False, "support_valve": False}),
    )
    monkeypatch.setattr(
        shelly,
        "find_valve_entity",
        AsyncMock(
            return_value={
                "entity_id": VALVE_ENTITY_ID,
                "writable": True,
                "domain": "number",
                "reason": "valve_position",
            }
        ),
    )

    info = await shelly.get_info(bt, ENTITY_ID)

    assert info == {"support_offset": False, "support_valve": True}


@pytest.mark.asyncio
async def test_init_stores_discovered_valve(monkeypatch):
    """Initialization stores the shared helper's discovery result."""
    bt = _bt()
    bt.real_trvs[ENTITY_ID]["valve_position_entity"] = None
    bt.real_trvs[ENTITY_ID]["valve_position_writable"] = False

    monkeypatch.setattr(
        shelly,
        "find_valve_entity",
        AsyncMock(
            return_value={
                "entity_id": VALVE_ENTITY_ID,
                "writable": True,
                "domain": "number",
                "reason": "valve_position",
            }
        ),
    )
    generic_init = AsyncMock()
    monkeypatch.setattr(shelly, "generic_init", generic_init)

    await shelly.init(bt, ENTITY_ID)

    assert bt.real_trvs[ENTITY_ID]["valve_position_entity"] == VALVE_ENTITY_ID
    assert bt.real_trvs[ENTITY_ID]["valve_position_writable"] is True
    generic_init.assert_awaited_once_with(bt, ENTITY_ID)


@pytest.mark.asyncio
async def test_set_valve_scales_percentage_to_number_range():
    """BT's percentage target is mapped to the Number min/max/step."""
    bt = _bt()
    bt.hass.states.get.return_value = SimpleNamespace(
        attributes={"min": 10, "max": 90, "step": 5}
    )

    await shelly.set_valve(bt, ENTITY_ID, 50)

    bt.hass.services.async_call.assert_awaited_once_with(
        "number",
        "set_value",
        {"entity_id": VALVE_ENTITY_ID, "value": 50},
        blocking=True,
        context=bt.context,
    )


@pytest.mark.asyncio
async def test_set_valve_clamps_percentage():
    """Out-of-range BT targets cannot escape the Number's valid range."""
    bt = _bt()
    bt.hass.states.get.return_value = SimpleNamespace(
        attributes={"min": 0, "max": 100, "step": 1}
    )

    await shelly.set_valve(bt, ENTITY_ID, 150)

    assert bt.hass.services.async_call.await_args.args[2]["value"] == 100


@pytest.mark.asyncio
async def test_set_temperature_is_skipped_in_direct_valve_mode(monkeypatch):
    """No climate target is written while Shelly exposes valve-only control."""
    bt = _bt()
    bt.hass.states.get.return_value = SimpleNamespace(attributes={"temperature": None})
    generic_set_temperature = AsyncMock()
    monkeypatch.setattr(shelly, "generic_set_temperature", generic_set_temperature)

    await shelly.set_temperature(bt, ENTITY_ID, 21.5)

    generic_set_temperature.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_temperature_uses_generic_when_target_exists(monkeypatch):
    """Normal Shelly thermostat mode keeps Better Thermostat's generic path."""
    bt = _bt()
    bt.hass.states.get.return_value = SimpleNamespace(attributes={"temperature": 20.0})
    generic_set_temperature = AsyncMock(return_value=None)
    monkeypatch.setattr(shelly, "generic_set_temperature", generic_set_temperature)

    await shelly.set_temperature(bt, ENTITY_ID, 21.5)

    generic_set_temperature.assert_awaited_once_with(bt, ENTITY_ID, 21.5)
