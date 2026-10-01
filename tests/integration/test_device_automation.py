"""The automation surface Home Assistant offers for a Better Thermostat device.

Triggers, conditions and actions are the one surface the integration never
uses itself — users build automations on it, Home Assistant calls into it, and
nothing inside this repository does. A broken entry here is therefore
invisible to every other test: the code still imports, the lists still render
in the automation editor, and the automation the user saves simply never runs.

So these tests go the way a user's automation goes: ask Home Assistant what
the device offers, put each offered entry into a real automation, and drive
the change it claims to watch.
"""

import json
import logging

from homeassistant.components import automation
from homeassistant.components.climate.const import ATTR_HVAC_ACTION, ATTR_HVAC_MODE
from homeassistant.components.device_automation import DeviceAutomationType
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_ENTITY_ID,
    CONF_TYPE,
    STATE_UNAVAILABLE,
)
from homeassistant.core import State
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_get_device_automations,
    async_mock_service,
)

from custom_components.better_thermostat.device_action import ACTION_TYPES
from custom_components.better_thermostat.device_condition import CONDITION_TYPES
from custom_components.better_thermostat.device_trigger import TRIGGER_TYPES

from .conftest import (
    DOMAIN,
    SENSOR_ID,
    TRV_ID,
    make_entry,
    setup_entry,
    wait_for,
    wait_for_startup,
)

BT_ENTITY = "climate.bt_test"

# The state change each trigger claims to watch, written as the attributes the
# thermostat itself publishes. Every key is checked against the live entity
# before it is used: a trigger that names an attribute Better Thermostat does
# not expose can only be caught if the test refuses to invent that attribute.
TRIGGER_CASES = {
    "heating_active": {ATTR_HVAC_ACTION: "heating"},
    "heating_stopped": {ATTR_HVAC_ACTION: "idle"},
    "window_opened": {"window_open": True},
    "window_closed": {"window_open": False},
    "target_temp_reached": {"current_temperature": 24.0, ATTR_TEMPERATURE: 20.0},
    "device_error": {"errors": json.dumps([TRV_ID])},
    "humidity_high": {"current_humidity": 85.0},
    "battery_low": {"batteries": json.dumps({TRV_ID: {"battery": 5}})},
    "hvac_mode_changed": {},
    "current_temperature_changed": {"current_temperature": 26.0},
    "current_humidity_changed": {"current_humidity": 70.0},
}

# What each trigger has to be sitting on before the change above is a change.
# A threshold trigger fires on the crossing, not on the value, so the ones
# whose quantity already sits on the far side have to be moved back first.
TRIGGER_PRECONDITIONS = {
    "heating_active": {ATTR_HVAC_ACTION: "idle"},
    "heating_stopped": {ATTR_HVAC_ACTION: "heating"},
    "window_closed": {"window_open": True},
    "target_temp_reached": {"current_temperature": 18.0, ATTR_TEMPERATURE: 22.0},
    "current_temperature_changed": {"current_temperature": 20.0},
    "current_humidity_changed": {"current_humidity": 50.0},
}

# Extra fields a trigger requires beyond the entry the device offers. The two
# classic value triggers carry no threshold of their own — the automation
# editor asks the user for one, and without it there is nothing to cross.
TRIGGER_EXTRA_FIELDS = {
    "hvac_mode_changed": {"to": "off"},
    "current_temperature_changed": {"above": 25.0},
    "current_humidity_changed": {"above": 65.0},
}

# Each case names the condition type, the fields the condition carries, the
# state the thermostat has to be in, and the room temperature that drives it
# there. The hvac action is driven through the room sensor rather than
# republished: the thermostat publishes its own action on every state write,
# and a write landing between a republished action and the check would put
# the real one back.
CONDITION_CASES = {
    "is_hvac_mode": ("is_hvac_mode", {ATTR_HVAC_MODE: "heat"}, "heat", None),
    "is_hvac_action_idle": ("is_hvac_action", {ATTR_HVAC_ACTION: "idle"}, None, 22.0),
    "is_hvac_action_heating": (
        "is_hvac_action",
        {ATTR_HVAC_ACTION: "heating"},
        None,
        17.0,
    ),
}


def _room_sensor(hass, value="19.0"):
    """Set the external room temperature sensor to ``value`` (°C)."""
    hass.states.async_set(SENSOR_ID, value, {"unit_of_measurement": "°C"})


async def _entry_with_device(hass):
    """Set a thermostat up and return its config entry and device id."""
    _room_sensor(hass)
    entry = make_entry()
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)
    registry_entry = er.async_get(hass).async_get(BT_ENTITY)
    assert registry_entry is not None
    assert registry_entry.device_id is not None
    return entry, registry_entry.device_id


