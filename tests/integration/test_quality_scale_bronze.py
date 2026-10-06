"""The Bronze rules of the Integration Quality Scale, held on a running instance.

Each test carries the rule it holds as a ``quality_rule`` marker;
``quality_scale.yaml`` records the rule's status and ``tests/quality_scale.py``
says how the two work together.
"""

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import async_get_platforms
from homeassistant.setup import async_setup_component
import pytest

from custom_components.better_thermostat import BetterThermostatData
from custom_components.better_thermostat.utils.const import (
    CONF_HEATER,
    CONF_SENSOR,
    SERVICE_RESET_HEATING_POWER,
    SERVICE_RESET_PID_LEARNINGS,
    SERVICE_RUN_VALVE_MAINTENANCE,
)
from custom_components.better_thermostat.utils.helpers import entry_settings

from .conftest import (
    DOMAIN,
    SENSOR_ID,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for_startup,
)


async def _started_entry(hass, fake_trv):
    """Set up one entry for ``fake_trv`` and return it once it has started."""
    set_room_sensor(hass, 19.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)
    return entry


def _entities(hass):
    """Return every entity the integration's platforms hold."""
    return [
        entity
        for platform in async_get_platforms(hass, DOMAIN)
        for entity in platform.entities.values()
    ]


@pytest.mark.quality_rule("action-setup")
async def test_the_actions_exist_before_any_entry_is_set_up(hass):
    """An automation calling an action validates while no thermostat is loaded."""
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    assert not hass.config_entries.async_entries(DOMAIN)
    for name in (
        SERVICE_RESET_HEATING_POWER,
        SERVICE_RESET_PID_LEARNINGS,
        SERVICE_RUN_VALVE_MAINTENANCE,
    ):
        assert hass.services.has_service(DOMAIN, name), name


@pytest.mark.quality_rule("appropriate-polling")
async def test_no_entity_polls(hass, fake_trv):
    """Every entity is pushed by the states it follows, so none is polled."""
    await _started_entry(hass, fake_trv)

    entities = _entities(hass)
    assert entities
    assert [(e.entity_id, type(e).__name__) for e in entities if e.should_poll] == []


@pytest.mark.quality_rule("has-entity-name")
async def test_every_entity_is_named_within_its_device(hass, fake_trv):
    await _started_entry(hass, fake_trv)

    entities = _entities(hass)
    assert entities
    assert [e.entity_id for e in entities if not e.has_entity_name] == []


@pytest.mark.quality_rule("entity-unique-id")
async def test_every_entity_has_its_own_unique_id(hass, fake_trv):
    """Each entity is registered under a unique id no other entity shares."""
    entry = await _started_entry(hass, fake_trv)

    entities = _entities(hass)
    unique_ids = [e.unique_id for e in entities]
    assert None not in unique_ids
    assert len(set(unique_ids)) == len(unique_ids)
    registered = {
        reg.unique_id
        for reg in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    }
    assert set(unique_ids) <= registered


@pytest.mark.quality_rule("entity-unique-id")
@pytest.mark.parametrize("rename", ["title", "configured name"])
async def test_the_unique_ids_do_not_follow_the_thermostat_name(hass, fake_trv, rename):
    """Renaming the thermostat keeps the identity of every entity."""
    entry = await _started_entry(hass, fake_trv)
    before = {e.unique_id for e in _entities(hass)}

    if rename == "title":
        hass.config_entries.async_update_entry(entry, title="Renamed")
    else:
        settings = {**entry_settings(entry), CONF_NAME: "Renamed"}
        hass.config_entries.async_update_entry(entry, options=settings)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await wait_for_startup(hass, entry)

    renamed = entry.title if rename == "title" else entry_settings(entry)[CONF_NAME]
    assert renamed == "Renamed"
    assert {e.unique_id for e in _entities(hass)} == before


@pytest.mark.quality_rule("runtime-data")
async def test_a_loaded_entry_keeps_its_runtime_state_on_the_entry(hass, fake_trv):
    """The entry carries its thermostat; nothing is parked in ``hass.data``."""
    entry = await _started_entry(hass, fake_trv)

    assert isinstance(entry.runtime_data, BetterThermostatData)
    (climate,) = [e for e in _entities(hass) if e.entity_id.startswith("climate.")]
    assert entry.runtime_data.climate is climate
    assert DOMAIN not in hass.data


@pytest.mark.quality_rule("runtime-data")
async def test_an_unloaded_entry_lets_go_of_its_runtime_state(hass, fake_trv):
    entry = await _started_entry(hass, fake_trv)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert not hasattr(entry, "runtime_data")


@pytest.mark.quality_rule("test-before-configure")
async def test_the_flow_refuses_a_thermostat_home_assistant_does_not_know(hass):
    """A thermostat that is not there is sent back before an entry exists.

    Better Thermostat talks to no device of its own; what it can check before
    it configures anything is that the thermostat it is to drive exists.
    """
    set_room_sensor(hass, 19.0)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"name": "Nowhere", CONF_HEATER: ["climate.not_there"], CONF_SENSOR: SENSOR_ID},
    )

    assert result["type"] is FlowResultType.FORM, result
    assert result["step_id"] == "user", result
    assert result["errors"] == {CONF_HEATER: "trv_not_found"}
    assert result["description_placeholders"]["trv"] == "climate.not_there"
    assert not hass.config_entries.async_entries(DOMAIN)
