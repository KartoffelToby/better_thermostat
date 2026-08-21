"""Tests for the window state-change dispatcher in climate.py.

``_trigger_window_change`` forwards window sensor events to the window
event handler. The handler in events/window.py owns the state
interpretation, including unknown/unavailable readings (a lost sensor
counts as closed so heating resumes) and garbage readings (repair
issue). The dispatcher therefore must not filter events by sensor
availability — otherwise that handling is unreachable and a sensor that
dies while the window is open strands the thermostat with heating off.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.better_thermostat.climate import BetterThermostat

CLIMATE_MOD = "custom_components.better_thermostat.climate"


def _make_self():
    """Build a BetterThermostat stand-in for the window dispatcher."""
    ns = SimpleNamespace(
        device_name="Test BT",
        window_id="binary_sensor.window",
        hass=MagicMock(),
        async_set_context=MagicMock(),
    )
    # ``_trigger_window_change`` delegates to the shared contact dispatcher;
    # bind it so the stand-in resolves the method call.
    ns._trigger_contact_change = BetterThermostat._trigger_contact_change.__get__(ns)
    return ns


def _event(state_value):
    new_state = MagicMock()
    new_state.state = state_value
    event = MagicMock()
    event.data = {"new_state": new_state}
    return event


def _patch_checks():
    """Patch the watcher helpers and the window handler coroutine."""
    return patch.multiple(
        CLIMATE_MOD,
        check_critical_entities=AsyncMock(return_value=True),
        check_and_update_degraded_mode=AsyncMock(),
        trigger_window_change=MagicMock(return_value=MagicMock()),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("reading", ["on", "off", "unknown", "unavailable"])
async def test_window_event_is_dispatched_regardless_of_availability(reading):
    """Every sensor reading reaches the handler, including a lost sensor."""
    bt = _make_self()
    bt.hass.states.get.return_value = MagicMock(state=reading)

    with _patch_checks():
        await BetterThermostat._trigger_window_change(bt, _event(reading))

    bt.hass.async_create_background_task.assert_called_once()


@pytest.mark.asyncio
async def test_event_without_new_state_is_dropped():
    """An event carrying no new state dispatches nothing."""
    bt = _make_self()
    event = MagicMock()
    event.data = {"new_state": None}

    with _patch_checks():
        await BetterThermostat._trigger_window_change(bt, event)

    bt.hass.async_create_background_task.assert_not_called()
