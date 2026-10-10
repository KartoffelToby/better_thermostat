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
from functools import partial
from unittest.mock import patch

from homeassistant.components.climate import HVACMode
from homeassistant.components.climate.const import ATTR_HVAC_ACTION
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityPlatformState
import pytest
from pytest_homeassistant_custom_component.common import mock_restore_cache

from custom_components.better_thermostat.climate import BetterThermostat
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
    HEAT_ONLY,
    INTEGER_GRID_TRV,
    ROLE_SCENARIOS,
    SEPARATE_COOLER,
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


async def test_window_sensor_removed_while_open_lets_the_room_heat(hass, fake_trv):
    """A window sensor that disappears while open counts as closed.

    Disabling, deleting or renaming the sensor in the entity registry
    removes its state. Like an unavailable sensor, a removed one must not
    hold the heating off until the entry is reloaded.
    """
    set_room_sensor(hass, 18.0)
    hass.states.async_set(WINDOW_ID, "off")
    entry = make_entry(fake_trv.profile, with_window=True)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    await bt.async_set_hvac_mode(HVACMode.HEAT)
    await hass.async_block_till_done()
    hass.states.async_set(WINDOW_ID, "on")
    assert await wait_for(hass, lambda: "off" in fake_trv.set_hvac_mode_calls)

    hass.states.async_remove(WINDOW_ID)

    assert await wait_for(hass, lambda: not bt.window_open)
    assert await wait_for(
        hass, lambda: fake_trv.set_hvac_mode_calls[-1] == fake_trv.profile.hvac_mode
    )
    assert hass.states.get(BT_ENTITY).attributes.get("window_open") is False


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
    assert entry.options.get("cooler") == scenario.cooler_entity_id
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
        entry, options={**entry.options, "name": "BT Livingroom"}
    )
    await hass.async_block_till_done()
    await wait_for_startup(hass, entry)

    assert registry.async_get_entity_id(*climate_key) == "climate.bt_livingroom"
    assert (
        registry.async_get_entity_id("sensor", *sensor_key)
        == "sensor.bt_livingroom_temperature_ema"
    )
    assert hass.states.get("climate.bt_livingroom") is not None


RENAMED_ENTITY = "climate.living_room_heating"


def _targets(scenario, heat: float) -> dict[str, float]:
    """The set_temperature data for a heating target, with a cooler or without."""
    if scenario.cooler_entity_id is None:
        return {ATTR_TEMPERATURE: heat}
    return {"target_temp_low": heat, "target_temp_high": 27.0}


async def _rename_the_thermostat(hass, entry):
    """Give the thermostat a new entity_id, as the entity settings dialog does."""
    er.async_get(hass).async_update_entity(BT_ENTITY, new_entity_id=RENAMED_ENTITY)
    await hass.async_block_till_done()
    return await wait_for_startup(hass, entry)


@pytest.mark.parametrize(
    "device_role", [HEAT_ONLY, SEPARATE_COOLER], indirect=True, ids=profile_id
)
async def test_a_thermostat_renamed_by_the_user_keeps_driving_the_trv(
    hass, device_role, caplog
):
    """A new entity_id from the user leaves a thermostat that still writes.

    Home Assistant answers an entity_id change in the registry by removing
    the entity and adding the same object again under the new id. The
    removal stops everything that writes to the TRV, so the entity has to
    come back as one that runs: the target set under the new id reaches the
    device. With a cooler the mode list changes at startup, which is where
    a second start of the same object breaks, so both wirings run.
    """
    set_room_sensor(hass, 18.0)
    entry = make_entry(device_role.scenario)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)

    bt = await _rename_the_thermostat(hass, entry)

    assert bt.entity_id == RENAMED_ENTITY
    assert hass.states.get(BT_ENTITY) is None
    assert hass.states.get(RENAMED_ENTITY).state == bt.map_on_hvac_mode
    assert [r.message for r in caplog.records if r.levelname == "ERROR"] == []

    trv = device_role.thermostat
    trv.set_temperature_calls.clear()
    # The reloaded startup has just written to the head; without the budget
    # the target would wait out the minimum interval between two writes.
    with patch(WRITE_BUDGET, 0.0):
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": RENAMED_ENTITY, **_targets(device_role.scenario, 23.5)},
            blocking=True,
        )
        assert await wait_for(hass, lambda: trv.set_temperature_calls)
    # target_temp_based sends the target corrected by how far the device's
    # own reading sits from the room sensor.
    corrected = 23.5 - 18.0 + trv.profile.current_temperature
    assert_write_is(trv.set_temperature_calls[-1], corrected, trv.profile)


