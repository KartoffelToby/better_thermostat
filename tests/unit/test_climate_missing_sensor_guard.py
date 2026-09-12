"""Guards for a missing room temperature sensor configuration.

The external room sensor is a required configuration field. When it is
absent, startup() and _finalize_startup() abort gracefully instead of
crashing, and both log at ERROR so the misconfiguration is visible: the
entity stays unavailable and no listeners are registered, which would
otherwise look like a silent hang.
"""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.decide import KernelState
from custom_components.better_thermostat.trv import Trv

_CLIMATE = "custom_components.better_thermostat.climate"
TRV_ID = "climate.test_trv"


def _bt_without_sensor():
    """Build a minimal BetterThermostat mock with no room sensor configured."""
    mock = MagicMock()
    mock.device_name = "Test BT"
    mock.is_removed = False
    mock.version = "1.0.0"
    mock.kernel_state = KernelState()
    mock.clock = FakeClock()
    mock.real_trvs = {TRV_ID: Trv(entity_id=TRV_ID, advanced={})}
    mock.entity_ids = [TRV_ID]
    mock.all_trvs = None
    mock.all_entities = []
    mock.sensor_entity_id = None
    mock.humidity_sensor_entity_id = None
    mock.window_id = None
    mock.door_id = None
    mock.cooler_entity_id = None
    mock.outdoor_sensor = None
    mock.weather_entity = None
    mock.unavailable_sensors = []
    mock._degraded_warning_emitted = False
    mock._degraded_grace_until = None
    mock._async_unsub_state_changed = None
    mock._trigger_time = AsyncMock()
    mock._trigger_check_weather = AsyncMock()
    mock._startup_control_trvs = AsyncMock()
    mock.hass = MagicMock()
    return mock


@pytest.mark.asyncio
async def test_startup_logs_error_and_aborts_without_room_sensor(caplog):
    """startup() names the missing required sensor at ERROR and returns."""
    bt = _bt_without_sensor()

    with caplog.at_level(logging.ERROR):
        await asyncio.wait_for(BetterThermostat.startup(bt), timeout=1)

    errors = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR
        and "no room temperature sensor configured" in r.message
    ]
    assert errors, "missing-sensor abort must be logged at ERROR"
    bt._check_entities_ready.assert_not_called()
    bt._collect_trv_states.assert_not_called()
    bt._finalize_startup.assert_not_called()


@pytest.mark.asyncio
async def test_finalize_startup_logs_error_and_skips_listeners(caplog):
    """_finalize_startup names the missing required sensor at ERROR."""
    bt = _bt_without_sensor()
    track_state = MagicMock()

    with (
        caplog.at_level(logging.ERROR),
        patch(f"{_CLIMATE}.await_critical_entities", AsyncMock()),
        patch(f"{_CLIMATE}.check_critical_entities", AsyncMock(return_value=True)),
        patch(f"{_CLIMATE}.await_optional_sensors", AsyncMock()),
        patch(f"{_CLIMATE}.check_and_update_degraded_mode", AsyncMock()),
        patch(f"{_CLIMATE}.async_track_time_interval", MagicMock()),
        patch(f"{_CLIMATE}.async_track_state_change_event", track_state),
        patch(f"{_CLIMATE}.async_track_time_change", MagicMock()),
        patch(f"{_CLIMATE}.asyncio.sleep", AsyncMock()),
    ):
        await BetterThermostat._finalize_startup(bt)

    errors = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR
        and "no room temperature sensor configured" in r.message
    ]
    assert errors, "missing-sensor listener skip must be logged at ERROR"
    track_state.assert_not_called()