def _offered(automations, wanted_type):
    """Return the entry Home Assistant offers for ``wanted_type``."""
    matches = [
        item
        for item in automations
        if item.get(CONF_DOMAIN) == DOMAIN and item[CONF_TYPE] == wanted_type
    ]
    assert matches, f"the device offers no {wanted_type}"
    # The offered entries carry editor metadata that is not part of the config.
    return {k: v for k, v in matches[0].items() if k != "metadata"}


def _republish(hass, **changes) -> State:
    """Republish the thermostat's own state with some attributes changed.

    Every key has to be one the entity already publishes. That is the point of
    going through the live state instead of writing a state from scratch: an
    automation entry that watches an attribute the thermostat does not have
    would otherwise be tested against an attribute this test invented.
    """
    state = hass.states.get(BT_ENTITY)
    assert state is not None
    missing = sorted(key for key in changes if key not in state.attributes)
    assert not missing, f"the thermostat publishes no {missing}"
    hass.states.async_set(BT_ENTITY, state.state, {**state.attributes, **changes})
    return state


async def _run_automation(hass, config) -> None:
    """Set one automation up and assert it survived validation."""
    assert await async_setup_component(
        hass, automation.DOMAIN, {automation.DOMAIN: [config]}
    )
    await hass.async_block_till_done()
    assert hass.states.async_entity_ids("automation"), (
        f"{config['alias']} did not survive automation setup"
    )


async def test_the_device_offers_every_declared_trigger(hass, fake_trv):
    """Home Assistant is offered each trigger type the platform declares."""
    _entry, device_id = await _entry_with_device(hass)

    offered = await async_get_device_automations(
        hass, DeviceAutomationType.TRIGGER, device_id
    )
    ours = {item[CONF_TYPE] for item in offered if item.get(CONF_DOMAIN) == DOMAIN}

    assert ours == TRIGGER_TYPES


@pytest.mark.parametrize("trigger_type", sorted(TRIGGER_CASES), ids=str)
async def test_each_trigger_fires_on_the_change_it_names(hass, fake_trv, trigger_type):
    """An automation built on each offered trigger runs when its change happens.

    This is the whole contract: the entry the device offers has to be a valid
    automation trigger, and it has to watch something the thermostat actually
    publishes. Both halves fail silently — the first at automation setup, the
    second never.
    """
    _entry, device_id = await _entry_with_device(hass)
    trigger = _offered(
        await async_get_device_automations(
            hass, DeviceAutomationType.TRIGGER, device_id
        ),
        trigger_type,
    )
    trigger.update(TRIGGER_EXTRA_FIELDS.get(trigger_type, {}))
    calls = async_mock_service(hass, "test", "automation")

    await _run_automation(
        hass,
        {
            "alias": trigger_type,
            "trigger": trigger,
            "action": {"service": "test.automation"},
        },
    )

    if trigger_type in TRIGGER_PRECONDITIONS:
        _republish(hass, **TRIGGER_PRECONDITIONS[trigger_type])
        await hass.async_block_till_done()

    if trigger_type == "hvac_mode_changed":
        # The mode is the entity state, so this one is driven through the
        # service the user would call rather than through the attributes.
        await hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {ATTR_ENTITY_ID: BT_ENTITY, ATTR_HVAC_MODE: "off"},
            blocking=True,
        )
    else:
        _republish(hass, **TRIGGER_CASES[trigger_type])

    assert await wait_for(hass, lambda: calls), f"{trigger_type} never fired"


async def test_a_trigger_that_names_only_a_device_finds_the_entity(hass, fake_trv):
    """A device-only trigger watches the thermostat on that device.

    This is the shape the bundled blueprints are written in: they let the user
    pick a device, and nothing in the blueprint knows an entity id. The
    automation editor writes the entity alongside the device, so both shapes
    reach the platform and both have to work.
    """
    _entry, device_id = await _entry_with_device(hass)
    calls = async_mock_service(hass, "test", "automation")

    await _run_automation(
        hass,
        {
            "alias": "device_only",
            "trigger": {
                "platform": "device",
                CONF_DOMAIN: DOMAIN,
                CONF_DEVICE_ID: device_id,
                CONF_TYPE: "heating_active",
            },
            "action": {"service": "test.automation"},
        },
    )

    _republish(hass, **TRIGGER_PRECONDITIONS["heating_active"])
    await hass.async_block_till_done()
    _republish(hass, **{ATTR_HVAC_ACTION: "heating"})

    assert await wait_for(hass, lambda: calls)