async def test_a_removed_thermostat_added_twice_schedules_one_reload(hass, fake_trv):
    """Each re-add of the removed object after an entity_id change would reload.

    A second entity_id change before the reload runs adds the same removed
    object once more; the reload already scheduled serves both.
    """
    set_room_sensor(hass, 18.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    bt.is_removed = True
    scheduled: list[str] = []

    with patch.object(hass.config_entries, "async_schedule_reload", scheduled.append):
        await bt.async_added_to_hass()
        await bt.async_added_to_hass()
        await hass.async_block_till_done()

    assert scheduled == [entry.entry_id]


@pytest.mark.parametrize(
    "device_role", [HEAT_ONLY, SEPARATE_COOLER], indirect=True, ids=profile_id
)
async def test_a_thermostat_renamed_by_the_user_keeps_its_targets(hass, device_role):
    """The targets set before a new entity_id are the ones shown after it.

    Home Assistant files the state it saves at the removal under the old
    entity_id. A thermostat restoring under the new one finds a saved state
    only if the reload removes the old object after Home Assistant has
    published it under the new id; otherwise it falls back to the TRV's own
    setpoint.
    """
    set_room_sensor(hass, 18.0)
    entry = make_entry(device_role.scenario)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)
    targets = _targets(device_role.scenario, 22.5)
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": BT_ENTITY, **targets}, blocking=True
    )

    await _rename_the_thermostat(hass, entry)

    attributes = hass.states.get(RENAMED_ENTITY).attributes
    assert {key: attributes[key] for key in targets} == targets


@pytest.mark.parametrize(
    "device_role", [HEAT_ONLY, SEPARATE_COOLER], indirect=True, ids=profile_id
)
async def test_a_thermostat_renamed_by_the_user_publishes_only_the_new_entity(
    hass, device_role
):
    """Once the new entity has written its state, the old object stays silent.

    Home Assistant finishes adding the old object after its
    ``async_added_to_hass`` returns: it marks the object as added and writes
    its state. The reload has to remove the old object after that, or the
    old object writes its stale state over the new entity's and stays
    marked as added although nothing drives it.
    """
    set_room_sensor(hass, 18.0)
    entry = make_entry(device_role.scenario)
    await setup_entry(hass, entry)
    old_bt = await wait_for_startup(hass, entry)
    writers: list[BetterThermostat] = []
    write_state = BetterThermostat.async_write_ha_state

    def recording_write(entity: BetterThermostat) -> None:
        writers.append(entity)
        write_state(entity)

    with patch.object(BetterThermostat, "async_write_ha_state", recording_write):
        new_bt = await _rename_the_thermostat(hass, entry)

    assert new_bt is not old_bt
    first_new_write = writers.index(new_bt)
    assert old_bt not in writers[first_new_write:]
    assert old_bt._platform_state is EntityPlatformState.REMOVED


