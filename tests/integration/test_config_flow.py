"""Config and options flow, driven through Home Assistant's flow helpers.

The unit suite calls the flow handlers directly and hands them mocked
registries. That skips what Home Assistant does between two steps: it
validates a submission against the schema the step published, it decides
which keys reach the handler when a field is left empty, and it sets up the
entry the flow produced. Two things only exist in that gap: the model a
swapped thermostat has to be resolved to from scratch, and the key an
emptied entity selector does *not* send.

So these tests submit forms instead of calling handlers, and end on the
entry: what was stored, and what the thermostat that came up from it drives.
"""

import asyncio

from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import voluptuous as vol

from custom_components.better_thermostat import RELOAD_LOCKS
from custom_components.better_thermostat.utils.const import (
    CONF_CALIBRATION,
    CONF_CHILD_LOCK,
    CONF_COOLER,
    CONF_HEATER,
    CONF_MODEL,
    CONF_OUTDOOR_SENSOR,
    CONF_PRESETS,
    CONF_SENSOR,
    CONF_SENSOR_WINDOW,
    CONF_TARGET_TEMP_MAX,
    CONF_TARGET_TEMP_MIN,
    TARGET_TEMP_BOUND_AUTO,
    CalibrationType,
)
from custom_components.better_thermostat.utils.preset_manager import (
    DEFAULT_ENABLED_PRESETS,
)

from .conftest import (
    BT_ENTITY,
    DOMAIN,
    SENSOR_ID,
    WINDOW_ID,
    assert_profile_adopted,
    build_devices,
    click_through_the_options,
    counting_reloads,
    form_default,
    make_entry,
    profile_id,
    set_room_sensor,
    setup_entry,
    wait_for_startup,
)
from .device_profiles import (
    COOLER_ID,
    GENERIC_HEAT_TRV,
    MQTT_OFFSET_TRV,
    ROOM_AC_COOLER,
    SPARE_HEAT_TRV,
    SPARE_TRV_ID,
    TRV_ID,
    DeviceProfile,
    OffsetChannel,
)

ENTRY_NAME = "BT Test"
OUTDOOR_ID = "sensor.outdoor_temperature"

# How long a reload that is not waiting on another entry may take. Only ever
# paid in full when the entries do share a lock, where the wait has no end.
RELOAD_TIMEOUT_S = 10


def _marker(form, key: str) -> vol.Marker:
    """Return the voluptuous marker a step published for ``key``."""
    for marker in form["data_schema"].schema:
        if marker == key:
            return marker
    raise AssertionError(f"step {form['step_id']} publishes no field {key!r}")


def _field_options(form, key: str) -> list[str]:
    """Return the options a step's select field offers for ``key``."""
    return list(form["data_schema"].schema[_marker(form, key)].config["options"])


def _expected_calibration_options(profile: DeviceProfile) -> list[str]:
    """Return the calibration strategies this device's channels allow.

    The setpoint is always writable, so target-temperature calibration is
    offered for every device; the local-offset strategy needs a calibration
    channel the flow could discover behind the entity.
    """
    options = [CalibrationType.TARGET_TEMP_BASED]
    if profile.offset_channel is OffsetChannel.NUMBER_ENTITY:
        options.append(CalibrationType.LOCAL_BASED)
    return options


def _expected_calibration(profile: DeviceProfile) -> str:
    """Return the calibration strategy a device's channels make the default."""
    return _expected_calibration_options(profile)[-1]


def _user_step_input(thermostat: str, **overrides) -> dict:
    """Return a submission for the user step, naming the entities it wires."""
    return {
        "name": ENTRY_NAME,
        CONF_HEATER: [thermostat],
        CONF_SENSOR: SENSOR_ID,
    } | overrides