async def test_the_device_offers_every_declared_condition(hass, fake_trv):
    """Home Assistant is offered each condition type the platform declares."""
    _entry, device_id = await _entry_with_device(hass)

    offered = await async_get_device_automations(
        hass, DeviceAutomationType.CONDITION, device_id
    )
    ours = {item[CONF_TYPE] for item in offered if item.get(CONF_DOMAIN) == DOMAIN}

    assert ours == CONDITION_TYPES


@pytest.mark.parametrize("case", sorted(CONDITION_CASES), ids=str)
async def test_each_condition_passes_on_the_state_it_names(hass, fake_trv, case):
    """A condition built on each offered entry passes for the state it names.

    A condition that can never be true is worse than a missing one: the
    automation runs, the condition blocks it, and nothing anywhere says why.
    """
    _entry, device_id = await _entry_with_device(hass)
    condition_type, extra_fields, expected_state, room = CONDITION_CASES[case]
    condition = _offered(
        await async_get_device_automations(
            hass, DeviceAutomationType.CONDITION, device_id
        ),
        condition_type,
    )
    condition.update(extra_fields)
    calls = async_mock_service(hass, "test", "automation")

    await _run_automation(
        hass,
        {
            "alias": condition_type,
            "trigger": {"platform": "event", "event_type": "run_condition"},
            "condition": [condition],
            "action": {"service": "test.automation"},
        },
    )

    if room is not None:
        _room_sensor(hass, str(room))
        action = extra_fields[ATTR_HVAC_ACTION]
        assert await wait_for(
            hass,
            lambda: (
                hass.states.get(BT_ENTITY).attributes.get(ATTR_HVAC_ACTION) == action
            ),
        ), f"the thermostat never reported {action} at {room} °C"
    state = hass.states.get(BT_ENTITY)
    if expected_state is not None:
        assert state.state == expected_state, (
            f"the thermostat is not in {expected_state}, so this proves nothing"
        )

    hass.bus.async_fire("run_condition")
    await hass.async_block_till_done()

    assert calls, f"{condition_type} blocked the state it names"


async def test_a_condition_that_names_the_registry_id_reads_the_state(hass, fake_trv):
    """A condition naming the entity by registry id still reads its state.

    A stored automation may hold the registry id instead of the entity id —
    that is the point of the id, it survives a rename. The schema accepts both
    and ``hass.states`` accepts only one, so the registry id has to be resolved
    before the state is read, or the condition is silently always false.
    """
    _entry, device_id = await _entry_with_device(hass)
    registry_entry = er.async_get(hass).async_get(BT_ENTITY)
    condition = _offered(
        await async_get_device_automations(
            hass, DeviceAutomationType.CONDITION, device_id
        ),
        "is_hvac_mode",
    )
    condition[CONF_ENTITY_ID] = registry_entry.id
    condition[ATTR_HVAC_MODE] = "heat"
    calls = async_mock_service(hass, "test", "automation")

    await _run_automation(
        hass,
        {
            "alias": "by_registry_id",
            "trigger": {"platform": "event", "event_type": "run_condition"},
            "condition": [condition],
            "action": {"service": "test.automation"},
        },
    )
    assert hass.states.get(BT_ENTITY).state == "heat"

    hass.bus.async_fire("run_condition")
    await hass.async_block_till_done()

    assert calls, "the condition did not resolve the registry id"


async def test_the_device_offers_every_declared_action(hass, fake_trv):
    """Home Assistant is offered each action type the platform declares."""
    _entry, device_id = await _entry_with_device(hass)

    offered = await async_get_device_automations(
        hass, DeviceAutomationType.ACTION, device_id
    )
    ours = {item[CONF_TYPE] for item in offered if item.get(CONF_DOMAIN) == DOMAIN}

    assert ours == ACTION_TYPES


async def test_the_set_hvac_mode_action_reaches_the_thermostat(hass, fake_trv):
    """The offered mode action turns the thermostat off when it runs."""
    _entry, device_id = await _entry_with_device(hass)
    action = _offered(
        await async_get_device_automations(
            hass, DeviceAutomationType.ACTION, device_id
        ),
        "set_hvac_mode",
    )
    action[ATTR_HVAC_MODE] = "off"
    assert hass.states.get(BT_ENTITY).state == "heat"

    await _run_automation(
        hass,
        {
            "alias": "set_hvac_mode",
            "trigger": {"platform": "event", "event_type": "run_action"},
            "action": action,
        },
    )
    hass.bus.async_fire("run_action")

    assert await wait_for(hass, lambda: hass.states.get(BT_ENTITY).state == "off")


