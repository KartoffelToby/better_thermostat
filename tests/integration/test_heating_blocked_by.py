"""The thermostat says what keeps its room from heating, and which switches it holds.

``heating_blocked_by`` follows the decision kernel through Home Assistant:
an open window, released devices and the room turned off each name their
block, and the block clears when the room heats again. ``active_overrides``
lists every switch held away from its default, also while the room heats.
"""

from unittest.mock import patch

from homeassistant.components.climate import (
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_HVAC_MODE,
    HVACMode,
)
from homeassistant.const import ATTR_ENTITY_ID, EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import mock_restore_cache

from custom_components.better_thermostat.utils.const import (
    ATTR_STATE_ACTIVE_OVERRIDES,
    ATTR_STATE_HEATING_BLOCKED_BY,
)

from .conftest import (
    BT_ENTITY,
    DOMAIN,
    SENSOR_ID,
    WINDOW_ID,
    WRITE_BUDGET,
    build_devices,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import GENERIC_HEAT_TRV


def _blocked_by(hass) -> object:
    return hass.states.get(BT_ENTITY).attributes[ATTR_STATE_HEATING_BLOCKED_BY]


def _overrides(hass) -> object:
    return hass.states.get(BT_ENTITY).attributes[ATTR_STATE_ACTIVE_OVERRIDES]


async def _switch(hass, bt, key: str, on: bool) -> None:
    entity_id = er.async_get(hass).async_get_entity_id(
        "switch", DOMAIN, f"{bt.unique_id}_{key}"
    )
    assert entity_id is not None
    await hass.services.async_call(
        "switch",
        "turn_on" if on else "turn_off",
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    await hass.async_block_till_done()


async def _started_with_window(hass, *, window: str):
    (head,) = await build_devices(hass, GENERIC_HEAT_TRV)
    # A room well below the target, so the head heats whenever it may.
    set_room_sensor(hass, 17.0)
    hass.states.async_set(WINDOW_ID, window)
    entry = make_entry(head.profile, with_window=True)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    return head, bt


async def test_an_open_window_names_its_block_until_it_closes(hass):
    """The window turns the head off, and the thermostat says so."""
    with patch(WRITE_BUDGET, 0.0):
        head, _ = await _started_with_window(hass, window="off")
        assert await wait_for(hass, lambda: head.hvac_mode == HVACMode.HEAT)
        assert _blocked_by(hass) is None
        assert _overrides(hass) == []

        hass.states.async_set(WINDOW_ID, "on")
        assert await wait_for(hass, lambda: head.hvac_mode == HVACMode.OFF)
        assert await wait_for(hass, lambda: _blocked_by(hass) == "window")

        hass.states.async_set(WINDOW_ID, "off")
        assert await wait_for(hass, lambda: head.hvac_mode == HVACMode.HEAT)
        assert await wait_for(hass, lambda: _blocked_by(hass) is None)


async def test_window_detection_off_heats_and_is_listed(hass):
    """With detection off nothing blocks, and the switch shows as held."""
    with patch(WRITE_BUDGET, 0.0):
        head, bt = await _started_with_window(hass, window="on")
        assert await wait_for(hass, lambda: _blocked_by(hass) == "window")

        await _switch(hass, bt, "window_detection", False)
        assert await wait_for(hass, lambda: head.hvac_mode == HVACMode.HEAT)
        assert await wait_for(hass, lambda: _blocked_by(hass) is None)
        assert _overrides(hass) == ["window_detection_off"]


async def test_released_devices_name_their_block_at_once(hass, fake_trv):
    """No control cycle follows a release; the state names the block anyway."""
    set_room_sensor(hass, 19.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    await hass.async_block_till_done()
    assert _blocked_by(hass) is None

    await _switch(hass, bt, "release_devices", True)
    state = hass.states.get(BT_ENTITY)
    assert state.attributes["devices_released"] is True
    assert state.attributes[ATTR_STATE_HEATING_BLOCKED_BY] == "devices_released"
    assert state.attributes[ATTR_STATE_ACTIVE_OVERRIDES] == ["devices_released"]

    with patch(WRITE_BUDGET, 0.0):
        await _switch(hass, bt, "release_devices", False)
        assert await wait_for(hass, lambda: _blocked_by(hass) is None)
    assert _overrides(hass) == []


async def test_a_paused_learner_is_listed_while_the_room_heats(hass, fake_trv):
    """Pausing learning blocks no heating, and it does not go unseen."""
    set_room_sensor(hass, 19.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)

    await _switch(hass, bt, "pause_learning", True)
    assert _blocked_by(hass) is None
    assert _overrides(hass) == ["learning_paused"]


async def test_turning_the_room_off_names_the_mode(hass, fake_trv):
    """A room switched off is blocked by its own mode."""
    set_room_sensor(hass, 19.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)

    with patch(WRITE_BUDGET, 0.0):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_HVAC_MODE,
            {ATTR_ENTITY_ID: BT_ENTITY, "hvac_mode": HVACMode.OFF},
            blocking=True,
        )
        assert await wait_for(hass, lambda: _blocked_by(hass) == "hvac_off")


async def test_the_block_comes_from_the_first_decision_after_a_restart(hass, fake_trv):
    """A saved block is not restored; the first decision names it again.

    The saved state carries a window block from before the restart, and the
    released devices it also carries are what the kernel decides on now.
    """
    mock_restore_cache(
        hass,
        [
            State(
                BT_ENTITY,
                HVACMode.HEAT,
                {
                    "temperature": 22.5,
                    "devices_released": True,
                    ATTR_STATE_HEATING_BLOCKED_BY: "window",
                },
            )
        ],
    )
    hass.states.async_set(SENSOR_ID, "19.5", {"unit_of_measurement": "°C"})
    hass.set_state(CoreState.starting)
    entry = make_entry(fake_trv.profile)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    await wait_for_startup(hass, entry)

    assert _blocked_by(hass) == "devices_released"
    assert _overrides(hass) == ["devices_released"]


async def test_the_thermostat_keeps_the_device_name(hass, fake_trv):
    """The translation key reaches the attributes, not the entity's name."""
    set_room_sensor(hass, 19.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)

    registered = er.async_get(hass).async_get(BT_ENTITY)
    assert registered is not None
    assert registered.translation_key == "better_thermostat"
    assert registered.original_name is None
