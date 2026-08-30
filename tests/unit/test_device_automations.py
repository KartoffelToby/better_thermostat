"""Tests for the device automation modules (trigger, condition, action).

Each ``async_get_*`` function enumerates the entities of a device and must
pick up exactly the Better Thermostat climate entity: an entity created by
this integration (``entry.platform``) in the climate domain
(``entry.domain``). Entities from other integrations on the same device and
non-climate entities from this integration must be skipped.

The tests use the real ``hass`` fixture with the real device and entity
registries so the functions under test run against genuine
``RegistryEntry`` objects.
"""

from __future__ import annotations

from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_ENTITY_ID, CONF_TYPE
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.condition import async_numeric_state
from homeassistant.helpers.template import Template
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.better_thermostat import DOMAIN
from custom_components.better_thermostat.device_action import (
    ACTION_TYPES,
    async_get_actions,
)
from custom_components.better_thermostat.device_condition import (
    CONDITION_TYPES,
    async_get_conditions,
)
from custom_components.better_thermostat.device_trigger import (
    CLASSIC_VALUE_TEMPLATES,
    TRIGGER_TYPES,
    async_get_triggers,
)
from custom_components.better_thermostat.utils.const import CONF_HUMIDITY


def _create_device(
    hass: HomeAssistant, *, with_humidity: bool = True
) -> dr.DeviceEntry:
    """Register a device attached to a mock config entry.

    Parameters
    ----------
    hass : HomeAssistant
        The Home Assistant test instance.
    with_humidity : bool
        Whether the entry names a humidity sensor. The two humidity triggers
        are only offered when it does, so a device built without one is how
        that half of the listing is exercised.

    Returns
    -------
    dr.DeviceEntry
        The registered device entry.
    """
    data = {CONF_HUMIDITY: "sensor.room_humidity"} if with_humidity else {}
    config_entry = MockConfigEntry(domain=DOMAIN, data=data)
    config_entry.add_to_hass(hass)
    device_registry = dr.async_get(hass)
    return device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id, identifiers={(DOMAIN, "test_device")}
    )


def _register_entity(
    hass: HomeAssistant,
    device: dr.DeviceEntry,
    *,
    domain: str = "climate",
    platform: str = DOMAIN,
    unique_id: str = "bt_unique",
) -> er.RegistryEntry:
    """Register an entity on a device in the real entity registry.

    Parameters
    ----------
    hass : HomeAssistant
        The Home Assistant test instance.
    device : dr.DeviceEntry
        The device the entity belongs to.
    domain : str
        Entity-id domain of the entity (e.g. ``climate``, ``sensor``).
    platform : str
        Integration domain that created the entity.
    unique_id : str
        Unique id of the entity within its platform.

    Returns
    -------
    er.RegistryEntry
        The registered entity entry.
    """
    entity_registry = er.async_get(hass)
    return entity_registry.async_get_or_create(
        domain,
        platform,
        unique_id,
        device_id=device.id,
        config_entry=hass.config_entries.async_get_entry(
            next(iter(device.config_entries))
        ),
    )


async def test_get_triggers_lists_all_types_for_bt_climate_entity(
    hass: HomeAssistant,
) -> None:
    """A BT climate entity on the device yields the full trigger list."""
    device = _create_device(hass)
    entity = _register_entity(hass, device)
    hass.states.async_set(entity.entity_id, "heat")

    triggers = await async_get_triggers(hass, device.id)

    assert {trigger[CONF_TYPE] for trigger in triggers} == TRIGGER_TYPES
    for trigger in triggers:
        assert trigger[CONF_DOMAIN] == DOMAIN
        assert trigger[CONF_DEVICE_ID] == device.id
        assert trigger[CONF_ENTITY_ID] == entity.entity_id


