"""Degraded-mode annunciation in the recurring trigger handlers.

Every handler updates the degraded-mode annunciation via
check_and_update_degraded_mode before the critical-entity check may abort it.
The combined failure case is what makes the order matter: a room sensor lost
while a TRV is offline is exactly when the user needs to be told, and it is
the one case where the critical-entity check reports failure.

The same order governs the way back. A degraded thermostat whose sensors all
return still has to clear its repair issue, and it cannot do that from behind
an early return either.
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

# Every handler that guards on the critical entities, with the arguments it
# takes beyond the entity itself. ``_trigger_window_change`` and
# ``_trigger_door_change`` delegate to ``_trigger_contact_change``, which is
# where the guard sits and which therefore stands in for both.
HANDLERS = [
    ("_trigger_time", (None,)),
    ("_trigger_check_weather", (None,)),
    ("_trigger_temperature_change", (MagicMock(),)),
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
    """The annunciation runs even though the critical check aborts the handler.

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
async def test_the_critical_check_still_stops_the_rest_of_the_handler(bt):
    """Moving the annunciation up does not remove the early return.

    The handler's own work stays behind the critical-entity check: there is
    nothing to compute against a valve that cannot be reached.
    """
    ambient = AsyncMock()
    with (
        patch(f"{_CLIMATE}.check_critical_entities", AsyncMock(return_value=False)),
        patch(f"{_CLIMATE}.check_ambient_air_temperature", ambient),
        patch(f"{_WATCHER}.ir.async_create_issue"),
        patch(f"{_HELPERS}.async_fire_logbook_entry", AsyncMock()),
    ):
        await BetterThermostat._trigger_time(bt, None)

    ambient.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_reachable_trv_reports_the_lost_sensor_too(bt):
    """The counter-direction: the annunciation did already work here.

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