async def _run_create_flow(hass, user_input, advanced=None):
    """Run the create flow to its end and return the finished flow result.

    The three steps are the whole flow: the user step names the entities, the
    advanced step settles the per-device options, and the confirm step writes
    the entry. Every step submits through Home Assistant, so a field the step
    did not publish is rejected here rather than silently stored.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input
    )
    assert result["type"] is FlowResultType.FORM, result
    assert result["step_id"] == "advanced", result
    advanced_form = result
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], advanced or {}
    )
    assert result["step_id"] == "confirm", result
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    await hass.async_block_till_done()
    return advanced_form, result


async def _run_options_flow(hass, entry, user_input, advanced=None):
    """Re-run the options flow over an existing entry and return its forms.

    Returns the advanced form and the final result, like the create flow, so
    a test can assert on what the form pre-filled as well as on what was
    written. The entry reloads on the update, so the caller waits for the
    thermostat that comes back up rather than reusing the old one.
    """
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "user"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input
    )
    assert result["type"] is FlowResultType.FORM, result
    assert result["step_id"] == "advanced", result
    advanced_form = result
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], advanced or {}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    await hass.async_block_till_done()
    return advanced_form, result


def _only_entry(hass):
    """Return the single Better Thermostat entry this test created."""
    (entry,) = hass.config_entries.async_entries(DOMAIN)
    return entry


def _entry_named(hass, name: str):
    """Return the Better Thermostat entry a flow created under ``name``."""
    (entry,) = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.data["name"] == name
    ]
    return entry


def _stored_trv(entry, index: int = 0) -> dict:
    """Return one device bundle out of an entry's stored thermostat list."""
    return entry.data[CONF_HEATER][index]


@pytest.mark.parametrize(
    "fake_trv", [GENERIC_HEAT_TRV, MQTT_OFFSET_TRV], indirect=True, ids=profile_id
)
async def test_create_flow_offers_the_calibration_the_device_can_take(hass, fake_trv):
    """The advanced step offers the strategies this device's channels support.

    Calibration is the one option in the flow that is not a preference: the
    strategies a device can take follow from the channels found behind its
    entity, and a device without a calibration channel must not be offered
    one. The default is the best of what was found.
    """
    profile = fake_trv.profile
    set_room_sensor(hass, 19.0)

    advanced_form, _ = await _run_create_flow(hass, _user_step_input(profile.entity_id))

    assert _field_options(advanced_form, CONF_CALIBRATION) == (
        _expected_calibration_options(profile)
    )
    assert form_default(advanced_form, CONF_CALIBRATION) == (
        _expected_calibration(profile)
    )


