"""Running valve maintenance on demand says why it did not run.

The service exercises the valves of every head that has valve maintenance
enabled. On a thermostat where none has, there is nothing to run; the call
says so instead of returning as if it had worked.
"""

import asyncio
from dataclasses import replace
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ServiceValidationError
import pytest

from custom_components.better_thermostat.utils.const import (
    SERVICE_RUN_VALVE_MAINTENANCE,
)

from .conftest import DOMAIN, make_entry, set_room_sensor, setup_entry, wait_for_startup
from .device_profiles import GENERIC_HEAT_TRV


async def test_maintenance_without_an_enabled_head_is_refused(hass, fake_trv):
    """No head has valve maintenance enabled, so the call is refused."""
    assert not GENERIC_HEAT_TRV.valve_maintenance
    set_room_sensor(hass, 18.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)

    with pytest.raises(ServiceValidationError) as refused:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RUN_VALVE_MAINTENANCE,
            {"entity_id": bt.entity_id},
            blocking=True,
        )

    assert refused.value.translation_key == "valve_maintenance_not_enabled"
    assert str(refused.value) == ("BT Test has no valve with valve maintenance enabled")


MAINTENANCE_EXERCISE = (
    "custom_components.better_thermostat.climate.run_valve_maintenance"
)


@pytest.mark.parametrize(
    "fake_trv", [replace(GENERIC_HEAT_TRV, valve_maintenance=True)], indirect=True
)
async def test_a_run_started_by_the_service_ends_with_the_thermostat(hass, fake_trv):
    """Unloading the thermostat stops a run the service started.

    The exercise holds the valves at their extremes for about two minutes.
    A run the unload does not stop keeps writing to the TRVs on behalf of a
    thermostat that no longer exists, and never puts them back.
    """
    set_room_sensor(hass, 18.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)

    exercising = asyncio.Event()
    stopped = asyncio.Event()

    async def held_exercise(*_args, **_kwargs):
        exercising.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            stopped.set()
            raise

    with patch(MAINTENANCE_EXERCISE, held_exercise):
        # The exercise holds the call open, so it runs beside the test.
        call = asyncio.ensure_future(
            hass.services.async_call(
                DOMAIN,
                SERVICE_RUN_VALVE_MAINTENANCE,
                {"entity_id": bt.entity_id},
                blocking=True,
            )
        )
        try:
            await asyncio.wait_for(exercising.wait(), 5)
            assert await hass.config_entries.async_unload(entry.entry_id)
            stopped_with_the_unload = stopped.is_set()
            if stopped_with_the_unload:
                # A run the removal stopped ends the call without an error.
                await asyncio.wait_for(call, 5)
        finally:
            call.cancel()
            await asyncio.gather(call, return_exceptions=True)

    assert stopped_with_the_unload
    assert entry.state is ConfigEntryState.NOT_LOADED
