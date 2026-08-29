"""End-to-end tests: config entry to real device writes.

These tests exist because the unit suite mocks the entity: a control
path that silently writes nothing keeps every unit test green. Here a
real entry is set up against a simulated TRV and the assertions are the
service calls that arrive at the device.

The device form is an axis, not a constant: the fixtures are parametrized
indirectly over the profiles in ``device_profiles`` and every expectation
that depends on the device is derived from the profile it was built from.
"""

from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from homeassistant.components.climate import HVACMode
from homeassistant.components.climate.const import ATTR_HVAC_ACTION
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import mock_restore_cache

from custom_components.better_thermostat.utils.const import CalibrationMode

from .conftest import (
    BT_ENTITY,
    DOMAIN,
    WINDOW_ID,
    WRITE_BUDGET,
    assert_on_device_grid,
    assert_profile_adopted,
    assert_write_is,
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
    entry = make_entry(fake_trv.profile)
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
    entry = make_entry(fake_trv.profile, with_window=True)
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
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    assert_profile_adopted(bt, fake_trv.profile)

    assert await wait_for(hass, lambda: fake_trv.set_temperature_calls)
    state = hass.states.get(BT_ENTITY)
    assert state.attributes.get(ATTR_TEMPERATURE) == 23.5
    # target_temp_based does not send the target itself: it sends the target
    # corrected by how far the device's own reading sits from the room sensor.
    corrected = 23.5 - 18.0 + fake_trv.profile.current_temperature
    assert_write_is(fake_trv.set_temperature_calls[-1], corrected, fake_trv.profile)


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
    entry = make_entry(fake_trv.profile)
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


@pytest.mark.parametrize("device_role", ROLE_SCENARIOS, indirect=True, ids=profile_id)
async def test_the_role_scenario_decides_what_the_entry_controls(hass, device_role):
    """The entry wires exactly the devices and channels its role names.

    A room with a cooler trades plain heat for heat_cool, a heat-only room
    keeps heat and never offers heat_cool, and a dual-role room points both
    channels at the one entity it built — the same entity is the thermostat
    and the cooler, which is the wiring the cooling path has to survive.
    """
    scenario = device_role.scenario
    set_room_sensor(hass, 18.0)
    entry = make_entry(scenario)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)

    assert len(device_role.entities) == (1 if scenario.cooler is None else 2)
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


@pytest.mark.parametrize("device_role", ROLE_SCENARIOS, indirect=True, ids=profile_id)
async def test_climate_entity_id_follows_device_name_after_rename(hass, device_role):
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
    entry = make_entry(device_role.scenario, name="Livingroom")
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)

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


@contextmanager
def _recording_intervals(registered):
    """Record name and interval for every handler startup puts on a timer."""
    from custom_components.better_thermostat import climate as climate_module

    real = climate_module.async_track_time_interval

    def _record(hass, action, interval, *args, **kwargs):
        registered.append((getattr(action, "__name__", repr(action)), interval))
        return real(hass, action, interval, *args, **kwargs)

    with patch.object(climate_module, "async_track_time_interval", _record):
        yield


def _on_the_five_minute_tick(registered):
    """The handlers startup put on a five-minute interval, in order."""
    return [name for name, interval in registered if interval == timedelta(minutes=5)]


def _without_the_control_tick(profile):
    """The same device, on a calibration mode that registers no control tick.

    The five-minute control tick re-sends on its own whenever it fires,
    so a device configured for one heals a lost write whether or not the
    reconciler runs. Only a calibration mode outside that tick's gate
    leaves the reconciler as the sole periodic path.
    """
    return replace(
        profile,
        name=f"{profile.name}_no_control_tick",
        calibration_mode=CalibrationMode.HEATING_POWER_CALIBRATION.value,
    )