async def test_the_set_temperature_action_reaches_the_thermostat(hass, fake_trv):
    """The offered setpoint action moves the thermostat's target when it runs."""
    _entry, device_id = await _entry_with_device(hass)
    action = _offered(
        await async_get_device_automations(
            hass, DeviceAutomationType.ACTION, device_id
        ),
        "set_temperature",
    )
    action[ATTR_TEMPERATURE] = 23.5
    assert hass.states.get(BT_ENTITY).attributes[ATTR_TEMPERATURE] != 23.5

    await _run_automation(
        hass,
        {
            "alias": "set_temperature",
            "trigger": {"platform": "event", "event_type": "run_action"},
            "action": action,
        },
    )
    hass.bus.async_fire("run_action")

    assert await wait_for(
        hass, lambda: hass.states.get(BT_ENTITY).attributes[ATTR_TEMPERATURE] == 23.5
    )


async def test_a_trigger_on_a_device_without_a_thermostat_is_refused(
    hass, fake_trv, caplog
):
    """A device carrying no thermostat is refused by name, and watches nothing.

    Resolving the entity from the device is what makes the device-only shape
    work, and it has exactly one way to come up empty. Home Assistant leaves
    such an automation switched on either way, so the log line naming the
    device is the only thing that separates "armed" from "attached to
    nothing".
    """
    await _entry_with_device(hass)
    calls = async_mock_service(hass, "test", "automation")
    other = MockConfigEntry(domain="other_integration")
    other.add_to_hass(hass)
    stranger = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id,
        identifiers={("other_integration", "no_thermostat_here")},
    )

    assert await async_setup_component(
        hass,
        automation.DOMAIN,
        {
            automation.DOMAIN: [
                {
                    "alias": "stranger",
                    "trigger": {
                        "platform": "device",
                        CONF_DOMAIN: DOMAIN,
                        CONF_DEVICE_ID: stranger.id,
                        CONF_TYPE: "heating_active",
                    },
                    "action": {"service": "test.automation"},
                }
            ]
        },
    )
    await hass.async_block_till_done()

    assert any(
        stranger.id in record.message
        and "Better Thermostat climate entity" in record.message
        for record in caplog.records
    ), "the refusal did not name the device"

    # The thermostat that does exist changes; nothing is watching for it.
    _republish(hass, **TRIGGER_PRECONDITIONS["heating_active"])
    await hass.async_block_till_done()
    _republish(hass, **{ATTR_HVAC_ACTION: "heating"})
    await hass.async_block_till_done()

    assert not calls


async def _automation_on(hass, device_id, trigger_type, **extra):
    """Arm an automation on the offered ``trigger_type`` and return its calls.

    Each call carries the states the trigger fired between, so a test can
    tell its own change from a state the thermostat wrote on its own.
    """
    trigger = _offered(
        await async_get_device_automations(
            hass, DeviceAutomationType.TRIGGER, device_id
        ),
        trigger_type,
    )
    trigger.update(extra)
    calls = async_mock_service(hass, "test", "automation")
    assert await async_setup_component(
        hass,
        automation.DOMAIN,
        {
            automation.DOMAIN: [
                {
                    "alias": trigger_type,
                    "trigger": trigger,
                    "action": {
                        "service": "test.automation",
                        "data": {
                            "from_state": "{{ trigger.from_state.state }}",
                            "to_state": "{{ trigger.to_state.state }}",
                            "current": (
                                "{{ trigger.to_state.attributes.current_temperature }}"
                            ),
                            "target": "{{ trigger.to_state.attributes.temperature }}",
                        },
                    },
                }
            ]
        },
    )
    await hass.async_block_till_done()
    return calls


@pytest.mark.parametrize(
    ("current", "target", "fires"),
    [(21.9, 22.0, False), (22.0, 22.0, True), (22.5, 22.0, True)],
    ids=["below", "equal", "above"],
)
async def test_target_reached_fires_once_the_room_is_at_the_target(
    hass, fake_trv, current, target, fires
):
    """``target_temp_reached`` fires when the room temperature is at or above target."""
    _entry, device_id = await _entry_with_device(hass)
    calls = await _automation_on(hass, device_id, "target_temp_reached")
    _republish(hass, current_temperature=18.0, **{ATTR_TEMPERATURE: target})
    await hass.async_block_till_done()

    _republish(hass, current_temperature=current, **{ATTR_TEMPERATURE: target})
    await hass.async_block_till_done()

    fired = [
        call
        for call in calls
        if call.data["current"] == current and call.data["target"] == target
    ]
    assert bool(fired) is fires


