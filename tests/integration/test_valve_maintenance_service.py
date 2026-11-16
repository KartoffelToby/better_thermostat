"""Running valve maintenance on demand says why it did not run.

The service exercises the valves of every head that has valve maintenance
enabled. On a thermostat where none has, there is nothing to run; the call
says so instead of returning as if it had worked. A run the call started
belongs to the thermostat and ends with it.
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


@pytest.mark.parametrize(
    "fake_trv", [replace(GENERIC_HEAT_TRV, valve_maintenance=True)], indirect=True
)
async def test_service_exercises_valve_while_regulation_is_paused(hass, fake_trv):
    """The maintenance service performs its writes even with regulation paused."""
    set_room_sensor(hass, 18.0)
    pause_sensor = "input_boolean.regulation_pause"
    hass.states.async_set(pause_sensor, "on")
    entry = make_entry(fake_trv.profile, pause_sensor_entity_id=pause_sensor)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    writes = []

    async def exercise(
        infos, *, set_valve_fn, set_temperature_fn, set_hvac_mode_fn, **kwargs
    ):
        assert infos
        await set_valve_fn(fake_trv.entity_id, 100)
        await set_temperature_fn(fake_trv.entity_id, 30.0)
        await set_hvac_mode_fn(fake_trv.entity_id, "heat")

    async def record_valve(_bt, entity_id, value):
        writes.append(("valve", entity_id, value))
        return True

    async def record_temperature(_bt, entity_id, value):
        writes.append(("temperature", entity_id, value))

    async def record_mode(_bt, entity_id, value):
        writes.append(("mode", entity_id, value))

    with (
        patch(MAINTENANCE_EXERCISE, side_effect=exercise),
        patch(
            "custom_components.better_thermostat.climate.adapter_set_valve",
            record_valve,
        ),
        patch(
            "custom_components.better_thermostat.climate.adapter_set_temperature",
            record_temperature,
        ),
        patch(
            "custom_components.better_thermostat.climate.adapter_set_hvac_mode",
            record_mode,
        ),
        patch(
            "custom_components.better_thermostat.climate.compute_next_maintenance",
            return_value=bt.clock.now(),
        ),
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RUN_VALVE_MAINTENANCE,
            {"entity_id": bt.entity_id},
            blocking=True,
        )

    assert bt.regulation_paused
    assert writes == [
        ("valve", fake_trv.entity_id, 100),
        ("temperature", fake_trv.entity_id, 30.0),
        ("mode", fake_trv.entity_id, "heat"),
    ]