@contextmanager
def _recording_intervals(registered):
    """Record name and interval for every handler startup puts on a timer."""
    from custom_components.better_thermostat import climate as climate_module

    real = climate_module.async_track_time_interval

    def _record(hass, action, interval, *args, **kwargs):
        # Each tick reaches the tracker through a dispatcher that spawns the
        # firing as work the entity owns, bound with ``partial``. What is on
        # the interval is the tick that dispatcher runs.
        tick = action.args[0] if isinstance(action, partial) else action
        registered.append((getattr(tick, "__name__", repr(tick)), interval))
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
    never comes, and no cycle re-sends while a write is in flight. The
    healing tick is therefore one that fires after that wait ends.
    """
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    set_room_sensor(hass, 18.0)
    entry = make_entry(fake_trv.profile)
    registered: list[tuple[str, timedelta]] = []
    with _recording_intervals(registered):
        await setup_entry(hass, entry)
        bt = await wait_for_startup(hass, entry)

    # The premise of the parametrization, read off this very startup: the
    # reconciler is the only five-minute handler, so a re-send comes from
    # nowhere else. Claimed as the whole set, because any other handler on
    # that interval would be an equally good suspect. The ladder tick runs
    # more often but only advances the degradation ladder and re-reads the
    # critical entities; with the room sensor steady it commits no rung and
    # queues no control cycle, so it heals nothing.
    assert _on_the_five_minute_tick(registered) == ["_reconcile_tick"]

    assert_profile_adopted(bt, fake_trv.profile)
    assert await wait_for(hass, lambda: fake_trv.set_temperature_calls)

    with patch(WRITE_BUDGET, 0.0):
        # The device drops the write for the new target. The target sits
        # below the room reading: a room below its target already has the
        # head on its maximum, so a higher target would command nothing new.
        fake_trv.drop_next_setpoint_write = True
        baseline_calls = len(fake_trv.set_temperature_calls)
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": BT_ENTITY, "temperature": 16.0},
            blocking=True,
        )
        assert await wait_for(
            hass, lambda: len(fake_trv.set_temperature_calls) > baseline_calls
        )
        lost = fake_trv.set_temperature_calls[-1]
        assert lost != fake_trv._attr_target_temperature  # really lost
        assert_on_device_grid(lost, fake_trv.profile)

        # The entity waits out the confirmation the device never sends;
        # the tick after that finds the divergence and re-sends.
        trv = bt.real_trvs[fake_trv.entity_id]
        assert await wait_for(hass, lambda: trv.target_temperature_received)
        resend_baseline = len(fake_trv.set_temperature_calls)
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=6))
        assert await wait_for(
            hass, lambda: len(fake_trv.set_temperature_calls) > resend_baseline
        )

    assert fake_trv.set_temperature_calls[-1] == lost
    assert fake_trv._attr_target_temperature == lost


@pytest.mark.parametrize(
    "fake_trv",
    [
        replace(
            GENERIC_HEAT_TRV,
            name=f"generic_heat_trv_{mode.value}",
            calibration_mode=mode.value,
        )
        for mode in CalibrationMode
    ],
    indirect=True,
    ids=profile_id,
)
async def test_a_quiet_room_does_not_trip_the_control_watchdog(hass, fake_trv, caplog):
    """An hour without a reason to control is not a stalled control loop.

    A room holding its temperature publishes no state change, and a
    calibration mode without the five-minute recompute queues no cycle of
    its own, so in such a room no control cycle may run for an hour. The
    devices still hold the intent, so the watchdog has no hang to report
    and no cycle to force, whichever calibration mode the room runs.
    """
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from custom_components.better_thermostat.core.clock import FakeClock

    set_room_sensor(hass, 19.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    # The periodic ticks are registered at the very end of startup.
    await hass.async_block_till_done()

    clock = FakeClock(monotonic_value=bt.clock.monotonic())
    bt.clock = clock
    start = dt_util.utcnow()
    caplog.clear()
    elapsed = 0
    while elapsed < 3600:
        clock.advance(30)
        elapsed += 30
        async_fire_time_changed(hass, start + timedelta(seconds=elapsed))
        await hass.async_block_till_done()

    assert [r.message for r in caplog.records if "control watchdog" in r.message] == []


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
