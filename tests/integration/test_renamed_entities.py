"""An entity the entry names keeps working under the entity id the user gives it.

Home Assistant lets the user change any entity's id. The entry names its
thermostats and sensors by entity id, so a rename has to carry over to the
settings, to the thermostat's own entities and to the state learned for it,
or the room stops being controlled although nothing about the device changed.
A thermostat the entry stops controlling leaves nothing behind for the next
device to be given its id.
"""

from dataclasses import replace

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.better_thermostat.utils.calibration.pid import (
    PIDState,
    build_pid_key,
)
from custom_components.better_thermostat.utils.const import CalibrationMode
from custom_components.better_thermostat.utils.helpers import entry_issue_id
from custom_components.better_thermostat.utils.renamed_entities import (
    move_trv_unique_ids,
)
from custom_components.better_thermostat.utils.state_manager import StateManager

from .conftest import (
    BT_ENTITY,
    DOMAIN,
    SENSOR_ID,
    build_devices,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import GROUP_OF_THREE, MQTT_OFFSET_TRV

PID = CalibrationMode.PID_CALIBRATION.value
PID_TRV = replace(MQTT_OFFSET_TRV, calibration_mode=PID)
RENAMED_TRV = "climate.renamed_trv"
LEARNED_KP = 77.0


def _configured_trvs(entry) -> list[str]:
    return [trv_config["trv"] for trv_config in entry.options["thermostat"]]


def _per_trv_entity_id(hass, entry, trv_entity_id, platform, suffix) -> str | None:
    return er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{entry.entry_id}_{trv_entity_id}_{suffix}"
    )


async def _reloaded(hass, entry, before):
    """Return the thermostat once a reload has replaced ``before`` and started."""
    assert await wait_for(
        hass,
        lambda: (
            entry.state is ConfigEntryState.LOADED
            and entry.runtime_data.climate is not None
            and entry.runtime_data.climate is not before
        ),
    )
    return await wait_for_startup(hass, entry)


def _learn_kp(bt, trv_entity_id, kp=LEARNED_KP) -> None:
    assert bt.state_mgr is not None
    # Auto-tuning off, so the gain stays what the test learned it to be.
    bt.state_mgr.set_pid(
        build_pid_key(bt, trv_entity_id), PIDState(pid_kp=kp, auto_tune=False)
    )


def _stored_kp(bt, trv_entity_id) -> float | None:
    assert bt.state_mgr is not None
    pid_state = bt.state_mgr.state.pid.get(build_pid_key(bt, trv_entity_id))
    return None if pid_state is None else pid_state.pid_kp


async def _started_pid_trv(hass):
    set_room_sensor(hass, 19.0)
    await build_devices(hass, PID_TRV)
    entry = make_entry(PID_TRV)
    advanced = entry.data["thermostat"][0]["advanced"]
    advanced["child_lock"] = True
    advanced["protect_overheating"] = True
    await setup_entry(hass, entry)
    return entry, await wait_for_startup(hass, entry)


async def test_a_renamed_thermostat_is_driven_under_its_new_id(hass):
    """The settings, the thermostat's entities and its learned gains follow.

    The thermostat's own entities keep the registry rows the user may have
    customized, here an entity id of the user's choosing.
    """
    entry, bt = await _started_pid_trv(hass)
    registry = er.async_get(hass)
    advanced_before = dict(entry.options["thermostat"][0]["advanced"])
    kp_number = "number.radiator_gain"
    generated = _per_trv_entity_id(hass, entry, PID_TRV.entity_id, "number", "pid_kp")
    assert generated is not None
    registry.async_update_entity(generated, new_entity_id=kp_number)
    child_lock = _per_trv_entity_id(
        hass, entry, PID_TRV.entity_id, "switch", "child_lock"
    )
    assert child_lock is not None
    await hass.async_block_till_done()
    _learn_kp(bt, PID_TRV.entity_id)

    registry.async_update_entity(PID_TRV.entity_id, new_entity_id=RENAMED_TRV)
    await hass.async_block_till_done()
    bt = await _reloaded(hass, entry, bt)

    assert _configured_trvs(entry) == [RENAMED_TRV]
    assert entry.options["thermostat"][0]["advanced"] == advanced_before
    assert list(bt.real_trvs) == [RENAMED_TRV]
    assert hass.states.get(BT_ENTITY).state != STATE_UNAVAILABLE
    assert _per_trv_entity_id(hass, entry, RENAMED_TRV, "number", "pid_kp") == kp_number
    assert (
        _per_trv_entity_id(hass, entry, RENAMED_TRV, "switch", "child_lock")
        == child_lock
    )
    assert (
        _per_trv_entity_id(hass, entry, PID_TRV.entity_id, "number", "pid_kp") is None
    )
    assert _stored_kp(bt, RENAMED_TRV) == LEARNED_KP
    assert float(hass.states.get(kp_number).state) == LEARNED_KP