async def test_get_triggers_omits_the_humidity_pair_without_that_sensor(
    hass: HomeAssistant,
) -> None:
    """A thermostat with no humidity sensor publishes no humidity to watch.

    Offering the two triggers anyway gives the user an automation that
    attaches and then never fires, which reads as a broken automation rather
    than a missing sensor.
    """
    device = _create_device(hass, with_humidity=False)
    entity = _register_entity(hass, device)
    hass.states.async_set(entity.entity_id, "heat")

    triggers = await async_get_triggers(hass, device.id)

    assert {trigger[CONF_TYPE] for trigger in triggers} == TRIGGER_TYPES - {
        "humidity_high",
        "current_humidity_changed",
    }


@pytest.mark.parametrize("trigger_type", sorted(CLASSIC_VALUE_TEMPLATES), ids=str)
async def test_a_value_trigger_stays_quiet_once_its_sensor_is_gone(
    hass: HomeAssistant, trigger_type: str
) -> None:
    """A lost sensor makes the trigger not fire, not report a broken automation.

    Home Assistant drops these attributes from the state while they hold no
    value, so a sensor that goes unavailable takes the quantity the automation
    watches with it. The trigger has to read that as "nothing to compare",
    because the alternative is a conversion error logged on every single state
    change for as long as the sensor stays away.
    """
    template = Template(CLASSIC_VALUE_TEMPLATES[trigger_type], hass)
    without_the_attribute = State("climate.bt", "heat", {})

    assert (
        async_numeric_state(
            hass, without_the_attribute, above=25.0, value_template=template
        )
        is False
    )


async def test_get_triggers_skips_entity_without_state(hass: HomeAssistant) -> None:
    """A registered BT climate entity without a state yields no triggers."""
    device = _create_device(hass)
    _register_entity(hass, device)

    assert await async_get_triggers(hass, device.id) == []


async def test_get_triggers_skips_foreign_and_non_climate_entities(
    hass: HomeAssistant,
) -> None:
    """Foreign-platform and non-climate entities yield no triggers."""
    device = _create_device(hass)
    foreign = _register_entity(hass, device, platform="demo", unique_id="foreign")
    sensor = _register_entity(hass, device, domain="sensor", unique_id="bt_sensor")
    hass.states.async_set(foreign.entity_id, "heat")
    hass.states.async_set(sensor.entity_id, "21.0")

    assert await async_get_triggers(hass, device.id) == []


async def test_get_conditions_lists_all_types_for_bt_climate_entity(
    hass: HomeAssistant,
) -> None:
    """A BT climate entity on the device yields the full condition list."""
    device = _create_device(hass)
    entity = _register_entity(hass, device)

    conditions = await async_get_conditions(hass, device.id)

    assert {condition[CONF_TYPE] for condition in conditions} == CONDITION_TYPES
    for condition in conditions:
        assert condition[CONF_DOMAIN] == DOMAIN
        assert condition[CONF_DEVICE_ID] == device.id
        assert condition[CONF_ENTITY_ID] == entity.entity_id


async def test_get_conditions_skips_foreign_and_non_climate_entities(
    hass: HomeAssistant,
) -> None:
    """Foreign-platform and non-climate entities yield no conditions."""
    device = _create_device(hass)
    _register_entity(hass, device, platform="demo", unique_id="foreign")
    _register_entity(hass, device, domain="switch", unique_id="bt_switch")

    assert await async_get_conditions(hass, device.id) == []


async def test_get_actions_lists_all_types_for_bt_climate_entity(
    hass: HomeAssistant,
) -> None:
    """A BT climate entity on the device yields the full action list."""
    device = _create_device(hass)
    entity = _register_entity(hass, device)

    actions = await async_get_actions(hass, device.id)

    assert {action[CONF_TYPE] for action in actions} == ACTION_TYPES
    for action in actions:
        assert action[CONF_DOMAIN] == DOMAIN
        assert action[CONF_DEVICE_ID] == device.id
        assert action[CONF_ENTITY_ID] == entity.entity_id


async def test_get_actions_skips_foreign_and_non_climate_entities(
    hass: HomeAssistant,
) -> None:
    """Foreign-platform and non-climate entities yield no actions."""
    device = _create_device(hass)
    _register_entity(hass, device, platform="demo", unique_id="foreign")
    _register_entity(hass, device, domain="number", unique_id="bt_number")

    assert await async_get_actions(hass, device.id) == []
