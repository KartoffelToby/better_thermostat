"""End-to-end tests: config entry to real device writes.

These tests exist because the unit suite mocks the entity: a control
path that silently writes nothing keeps every unit test green. Here a
real entry is set up against a simulated TRV and the assertions are the
service calls that arrive at the device.
"""

from homeassistant.components.climate.const import ATTR_HVAC_ACTION
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.core import State
from pytest_homeassistant_custom_component.common import mock_restore_cache

from .conftest import (
    SENSOR_ID,
    WINDOW_ID,
    make_entry,
    setup_entry,
    wait_for,
    wait_for_startup,
)

BT_ENTITY = "climate.bt_test"


def _room_sensor(hass, value="18.0"):
    """Set the external room temperature sensor to ``value`` (°C)."""
    hass.states.async_set(SENSOR_ID, value, {"unit_of_measurement": "°C"})


async def test_setup_creates_the_entity_and_syncs_the_trv(hass, fake_trv):
    """Startup ends with a real setpoint write at the device."""
    _room_sensor(hass)
    entry = make_entry()
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)

    state = hass.states.get(BT_ENTITY)
    assert state is not None
    assert state.state == "heat"

    # The initial sync wrote a setpoint through the climate service.
    assert await wait_for(hass, lambda: fake_trv.set_temperature_calls)
    written = fake_trv.set_temperature_calls[-1]
    assert 5.0 <= written <= 30.0


async def test_window_open_turns_the_trv_off(hass, fake_trv):
    """A window-open event reaches the device as an OFF command."""
    _room_sensor(hass)
    hass.states.async_set(WINDOW_ID, "off")
    entry = make_entry(with_window=True)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)
    assert await wait_for(hass, lambda: fake_trv.set_temperature_calls)

    hass.states.async_set(WINDOW_ID, "on")
    assert await wait_for(hass, lambda: "off" in fake_trv.set_hvac_mode_calls)

    bt_state = hass.states.get(BT_ENTITY)
    assert bt_state.attributes.get("window_open") is True


async def test_restored_target_temperature_survives_a_restart(hass, fake_trv):
    """The restored target temperature drives the first sync."""
    mock_restore_cache(
        hass,
        [State(BT_ENTITY, "heat", {ATTR_TEMPERATURE: 23.5, ATTR_HVAC_ACTION: "idle"})],
    )
    _room_sensor(hass)
    entry = make_entry()
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)

    assert await wait_for(hass, lambda: fake_trv.set_temperature_calls)
    state = hass.states.get(BT_ENTITY)
    assert state.attributes.get(ATTR_TEMPERATURE) == 23.5


async def test_unload_and_reload_the_entry(hass, fake_trv):
    """Unloading stops the entry cleanly; reloading controls again.

    The entity runs several background tasks (control queue, window
    queue, keepalive) and many listeners — the classic leak class for
    custom components lives exactly here.
    """
    from homeassistant.config_entries import ConfigEntryState

    _room_sensor(hass)
    entry = make_entry()
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    bt_state = hass.states.get(BT_ENTITY)
    assert bt_state is None or bt_state.state == "unavailable"

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await wait_for_startup(hass, entry)
    assert hass.states.get(BT_ENTITY).state == "heat"

    # The device is already converged, so the restart sync rightly writes
    # nothing; a target change proves the reloaded entry controls again.
    fake_trv.set_temperature_calls.clear()
    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": BT_ENTITY, "temperature": 23.5},
        blocking=True,
    )
    assert await wait_for(hass, lambda: fake_trv.set_temperature_calls)


async def test_reconcile_tick_heals_a_lost_setpoint_write(hass, fake_trv):
    """A write the radio swallowed converges through the periodic tick.

    This pins the wiring: the five-minute interval is registered, the
    tick detects the commanded-vs-reported divergence, and the queued
    control cycle re-sends through the real service. The write budget
    is unit-tested elsewhere and zeroed here so the test does not have
    to wait out real wall-clock spacing.
    """
    from datetime import timedelta
    from unittest.mock import patch

    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    _room_sensor(hass)
    entry = make_entry()
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)
    assert await wait_for(hass, lambda: fake_trv.set_temperature_calls)

    with patch(
        "custom_components.better_thermostat.utils.controlling.MIN_WRITE_INTERVAL_S",
        0.0,
    ):
        # The device drops the write for the new target.
        fake_trv.drop_next_setpoint_write = True
        baseline_calls = len(fake_trv.set_temperature_calls)
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": BT_ENTITY, "temperature": 23.0},
            blocking=True,
        )
        assert await wait_for(
            hass, lambda: len(fake_trv.set_temperature_calls) > baseline_calls
        )
        lost = fake_trv.set_temperature_calls[-1]
        assert lost != fake_trv._attr_target_temperature  # really lost

        # The next reconcile tick detects the divergence and re-sends.
        resend_baseline = len(fake_trv.set_temperature_calls)
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=6))
        assert await wait_for(
            hass, lambda: len(fake_trv.set_temperature_calls) > resend_baseline
        )

    assert fake_trv.set_temperature_calls[-1] == lost
    assert fake_trv._attr_target_temperature == lost
