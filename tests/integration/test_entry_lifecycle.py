"""End-to-end tests: config entry to real device writes.

These tests exist because the unit suite mocks the entity: a control
path that silently writes nothing keeps every unit test green. Here a
real entry is set up against a simulated TRV and the assertions are the
service calls that arrive at the device.

The device form is an axis, not a constant: the fixtures are parametrized
indirectly over the profiles in ``device_profiles`` and every expectation
that depends on the device is derived from the profile it was built from.
"""

from homeassistant.components.climate import HVACMode
from homeassistant.components.climate.const import ATTR_HVAC_ACTION
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import mock_restore_cache

from .conftest import (
    BT_ENTITY,
    DOMAIN,
    WINDOW_ID,
    assert_on_device_grid,
    assert_profile_adopted,
    make_entry,
    profile_id,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import (
    DUAL_ROLE,
    GENERIC_HEAT_TRV,
    HEAT_COOL_TRV,
    INTEGER_GRID_TRV,
    ROLE_SCENARIOS,
)


@pytest.mark.parametrize(
    "fake_trv",
    [GENERIC_HEAT_TRV, HEAT_COOL_TRV, INTEGER_GRID_TRV],
    indirect=True,
    ids=profile_id,
)
async def test_setup_creates_the_entity_and_syncs_the_trv(hass, fake_trv):
    """Startup ends with a real setpoint write on the device's own grid."""
    set_room_sensor(hass, 18.0)
    entry = make_entry()
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    assert_profile_adopted(bt, fake_trv.profile)

    state = hass.states.get(BT_ENTITY)
    assert state is not None
    assert state.state == "heat"

    # The initial sync wrote a setpoint through the climate service, and it
    # arrived inside the range and on the grid this device published.
    assert await wait_for(hass, lambda: fake_trv.set_temperature_calls)
    written = fake_trv.set_temperature_calls[-1]
    assert fake_trv.profile.min_temp <= written <= fake_trv.profile.max_temp
    assert_on_device_grid(written, fake_trv.profile)


@pytest.mark.parametrize(
    "fake_trv", [GENERIC_HEAT_TRV, HEAT_COOL_TRV], indirect=True, ids=profile_id
)
async def test_window_open_turns_the_trv_off(hass, fake_trv):
    """A window-open event reaches the device as an OFF command.

    Whichever mode a device calls heating, it keeps that mode while the
    window is shut and receives the plain OFF it published when the window
    opens — the remap applies to the heating mode, not to OFF.
    """
    set_room_sensor(hass, 18.0)
    hass.states.async_set(WINDOW_ID, "off")
    entry = make_entry(with_window=True)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    assert_profile_adopted(bt, fake_trv.profile)
    assert await wait_for(hass, lambda: fake_trv.set_temperature_calls)
    assert hass.states.get(fake_trv.entity_id).state == fake_trv.profile.hvac_mode

    hass.states.async_set(WINDOW_ID, "on")
    assert await wait_for(hass, lambda: "off" in fake_trv.set_hvac_mode_calls)

    assert fake_trv.set_hvac_mode_calls[-1] == HVACMode.OFF
    assert hass.states.get(fake_trv.entity_id).state == HVACMode.OFF
    bt_state = hass.states.get(BT_ENTITY)
    assert bt_state.attributes.get("window_open") is True


@pytest.mark.parametrize(
    "fake_trv", [GENERIC_HEAT_TRV, INTEGER_GRID_TRV], indirect=True, ids=profile_id
)
async def test_restored_target_temperature_survives_a_restart(hass, fake_trv):
    """The restored target temperature drives the first sync.

    The device grid constrains the write, not the target: a thermostat in
    front of a whole-degree device keeps the half-degree target it restored
    and rounds only what it sends.
    """
    mock_restore_cache(
        hass,
        [State(BT_ENTITY, "heat", {ATTR_TEMPERATURE: 23.5, ATTR_HVAC_ACTION: "idle"})],
    )
    set_room_sensor(hass, 18.0)
    entry = make_entry()
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    assert_profile_adopted(bt, fake_trv.profile)

    assert await wait_for(hass, lambda: fake_trv.set_temperature_calls)
    state = hass.states.get(BT_ENTITY)
    assert state.attributes.get(ATTR_TEMPERATURE) == 23.5
    assert_on_device_grid(fake_trv.set_temperature_calls[-1], fake_trv.profile)


async def test_unload_and_reload_the_entry(hass, fake_trv):
    """Unloading stops the entry cleanly; reloading controls again.

    The entity runs several background tasks (control queue, window
    queue, keepalive) and many listeners — the classic leak class for
    custom components lives exactly here. Teardown is device-independent,
    and two full entry setups make this the most expensive test in the
    file, so it stays on the default device form.
    """
    from homeassistant.config_entries import ConfigEntryState

    set_room_sensor(hass, 18.0)
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


@pytest.mark.parametrize(
    ("device_role", "scenario"),
    [(role, role) for role in ROLE_SCENARIOS],
    indirect=["device_role"],
    ids=[role.name for role in ROLE_SCENARIOS],
)
async def test_climate_entity_id_follows_device_name_after_rename(
    hass, device_role, scenario
):
    """Renaming the device renames the climate (and sensor) entity_id to match.

    HA's entity registry reuses the existing entry on reload (unique id ==
    config entry id), so without an explicit rename the entity_id is frozen
    at first creation while only the friendly name follows the device.
    Blueprints that reference ``climate.bt_<room>`` then miss the entity.

    The rename runs over every wiring of the room — heat only, heat with a
    separate cooler, and one entity in both roles — because the entity id is
    rebuilt from the entry on every reload, whatever the entry controls.
    """
    set_room_sensor(hass, 18.0)
    entry = make_entry(name="Livingroom")
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)

    # The entry controls exactly what the scenario wired: a room with a cooler
    # trades plain heat for heat_cool, and a dual-role room points both
    # channels at the one entity it built.
    assert len(device_role) == (1 if scenario.cooler is None else 2)
    assert entry.data.get("cooler") == scenario.cooler_entity_id
    assert bt.cooler_entity_id == scenario.cooler_entity_id
    assert list(bt.real_trvs) == [scenario.trv.entity_id]
    assert (bt.cooler_entity_id in bt.real_trvs) is (scenario is DUAL_ROLE)
    assert_profile_adopted(bt, scenario.trv)
    if scenario.cooler_entity_id is None:
        assert HVACMode.HEAT in bt.hvac_modes
        assert HVACMode.HEAT_COOL not in bt.hvac_modes
    else:
        assert HVACMode.HEAT_COOL in bt.hvac_modes
        assert HVACMode.HEAT not in bt.hvac_modes

    registry = er.async_get(hass)
    climate_key = ("climate", DOMAIN, entry.entry_id)
    sensor_key = (DOMAIN, entry.entry_id + "_external_temp_ema")
    assert registry.async_get_entity_id(*climate_key) == "climate.livingroom"
    assert (
        registry.async_get_entity_id("sensor", *sensor_key)
        == "sensor.livingroom_temperature_ema"
    )

    # The device is renamed; the entity_id must follow to climate.bt_livingroom.
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "name": "BT Livingroom"}
    )
    await hass.async_block_till_done()
    await wait_for_startup(hass, entry)

    assert registry.async_get_entity_id(*climate_key) == "climate.bt_livingroom"
    assert (
        registry.async_get_entity_id("sensor", *sensor_key)
        == "sensor.bt_livingroom_temperature_ema"
    )
    assert hass.states.get("climate.bt_livingroom") is not None