@pytest.mark.parametrize(
    "fake_trv", [GENERIC_HEAT_TRV, MQTT_OFFSET_TRV], indirect=True, ids=profile_id
)
async def test_create_flow_ends_in_a_thermostat_driving_the_device(hass, fake_trv):
    """A flow run start to finish leaves a loaded entry and a live thermostat.

    The flow's product is not the dict it stores but the entry that comes up
    from it, so this ends where the lifecycle tests begin: an entity that has
    read this device's capabilities.
    """
    profile = fake_trv.profile
    set_room_sensor(hass, 19.0)

    _, result = await _run_create_flow(hass, _user_step_input(profile.entity_id))

    entry = _only_entry(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert result["title"] == ENTRY_NAME
    assert _stored_trv(entry)["trv"] == profile.entity_id
    assert _stored_trv(entry)["advanced"][CONF_CALIBRATION] == (
        _expected_calibration(profile)
    )

    bt = await wait_for_startup(hass, entry)
    assert hass.states.get(BT_ENTITY) is not None
    assert_profile_adopted(bt, profile)


async def test_create_flow_stores_the_generic_model_for_a_device_less_thermostat(hass):
    """A thermostat with no device behind it resolves to the generic model.

    Model resolution reads the device registry, and a device-less entity —
    a ``generic_thermostat`` helper, say — leaves that lookup empty. What is
    stored then decides which quirk module the entry loads later, so the
    fallback is a stored value, not an internal detail.
    """
    set_room_sensor(hass, 19.0)
    (trv,) = await build_devices(hass, GENERIC_HEAT_TRV)

    await _run_create_flow(hass, _user_step_input(trv.entity_id))

    entry = _only_entry(hass)
    assert _stored_trv(entry)[CONF_MODEL] == "generic"
    assert _stored_trv(entry)["integration"] == "generic_thermostat"


async def test_options_flow_swaps_the_thermostat_to_a_device_less_entity(hass):
    """Swapping onto an entity the entry never saw resolves its model afresh.

    The new thermostat is not in the stored bundle, so the options flow takes
    its "new device" branch and resolves integration and model from scratch —
    against a registry that knows nothing about a device-less entity. The
    flow has to come out the other side with a model, and the entry that
    reloads has to drive the new device.
    """
    set_room_sensor(hass, 19.0)
    trv, spare = await build_devices(hass, GENERIC_HEAT_TRV, SPARE_HEAT_TRV)
    await _run_create_flow(hass, _user_step_input(trv.entity_id))
    entry = _only_entry(hass)
    await wait_for_startup(hass, entry)

    await _run_options_flow(hass, entry, _user_step_input(SPARE_TRV_ID))

    assert _stored_trv(entry)["trv"] == SPARE_TRV_ID
    assert _stored_trv(entry)[CONF_MODEL] == "generic"
    assert _stored_trv(entry)["integration"] == "generic_thermostat"

    bt = await wait_for_startup(hass, entry)
    assert list(bt.real_trvs) == [SPARE_TRV_ID]
    assert_profile_adopted(bt, SPARE_HEAT_TRV)


async def test_options_flow_keeps_the_settings_of_a_thermostat_left_alone(hass):
    """A device that stays in the entry keeps the options it was given.

    The swap branch and this one sit in the same loop, one per device in the
    submission. A device already in the entry has to be carried over rather
    than resolved again, or every unrelated edit silently resets the options
    of every device the entry controls.
    """
    set_room_sensor(hass, 19.0)
    (trv,) = await build_devices(hass, GENERIC_HEAT_TRV)
    await _run_create_flow(
        hass, _user_step_input(trv.entity_id), advanced={CONF_CHILD_LOCK: True}
    )
    entry = _only_entry(hass)
    await wait_for_startup(hass, entry)
    assert _stored_trv(entry)["advanced"][CONF_CHILD_LOCK] is True

    advanced_form, _ = await _run_options_flow(
        hass, entry, _user_step_input(trv.entity_id, name="Renamed Room")
    )

    assert form_default(advanced_form, CONF_CHILD_LOCK) is True
    assert _stored_trv(entry)["advanced"][CONF_CHILD_LOCK] is True
    assert entry.data["name"] == "Renamed Room"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (CONF_COOLER, COOLER_ID),
        (CONF_SENSOR_WINDOW, WINDOW_ID),
        (CONF_OUTDOOR_SENSOR, OUTDOOR_ID),
    ],
)
async def test_options_flow_clears_an_optional_entity_field(hass, field, value):
    """An optional entity, once set, can be taken back out of the entry.

    An emptied entity selector sends no key at all, so "keep what is stored"
    and "the user cleared it" arrive as the same submission. The flow has to
    read the absent key as cleared, which is only observable from here: a
    handler called directly is handed whatever the test puts in the dict.
    """
    set_room_sensor(hass, 19.0)
    hass.states.async_set(WINDOW_ID, "off")
    hass.states.async_set(OUTDOOR_ID, "8.0", {"unit_of_measurement": "°C"})
    await build_devices(hass, GENERIC_HEAT_TRV, ROOM_AC_COOLER)
    await _run_create_flow(hass, _user_step_input(TRV_ID, **{field: value}))
    entry = _only_entry(hass)
    await wait_for_startup(hass, entry)
    assert entry.data[field] == value

    # The submission the user sends after emptying the field: every other
    # field unchanged, and this one simply absent.
    await _run_options_flow(hass, entry, _user_step_input(TRV_ID))

    assert entry.data[field] is None


async def test_clearing_the_cooler_stops_the_thermostat_from_driving_it(hass):
    """A cleared cooler is gone from the thermostat, not just from the entry.

    Storing ``None`` is half the job: the entry reloads on every options
    change, and what the user asked for is that the cooler stops being
    driven. This asserts on the thermostat that came back up.
    """
    set_room_sensor(hass, 19.0)
    await build_devices(hass, GENERIC_HEAT_TRV, ROOM_AC_COOLER)
    await _run_create_flow(hass, _user_step_input(TRV_ID, **{CONF_COOLER: COOLER_ID}))
    entry = _only_entry(hass)
    bt = await wait_for_startup(hass, entry)
    assert bt.cooler_entity_id == COOLER_ID
    assert COOLER_ID in bt.all_entities

    await _run_options_flow(hass, entry, _user_step_input(TRV_ID))

    bt = await wait_for_startup(hass, entry)
    assert bt.cooler_entity_id is None
    assert COOLER_ID not in bt.all_entities


