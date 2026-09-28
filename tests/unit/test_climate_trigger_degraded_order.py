"""Degraded-mode annunciation in the recurring trigger handlers.

Every handler updates the degraded-mode annunciation via
check_and_update_degraded_mode, and an unavailable TRV does not end the
handler. The combined failure case is what this pins: a room sensor lost
while a TRV is offline is exactly when the user needs to be told.

The same holds for the way back. A degraded thermostat whose sensors all
return still has to clear its repair issue while a TRV is offline.
"""

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.trv import Trv

_CLIMATE = "custom_components.better_thermostat.climate"
_WATCHER = "custom_components.better_thermostat.utils.watcher"
_HELPERS = "custom_components.better_thermostat.utils.helpers"
SENSOR_ID = "sensor.room_temp"
TRV_ID = "climate.test_trv"

# Every handler that runs the critical-entity check, with the arguments it
# takes beyond the entity itself; an unavailable TRV no longer stops any of
# them. ``_trigger_window_change`` and ``_trigger_door_change`` delegate to
# ``_trigger_contact_change``, which runs the check and therefore stands in
# for both. The room-sensor listener hands each reading to
# ``_handle_temperature_reading``, which runs the check for it.
HANDLERS = [
    ("_trigger_time", (None,)),
    ("_trigger_check_weather", (None,)),
    ("_handle_temperature_reading", (MagicMock(),)),
    ("_trigger_humidity_change", (MagicMock(),)),
    ("_trigger_trv_change", (MagicMock(),)),
    ("_trigger_cooler_change", (MagicMock(),)),
    ("_trigger_outdoor_change", (MagicMock(),)),
    ("_trigger_contact_change", (MagicMock(), None, MagicMock(), "window")),
    ("_maintenance_tick", (None,)),
]


@pytest.fixture
def bt():
    """Mock BT whose room sensor reads as unavailable."""
    mock = MagicMock()
    mock.device_name = "Test BT"
    mock.real_trvs = {TRV_ID: Trv(entity_id=TRV_ID)}
    mock.sensor_entity_id = SENSOR_ID
    mock.humidity_sensor_entity_id = None
    mock.window_id = None
    mock.door_id = None
    mock.outdoor_sensor = None
    mock.weather_entity = None
    mock.cooler_entity_id = None
    mock.unavailable_sensors = []
    mock.degraded_mode = False
    mock._degraded_warning_emitted = False
    mock._degraded_grace_until = None
    mock.in_maintenance = False
    mock.call_for_heat = True
    mock._last_call_for_heat = True
    mock.async_update_ha_state = AsyncMock()
    mock.control_queue_task = MagicMock(put=AsyncMock())
    # The handlers hand their work on; this module checks only the annunciation.
    mock._spawn_owned = lambda coro, name=None: coro.close()
    mock._temperature_filter_lock = None
    mock.hass = MagicMock()
    mock.hass.states.get.return_value = None
    return mock


def _guards(*, trv_reachable):
    """Patch the handler's surroundings, leaving the annunciation real."""
    return (
        patch(
            f"{_CLIMATE}.check_critical_entities", AsyncMock(return_value=trv_reachable)
        ),
        patch(f"{_CLIMATE}.check_ambient_air_temperature", AsyncMock()),
        patch(f"{_CLIMATE}.check_weather", AsyncMock()),
        patch(f"{_WATCHER}.ir.async_create_issue"),
        patch(f"{_WATCHER}.ir.async_delete_issue"),
        patch(f"{_HELPERS}.async_fire_logbook_entry", AsyncMock()),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("handler", "args"), HANDLERS, ids=[h for h, _ in HANDLERS])
async def test_a_lost_room_sensor_is_reported_while_a_trv_is_offline(bt, handler, args):
    """The annunciation runs while the critical check finds a TRV offline.

    An unreachable valve is not a reason to stop telling the user that the
    room sensor is gone; it is the case where both have failed at once.
    """
    with contextlib.ExitStack() as stack:
        for guard in _guards(trv_reachable=False):
            stack.enter_context(guard)
        await getattr(BetterThermostat, handler)(bt, *args)

    assert bt.degraded_mode is True
    assert bt.unavailable_sensors == [SENSOR_ID]


@pytest.mark.asyncio
async def test_an_offline_trv_does_not_stop_the_rest_of_the_handler(bt):
    """The tick runs on while a TRV is offline.

    The control cycle leaves the unreachable valve out and drives the rest of
    the room, so the outdoor refresh and the cycle request still happen.
    """
    ambient = AsyncMock()
    with (
        patch(f"{_CLIMATE}.check_critical_entities", AsyncMock(return_value=False)),
        patch(f"{_CLIMATE}.check_ambient_air_temperature", ambient),
        patch(f"{_WATCHER}.ir.async_create_issue"),
        patch(f"{_HELPERS}.async_fire_logbook_entry", AsyncMock()),
    ):
        await BetterThermostat._trigger_time(bt, MagicMock())

    ambient.assert_awaited_once_with(bt)
    bt.control_queue_task.put.assert_awaited_once_with(bt)


@pytest.mark.asyncio
async def test_a_reachable_trv_reports_the_lost_sensor_too(bt):
    """The counter-direction: a reachable valve reports the sensor too.

    Without it the test above would pass for a thermostat that reports
    degraded mode in no case at all.
    """
    with (
        patch(f"{_CLIMATE}.check_critical_entities", AsyncMock(return_value=True)),
        patch(f"{_CLIMATE}.check_ambient_air_temperature", AsyncMock()),
        patch(f"{_WATCHER}.ir.async_create_issue"),
        patch(f"{_HELPERS}.async_fire_logbook_entry", AsyncMock()),
    ):
        await BetterThermostat._trigger_time(bt, None)

    assert bt.degraded_mode is True


@pytest.mark.asyncio
async def test_a_recovered_sensor_clears_degraded_mode_while_a_trv_is_offline(bt):
    """The way back out, in the same combined failure case.

    A thermostat that entered degraded mode and then lost a valve would
    otherwise keep a repair issue nobody can dismiss, describing a sensor
    that has been healthy for hours.
    """
    bt.degraded_mode = True
    bt._degraded_warning_emitted = True
    bt.unavailable_sensors = [SENSOR_ID]
    bt.hass.states.get.return_value = MagicMock(state="20.5")

    with (
        patch(f"{_CLIMATE}.check_critical_entities", AsyncMock(return_value=False)),
        patch(f"{_CLIMATE}.check_ambient_air_temperature", AsyncMock()),
        patch(f"{_WATCHER}.ir.async_delete_issue") as delete_issue,
        patch(f"{_HELPERS}.async_fire_logbook_entry", AsyncMock()),
    ):
        await BetterThermostat._trigger_time(bt, None)

    assert bt.degraded_mode is False
    delete_issue.assert_called_once()