@pytest.mark.parametrize(
    ("trigger_type", "before"),
    [
        ("window_closed", {"window_open": True}),
        ("heating_stopped", {ATTR_HVAC_ACTION: "heating"}),
        ("target_temp_reached", {"current_temperature": 18.0, ATTR_TEMPERATURE: 22.0}),
    ],
)
async def test_a_thermostat_going_unavailable_fires_no_trigger(
    hass, fake_trv, trigger_type, before
):
    """A thermostat that drops out has not closed a window or stopped heating."""
    _entry, device_id = await _entry_with_device(hass)
    calls = await _automation_on(hass, device_id, trigger_type)
    _republish(hass, **before)
    await hass.async_block_till_done()

    hass.states.async_set(BT_ENTITY, STATE_UNAVAILABLE, {})
    await hass.async_block_till_done()

    assert not [c for c in calls if c.data["to_state"] == STATE_UNAVAILABLE]


async def test_a_mode_trigger_without_a_mode_is_refused_by_name(hass, fake_trv, caplog):
    """``hvac_mode_changed`` without ``to`` logs what is missing and watches nothing.

    The automation's other triggers keep working.
    """
    _entry, device_id = await _entry_with_device(hass)
    trigger = _offered(
        await async_get_device_automations(
            hass, DeviceAutomationType.TRIGGER, device_id
        ),
        "hvac_mode_changed",
    )
    calls = async_mock_service(hass, "test", "automation")
    hass.states.async_set("input_boolean.other", "off")
    assert await async_setup_component(
        hass,
        automation.DOMAIN,
        {
            automation.DOMAIN: [
                {
                    "alias": "mixed",
                    "trigger": [
                        trigger,
                        {
                            "platform": "state",
                            "entity_id": "input_boolean.other",
                            "to": "on",
                        },
                    ],
                    "action": {"service": "test.automation"},
                }
            ]
        },
    )
    await hass.async_block_till_done()

    assert "KeyError" not in caplog.text
    assert any(
        record.levelname == "ERROR"
        and "hvac_mode_changed" in record.getMessage()
        and "'to'" in record.getMessage()
        and record.exc_info is None
        for record in caplog.records
    ), caplog.text

    await hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {ATTR_ENTITY_ID: BT_ENTITY, ATTR_HVAC_MODE: "off"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert not calls

    hass.states.async_set("input_boolean.other", "on")
    await hass.async_block_till_done()
    assert calls


@pytest.mark.parametrize(
    ("trigger_type", "before"),
    [
        ("window_opened", {"window_open": True}),
        ("window_closed", {"window_open": False}),
        ("heating_active", {ATTR_HVAC_ACTION: "heating"}),
        ("heating_stopped", {ATTR_HVAC_ACTION: "idle"}),
    ],
)
async def test_a_thermostat_coming_back_unchanged_fires_no_trigger(
    hass, fake_trv, trigger_type, before
):
    """A thermostat that drops out and returns as it was has changed nothing."""
    _entry, device_id = await _entry_with_device(hass)
    state = _republish(hass, **before)
    await hass.async_block_till_done()
    calls = await _automation_on(hass, device_id, trigger_type)
    attributes = {**state.attributes, **before}

    hass.states.async_set(BT_ENTITY, STATE_UNAVAILABLE, {})
    await hass.async_block_till_done()
    hass.states.async_set(BT_ENTITY, state.state, attributes)
    await hass.async_block_till_done()

    assert not [c for c in calls if c.data["from_state"] == STATE_UNAVAILABLE]


@pytest.mark.parametrize(
    ("trigger_type", "attribute"),
    [
        ("current_temperature_changed", "current_temperature"),
        ("current_humidity_changed", "current_humidity"),
    ],
)
@pytest.mark.parametrize("missing", ["absent", "none"])
async def test_a_value_trigger_on_a_missing_value_stays_quiet(
    hass, fake_trv, caplog, trigger_type, attribute, missing
):
    """A value trigger whose value is missing neither fires nor logs a warning."""
    _entry, device_id = await _entry_with_device(hass)
    calls = await _automation_on(
        hass, device_id, trigger_type, **TRIGGER_EXTRA_FIELDS[trigger_type]
    )
    state = hass.states.get(BT_ENTITY)
    attributes = {k: v for k, v in state.attributes.items() if k != attribute}
    if missing == "none":
        attributes[attribute] = None
    caplog.clear()

    for step in range(3):
        hass.states.async_set(BT_ENTITY, state.state, {**attributes, "step": step})
        await hass.async_block_till_done()

    noise = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert noise == []
    assert not calls
