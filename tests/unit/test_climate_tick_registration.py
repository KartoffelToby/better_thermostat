"""Periodic tick registration in BetterThermostat._finalize_startup.

_finalize_startup registers two independent 5-minute intervals: the control
tick (_trigger_time, gated on an active balance/calibration mode) and the
valve maintenance tick (_maintenance_tick, gated only on a TRV having valve
maintenance enabled). These tests pin down that the maintenance tick is
registered whenever a TRV enables it, regardless of the calibration mode.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.const import (
    CONF_VALVE_MAINTENANCE,
    CalibrationMode,
)

_CLIMATE = "custom_components.better_thermostat.climate"
SENSOR_ID = "sensor.room_temp"
TRV_ID = "climate.test_trv"


def _make_bt(advanced):
    """Build a minimal BetterThermostat mock for _finalize_startup."""
    mock = MagicMock(spec=BetterThermostat)
    mock.hass = MagicMock()
    mock.device_name = "Test BT"
    mock.is_removed = False
    mock.real_trvs = {TRV_ID: Trv(entity_id=TRV_ID, advanced=advanced)}
    mock.all_trvs = None
    mock.all_entities = []
    mock.entity_ids = [TRV_ID]
    mock.sensor_entity_id = SENSOR_ID
    mock.humidity_sensor_entity_id = None
    mock.window_id = None
    mock.door_id = None
    mock.cooler_entity_id = None
    mock.outdoor_sensor = None
    mock._async_unsub_state_changed = None
    # Plain MagicMocks so the un-awaited coroutines handed to the background
    # task mock do not raise "coroutine was never awaited" warnings.
    mock._post_grace_recheck = MagicMock()
    mock._external_temperature_keepalive = MagicMock()
    return mock


async def _run_finalize_startup(bt):
    """Run _finalize_startup and return the async_track_time_interval mock."""
    with (
        patch(f"{_CLIMATE}.await_critical_entities", AsyncMock()),
        patch(f"{_CLIMATE}.check_critical_entities", AsyncMock(return_value=True)),
        patch(f"{_CLIMATE}.await_optional_sensors", AsyncMock()),
        patch(f"{_CLIMATE}.check_and_update_degraded_mode", AsyncMock()),
        patch(f"{_CLIMATE}.asyncio.sleep", AsyncMock()),
        patch(f"{_CLIMATE}.async_track_time_interval") as track_interval,
        patch(f"{_CLIMATE}.async_track_state_change_event"),
        patch(f"{_CLIMATE}.async_track_time_change"),
    ):
        await BetterThermostat._finalize_startup(bt)
    return track_interval


def _registered_callbacks(track_interval, bt):
    """Extract the callbacks registered via async_track_time_interval."""
    return [call.args[1] for call in track_interval.call_args_list]


@pytest.mark.asyncio
async def test_maintenance_tick_registered_with_calibration_mode():
    """A calibrating instance with valve maintenance enabled gets the tick."""
    bt = _make_bt(
        {
            "calibration_mode": CalibrationMode.PID_CALIBRATION.value,
            CONF_VALVE_MAINTENANCE: True,
        }
    )
    track_interval = await _run_finalize_startup(bt)
    callbacks = _registered_callbacks(track_interval, bt)
    assert bt._trigger_time in callbacks
    assert bt._maintenance_tick in callbacks


@pytest.mark.asyncio
async def test_maintenance_tick_registered_without_calibration_mode():
    """Without balance/calibration, valve maintenance is still registered."""
    bt = _make_bt(
        {
            "calibration_mode": CalibrationMode.NO_CALIBRATION.value,
            CONF_VALVE_MAINTENANCE: True,
        }
    )
    track_interval = await _run_finalize_startup(bt)
    callbacks = _registered_callbacks(track_interval, bt)
    assert bt._trigger_time not in callbacks
    assert bt._maintenance_tick in callbacks


@pytest.mark.asyncio
async def test_maintenance_tick_skipped_when_no_trv_enables_it():
    """Without any TRV enabling valve maintenance, no tick is registered."""
    bt = _make_bt(
        {
            "calibration_mode": CalibrationMode.NO_CALIBRATION.value,
            CONF_VALVE_MAINTENANCE: False,
        }
    )
    track_interval = await _run_finalize_startup(bt)
    callbacks = _registered_callbacks(track_interval, bt)
    assert bt._maintenance_tick not in callbacks


def _make_bt_for_binding(trv_confs):
    """Build a _finalize_startup mock with a configured all_trvs list."""
    bt = _make_bt({"calibration_mode": CalibrationMode.NO_CALIBRATION.value})
    bt.all_trvs = trv_confs
    bt._unique_id = "bt_uid"
    bt._config_entry_id = "entry_1"
    return bt


@pytest.mark.asyncio
async def test_via_device_binding_runs_for_single_trv():
    """A single-TRV setup binds the BT device via that one valve."""
    bt = _make_bt_for_binding([{"trv": TRV_ID}])
    with patch(f"{_CLIMATE}.async_bind_trv_device", AsyncMock()) as bind:
        await _run_finalize_startup(bt)

    bind.assert_awaited_once_with(bt.hass, "bt_uid", TRV_ID, "entry_1")


@pytest.mark.asyncio
async def test_via_device_binding_skipped_for_multi_trv():
    """A multi-TRV setup skips via_device binding.

    via_device is single-valued, so binding each TRV would just rewrite the
    same BT device row and leave it attached to the last valve only.
    """
    bt = _make_bt_for_binding([{"trv": TRV_ID}, {"trv": "climate.second_trv"}])
    with patch(f"{_CLIMATE}.async_bind_trv_device", AsyncMock()) as bind:
        await _run_finalize_startup(bt)

    bind.assert_not_awaited()