async def test_a_thermostat_renamed_while_its_entry_is_unloaded_is_followed(hass):
    """The stored settings and learned state follow without a running thermostat."""
    entry, bt = await _started_pid_trv(hass)
    _learn_kp(bt, PID_TRV.entity_id)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    er.async_get(hass).async_update_entity(PID_TRV.entity_id, new_entity_id=RENAMED_TRV)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert _configured_trvs(entry) == [RENAMED_TRV]
    stored = StateManager(hass, entry.entry_id)
    await stored.load()
    stored.close()
    assert {key.split(":")[1] for key in stored.state.pid} == {RENAMED_TRV}

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    bt = await wait_for_startup(hass, entry)
    assert _stored_kp(bt, RENAMED_TRV) == LEARNED_KP


async def test_two_renames_in_a_row_end_under_the_last_id(hass):
    """A second rename that arrives before the first reload is followed too."""
    entry, bt = await _started_pid_trv(hass)
    _learn_kp(bt, PID_TRV.entity_id)
    registry = er.async_get(hass)

    registry.async_update_entity(PID_TRV.entity_id, new_entity_id="climate.interim")
    registry.async_update_entity("climate.interim", new_entity_id=RENAMED_TRV)
    await hass.async_block_till_done()
    bt = await _reloaded(hass, entry, bt)

    assert _configured_trvs(entry) == [RENAMED_TRV]
    assert _stored_kp(bt, RENAMED_TRV) == LEARNED_KP


async def test_two_renames_while_unloaded_end_under_the_last_id(hass):
    entry, bt = await _started_pid_trv(hass)
    _learn_kp(bt, PID_TRV.entity_id)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)

    registry.async_update_entity(PID_TRV.entity_id, new_entity_id="climate.interim")
    registry.async_update_entity("climate.interim", new_entity_id=RENAMED_TRV)
    await hass.async_block_till_done()

    assert _configured_trvs(entry) == [RENAMED_TRV]
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    bt = await wait_for_startup(hass, entry)
    assert _stored_kp(bt, RENAMED_TRV) == LEARNED_KP


async def test_a_renamed_room_sensor_is_read_under_its_new_id(hass):
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor", "test", "room", suggested_object_id=SENSOR_ID.split(".", 1)[1]
    )
    entry, bt = await _started_pid_trv(hass)
    renamed_sensor = "sensor.living_room_temperature"

    registry.async_update_entity(SENSOR_ID, new_entity_id=renamed_sensor)
    hass.states.async_set(renamed_sensor, "21.5", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    bt = await _reloaded(hass, entry, bt)

    assert entry.options["temperature_sensor"] == renamed_sensor
    assert await wait_for(hass, lambda: bt.room_temperature == 21.5)


async def test_a_rename_clears_the_missing_entity_repair_of_the_old_id(hass):
    entry, bt = await _started_pid_trv(hass)
    old_issue = entry_issue_id(entry.entry_id, "missing_entity", PID_TRV.entity_id)
    ir.async_create_issue(
        hass,
        DOMAIN,
        old_issue,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="missing_entity",
    )

    er.async_get(hass).async_update_entity(PID_TRV.entity_id, new_entity_id=RENAMED_TRV)
    await hass.async_block_till_done()
    await _reloaded(hass, entry, bt)

    assert ir.async_get(hass).async_get_issue(DOMAIN, old_issue) is None


async def test_an_entity_the_entry_does_not_name_changes_nothing(hass):
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor", "test", "unrelated", suggested_object_id="unrelated"
    )
    entry, bt = await _started_pid_trv(hass)
    options_before = dict(entry.options)

    registry.async_update_entity("sensor.unrelated", new_entity_id="sensor.other")
    await hass.async_block_till_done()

    assert dict(entry.options) == options_before
    assert entry.runtime_data.climate is bt