@pytest.mark.parametrize(
    "fake_trv",
    [
        _without_the_control_tick(GENERIC_HEAT_TRV),
        _without_the_control_tick(INTEGER_GRID_TRV),
    ],
    indirect=True,
    ids=profile_id,
)
async def test_reconcile_tick_heals_a_lost_setpoint_write(hass, fake_trv):
    """A write the radio swallowed converges through the reconcile tick.

    The tick detects the commanded-vs-reported divergence and the queued
    control cycle re-sends through the real service. The divergence is
    measured against a tolerance of half a step, so the setpoint grid is
    an axis of the detection itself. That the interval is registered at
    all is pinned as a set in ``test_climate_startup_registration``. The
    write budget is unit-tested elsewhere and zeroed here so the test
    does not have to wait out real wall-clock spacing.

    A dropped write leaves the entity waiting for a confirmation that
    never comes, and the reconciler stands down while a write is in
    flight. The healing tick is therefore the one after that wait ends,
    not the first one to fire, and the loop below is what carries the
    scenario across it.
    """
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    set_room_sensor(hass, 18.0)
    entry = make_entry(fake_trv.profile)
    registered = []
    with _recording_intervals(registered):
        await setup_entry(hass, entry)
        bt = await wait_for_startup(hass, entry)

    # The premise of the parametrization, read off this very startup: the
    # reconciler is the only five-minute handler that can re-send, so a
    # re-send comes from nowhere else. Claimed as the whole set, because any
    # other handler on that interval would be an equally good suspect. The
    # availability tick shares the interval but only advances the degradation
    # ladder and re-reads the critical entities, queueing no control cycle,
    # so it heals nothing.
    assert sorted(_on_the_five_minute_tick(registered)) == [
        "_availability_tick",
        "_reconcile_tick",
    ]

    assert_profile_adopted(bt, fake_trv.profile)
    assert await wait_for(hass, lambda: fake_trv.set_temperature_calls)

    with patch(WRITE_BUDGET, 0.0):
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

        # The first tick lands inside the confirmation wait and stands
        # down; the one after it finds the divergence and re-sends.
        resend_baseline = len(fake_trv.set_temperature_calls)
        for minutes in (6, 12):
            async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=minutes))
            if await wait_for(
                hass, lambda: len(fake_trv.set_temperature_calls) > resend_baseline
            ):
                break
        assert len(fake_trv.set_temperature_calls) > resend_baseline

    assert fake_trv.set_temperature_calls[-1] == lost
    assert fake_trv._attr_target_temperature == lost


@pytest.mark.parametrize("fake_trv", [GENERIC_HEAT_TRV], indirect=True, ids=profile_id)
async def test_entity_ids_the_user_chose_survive_a_restart(hass, fake_trv):
    """A restart leaves the ids in the registry alone, whoever wrote them.

    An entity_id is the user's to set, and the ones seeded here are the
    form somebody who named BT's entities themselves holds. Setting the
    entry up finds them already in the registry, which is what a restart
    looks like from the integration's side.

    Both platforms are seeded: the climate entity derives its id from the
    entry, the auxiliary ones from the device, and those are two different
    code paths.
    """
    set_room_sensor(hass, 18.0)
    entry = make_entry(fake_trv.profile, name="Livingroom")
    entry.add_to_hass(hass)

    registry = er.async_get(hass)
    registry.async_get_or_create(
        "climate",
        DOMAIN,
        entry.entry_id,
        config_entry=entry,
        suggested_object_id="livingroom_thermostat",
    )
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        entry.entry_id + "_external_temp_ema",
        config_entry=entry,
        suggested_object_id="livingroom_thermostat_ema",
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await wait_for_startup(hass, entry)

    assert (
        registry.async_get_entity_id("climate", DOMAIN, entry.entry_id)
        == "climate.livingroom_thermostat"
    )
    assert (
        registry.async_get_entity_id(
            "sensor", DOMAIN, entry.entry_id + "_external_temp_ema"
        )
        == "sensor.livingroom_thermostat_ema"
    )