async def test_one_thermostat_reload_does_not_wait_on_another(hass):
    """Two entries reload independently of each other.

    Every options change reloads the entry it belongs to, and a reload runs
    the full startup, which waits on the devices. Serializing that across
    entries would let one thermostat whose device is unreachable hold up the
    settings of every other thermostat in the instance.
    """
    set_room_sensor(hass, 19.0)
    trv, spare = await build_devices(hass, GENERIC_HEAT_TRV, SPARE_HEAT_TRV)
    await _run_create_flow(hass, _user_step_input(trv.entity_id))
    await _run_create_flow(hass, _user_step_input(spare.entity_id, name="Other Room"))
    holding = _entry_named(hass, ENTRY_NAME)
    waiting = _entry_named(hass, "Other Room")
    await wait_for_startup(hass, holding)
    await wait_for_startup(hass, waiting)

    # Reloading is what creates the lock, and only a submission that changes
    # something reloads, so the priming pass renames the first entry before its
    # reload lock is taken and held.
    await _run_options_flow(
        hass, holding, _user_step_input(trv.entity_id, name="Primed Room")
    )
    before = hass.data[DOMAIN][waiting.entry_id]["climate"]
    async with hass.data[RELOAD_LOCKS][holding.entry_id]:
        async with asyncio.timeout(RELOAD_TIMEOUT_S):
            await _run_options_flow(
                hass, waiting, _user_step_input(spare.entity_id, name="Renamed Room")
            )
        # The stored name is written before the reload is even scheduled, so
        # the thermostat is what says whether the reload ran: a reloaded
        # entry has built a new one.
        after = await wait_for_startup(hass, waiting)

    assert after is not before
    assert waiting.data["name"] == "Renamed Room"


async def test_options_flow_offers_the_presets_an_untouched_entry_runs_on(hass):
    """An entry carrying no preset list keeps its presets through the form.

    A preset list that was never written is not an empty one: the thermostat
    comes up on the full default set. So that is the set the update form has to
    pre-fill, or a pass through the form that changes nothing submits a
    narrower list and takes every other preset away.
    """
    set_room_sensor(hass, 19.0)
    (trv,) = await build_devices(hass, GENERIC_HEAT_TRV)
    data = dict(make_entry(GENERIC_HEAT_TRV).data)
    data.pop(CONF_PRESETS, None)
    entry = MockConfigEntry(domain=DOMAIN, version=18, data=data, title=data["name"])
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)
    running_on = set(hass.states.get(BT_ENTITY).attributes["preset_modes"])

    form = await hass.config_entries.options.async_init(entry.entry_id)
    offered = form_default(form, CONF_PRESETS)
    hass.config_entries.options.async_abort(form["flow_id"])
    await _run_options_flow(
        hass, entry, _user_step_input(trv.entity_id, **{CONF_PRESETS: offered})
    )
    await wait_for_startup(hass, entry)

    assert set(offered) == set(DEFAULT_ENABLED_PRESETS)
    assert set(hass.states.get(BT_ENTITY).attributes["preset_modes"]) == running_on


async def test_one_options_change_reloads_the_entry_once(hass, fake_trv):
    """What the user changes once costs one restart of the thermostat.

    Writing the entry is what reloads it, so a flow that writes the same
    configuration to more than one place reloads more than once — and the
    second reload arrives while the first one's startup is still running,
    before it has restored what the thermostat was running on.
    """
    set_room_sensor(hass, 19.0)
    entry = make_entry(GENERIC_HEAT_TRV)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)

    async with counting_reloads(hass, entry) as reloads:
        await click_through_the_options(hass, entry, name="Renamed Room")
    await wait_for_startup(hass, entry)

    assert entry.data["name"] == "Renamed Room"
    assert len(reloads) == 1


async def test_a_settled_options_pass_that_changes_nothing_leaves_the_entry_alone(
    hass, fake_trv
):
    """Opening the settings and closing them again does not restart anything.

    The first pass is not necessarily a no-op: it settles keys the stored entry
    never carried, and that is a real change. Once it has, a pass that accepts
    every offered value writes nothing — and an entry that is not written to is
    not taken down.
    """
    set_room_sensor(hass, 19.0)
    entry = make_entry(GENERIC_HEAT_TRV)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)
    await click_through_the_options(hass, entry)
    before = await wait_for_startup(hass, entry)

    async with counting_reloads(hass, entry) as reloads:
        await click_through_the_options(hass, entry)

    assert reloads == []
    assert hass.data[DOMAIN][entry.entry_id]["climate"] is before