@pytest.mark.parametrize(
    "fake_trv", [GENERIC_HEAT_TRV, INTEGER_GRID_TRV], indirect=True, ids=profile_id
)
async def test_reconcile_tick_heals_a_lost_setpoint_write(hass, fake_trv):
    """A write the radio swallowed converges through the periodic tick.

    This pins the wiring: the five-minute interval is registered, the
    tick detects the commanded-vs-reported divergence, and the queued
    control cycle re-sends through the real service. The divergence is
    measured against a tolerance of half a step, so the setpoint grid is
    an axis of the detection itself. The write budget is unit-tested
    elsewhere and zeroed here so the test does not have to wait out real
    wall-clock spacing.
    """
    from datetime import timedelta
    from unittest.mock import patch

    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    set_room_sensor(hass, 18.0)
    entry = make_entry()
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    assert_profile_adopted(bt, fake_trv.profile)
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
        assert_on_device_grid(lost, fake_trv.profile)

        # The next reconcile tick detects the divergence and re-sends.
        resend_baseline = len(fake_trv.set_temperature_calls)
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=6))
        assert await wait_for(
            hass, lambda: len(fake_trv.set_temperature_calls) > resend_baseline
        )

    assert fake_trv.set_temperature_calls[-1] == lost
    assert fake_trv._attr_target_temperature == lost
