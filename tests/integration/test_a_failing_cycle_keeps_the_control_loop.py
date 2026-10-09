"""A control cycle that raises does not end the control loop.

One task runs every control cycle a thermostat does, and nothing restarts it.
An error escaping one cycle would leave every later target change, window
event and reconcile tick queued for a loop that is gone, until the entry is
reloaded.
"""

from unittest.mock import patch

from homeassistant.components.climate import (
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_TEMPERATURE,
)

from custom_components.better_thermostat.utils import controlling

from .conftest import (
    BT_ENTITY,
    WRITE_BUDGET,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)


async def test_the_cycle_after_a_failing_one_still_runs(hass, fake_trv, caplog):
    """A target set after a cycle that raised still reaches the TRV."""
    set_room_sensor(hass, 18.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)

    announce = controlling.announce_learned_state
    failures: list[int] = []

    def fails_once(*args):
        if not failures:
            failures.append(1)
            raise RuntimeError("helper failed at the end of a cycle")
        return announce(*args)

    with (
        patch.object(controlling, "announce_learned_state", fails_once),
        patch(WRITE_BUDGET, 0.0),
    ):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {"entity_id": BT_ENTITY, "temperature": 23.0},
            blocking=True,
        )
        assert await wait_for(hass, lambda: bool(failures))
        written = len(fake_trv.set_temperature_calls)

        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {"entity_id": BT_ENTITY, "temperature": 17.0},
            blocking=True,
        )
        assert await wait_for(
            hass, lambda: len(fake_trv.set_temperature_calls) > written
        )

    control_task = bt._control_task
    assert control_task is not None
    assert not control_task.done()
    assert await wait_for(hass, lambda: bt.ignore_states is False)
    assert "helper failed at the end of a cycle" in caplog.text


async def test_a_failing_cycle_still_reads_what_the_trvs_reported(hass, fake_trv):
    """The reports held during a cycle that raised are read when it ends.

    The inbound handler stands down for a cycle. A cycle that completes reads
    what the TRVs reported meanwhile before it takes the next request; one
    that raised does the same, or a setpoint turned at the device during it
    waits for the device's next report.
    """
    set_room_sensor(hass, 18.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)

    refresh = controlling.refresh_cached_trv_modes
    read_held = controlling.read_reports_held_during_cycle
    failures: list[int] = []
    settled_after_the_failure: list[str] = []

    def fails_once(*_args):
        failures.append(1)
        raise RuntimeError("helper failed at the end of a cycle")

    def refresh_spy(bt):
        if failures:
            settled_after_the_failure.append("modes")
        return refresh(bt)

    async def read_held_spy(bt):
        if failures:
            settled_after_the_failure.append("reports")
        return await read_held(bt)

    with (
        patch.object(controlling, "announce_learned_state", fails_once),
        patch.object(controlling, "refresh_cached_trv_modes", refresh_spy),
        patch.object(controlling, "read_reports_held_during_cycle", read_held_spy),
        patch(WRITE_BUDGET, 0.0),
    ):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {"entity_id": BT_ENTITY, "temperature": 23.0},
            blocking=True,
        )
        assert await wait_for(hass, lambda: bool(failures))
        assert await wait_for(
            hass, lambda: "reports" in settled_after_the_failure, timeout_seconds=2.0
        )

    assert settled_after_the_failure[:2] == ["modes", "reports"]