async def test_a_thermostat_whose_id_extends_the_renamed_one_keeps_its_entities(hass):
    """Unique ids are moved per thermostat, not per shared prefix."""
    registry = er.async_get(hass)
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    entry_id = entry.entry_id
    for trv_entity_id in ("climate.trv", "climate.trv_2"):
        registry.async_get_or_create(
            "switch",
            DOMAIN,
            f"{entry_id}_{trv_entity_id}_child_lock",
            config_entry=entry,
        )

    moved = move_trv_unique_ids(
        registry,
        entry_id,
        ["climate.trv", "climate.trv_2"],
        "climate.trv",
        "climate.radiator",
    )

    assert moved == 1
    assert {
        reg_entry.unique_id
        for reg_entry in registry.entities.values()
        if reg_entry.platform == DOMAIN
    } == {
        f"{entry_id}_climate.radiator_child_lock",
        f"{entry_id}_climate.trv_2_child_lock",
    }


async def test_a_row_left_under_the_new_unique_id_gives_way(hass):
    """A row an earlier configuration left under the target is replaced."""
    registry = er.async_get(hass)
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    entry_id = entry.entry_id
    current = registry.async_get_or_create(
        "switch", DOMAIN, f"{entry_id}_climate.trv_child_lock", config_entry=entry
    )
    registry.async_get_or_create(
        "switch", DOMAIN, f"{entry_id}_climate.radiator_child_lock", config_entry=entry
    )

    move_trv_unique_ids(
        registry, entry_id, ["climate.trv"], "climate.trv", "climate.radiator"
    )

    assert [
        (reg_entry.entity_id, reg_entry.unique_id)
        for reg_entry in registry.entities.values()
        if reg_entry.platform == DOMAIN
    ] == [(current.entity_id, f"{entry_id}_climate.radiator_child_lock")]


async def test_a_thermostat_given_a_removed_ones_id_starts_without_its_learning(
    hass, trv_group
):
    """Learned state belongs to the thermostat, not to the entity id it had."""
    set_room_sensor(hass, 19.0)
    profiles = tuple(
        replace(profile, calibration_mode=PID) for profile in GROUP_OF_THREE.profiles
    )
    entry = make_entry(replace(GROUP_OF_THREE, profiles=profiles))
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    first, removed, last = (profile.entity_id for profile in profiles)
    _learn_kp(bt, removed)
    _learn_kp(bt, first)
    heaters = entry.options["thermostat"]

    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "thermostat": [h for h in heaters if h["trv"] != removed],
        },
    )
    await hass.async_block_till_done()
    bt = await _reloaded(hass, entry, bt)
    assert bt.state_mgr is not None
    assert not [key for key in bt.state_mgr.state.pid if f":{removed}:" in key]
    assert _stored_kp(bt, first) == LEARNED_KP

    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "thermostat": heaters}
    )
    await hass.async_block_till_done()
    bt = await _reloaded(hass, entry, bt)

    assert _configured_trvs(entry) == [first, removed, last]
    assert _stored_kp(bt, removed) != LEARNED_KP
    assert _stored_kp(bt, first) == LEARNED_KP