async def test_a_configured_range_holds_the_thermostat_below_what_its_device_allows(
    hass, fake_trv
):
    """A range set in the flow reaches the thermostat the entry brings up.

    The device offers 5 to 30 degrees, and the point of configuring a range is
    to hand the room a narrower one than that. Storing the two bounds is only
    half of it: what the user sees is the span the climate entity publishes,
    so this ends on the entity's own limits.
    """
    set_room_sensor(hass, 19.0)

    await _run_create_flow(
        hass,
        _user_step_input(
            TRV_ID,
            **{CONF_TARGET_TEMP_MIN: "min_max_16", CONF_TARGET_TEMP_MAX: "min_max_24"},
        ),
    )

    entry = _only_entry(hass)
    assert entry.data[CONF_TARGET_TEMP_MIN] == "16.0"
    assert entry.data[CONF_TARGET_TEMP_MAX] == "24.0"

    bt = await wait_for_startup(hass, entry)
    assert (bt.min_temp, bt.max_temp) == (16.0, 24.0)
    state = hass.states.get(BT_ENTITY)
    assert (state.attributes["min_temp"], state.attributes["max_temp"]) == (16.0, 24.0)


async def test_the_options_form_offers_the_range_the_entry_runs_on(hass, fake_trv):
    """Reopening the settings shows the bounds the entry was configured with.

    The form speaks in selector tokens while the entry stores degrees, so a
    pre-fill that does not translate back sends the user's own configuration
    to the handler as "auto" the next time they touch anything else.
    """
    set_room_sensor(hass, 19.0)
    await _run_create_flow(
        hass, _user_step_input(TRV_ID, **{CONF_TARGET_TEMP_MIN: "min_max_16"})
    )
    entry = _only_entry(hass)
    await wait_for_startup(hass, entry)

    form = await hass.config_entries.options.async_init(entry.entry_id)
    offered = (
        form_default(form, CONF_TARGET_TEMP_MIN),
        form_default(form, CONF_TARGET_TEMP_MAX),
    )
    hass.config_entries.options.async_abort(form["flow_id"])
    await click_through_the_options(hass, entry)
    await wait_for_startup(hass, entry)

    assert offered == ("min_max_16", "auto")
    assert entry.data[CONF_TARGET_TEMP_MIN] == "16.0"


@pytest.mark.parametrize("flow", ["create", "options"])
async def test_a_minimum_above_the_maximum_is_sent_back_to_the_user(
    hass, fake_trv, flow
):
    """An inverted range does not reach the entry; the form comes back.

    A minimum above the maximum leaves no temperature the user could ask for,
    so both flows have to stop on their first step. The redisplayed form still
    has to carry the thermostats that were picked, or correcting the range
    means selecting every device again.
    """
    set_room_sensor(hass, 19.0)
    inverted = _user_step_input(
        TRV_ID,
        **{CONF_TARGET_TEMP_MIN: "min_max_25", CONF_TARGET_TEMP_MAX: "min_max_20"},
    )

    if flow == "create":
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], inverted
        )
    else:
        await _run_create_flow(hass, _user_step_input(TRV_ID))
        entry = _only_entry(hass)
        await wait_for_startup(hass, entry)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], inverted
        )
        assert entry.data[CONF_TARGET_TEMP_MIN] == TARGET_TEMP_BOUND_AUTO

    assert result["type"] is FlowResultType.FORM, result
    assert result["step_id"] == "user", result
    assert result["errors"] == {CONF_TARGET_TEMP_MIN: "target_temp_min_above_max"}
    assert form_default(result, CONF_HEATER) == [TRV_ID]
    assert form_default(result, CONF_TARGET_TEMP_MIN) == "min_max_25"


@pytest.mark.parametrize("flow", ["create", "options"])
async def test_a_submission_without_a_thermostat_is_sent_back_to_the_user(
    hass, fake_trv, flow
):
    """Emptying the thermostat field returns the form instead of failing.

    An entity selector accepts an empty list, and there is nothing for Better
    Thermostat to control without a thermostat, so the step has to say so. The
    options flow reaches the same place from the other side: the devices it
    was given resolved to no bundle at all.
    """
    set_room_sensor(hass, 19.0)
    without_a_thermostat = _user_step_input(TRV_ID) | {CONF_HEATER: []}

    if flow == "create":
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], without_a_thermostat
        )
    else:
        await _run_create_flow(hass, _user_step_input(TRV_ID))
        entry = _only_entry(hass)
        await wait_for_startup(hass, entry)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], without_a_thermostat
        )
        assert _stored_trv(entry)["trv"] == TRV_ID

    assert result["type"] is FlowResultType.FORM, result
    assert result["step_id"] == "user", result
    assert result["errors"] == {CONF_HEATER: "no_heater"}
