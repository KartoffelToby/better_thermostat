"""The window detection switch against a running Home Assistant.

A thermostat with a window sensor gets a switch that decides whether an
open window turns its heating off. Turned off through the switch service,
the heads heat with the window open, a close delay still running
included; turned back on, the open window turns them off again. The
setting survives a reload and a restart.
"""

from unittest.mock import AsyncMock, patch

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import (
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
)
from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
    mock_restore_cache_with_extra_data,
)

from custom_components.better_thermostat.entity import LAST_AVAILABLE_STATE
from custom_components.better_thermostat.switch import restored_window_detection
from custom_components.better_thermostat.utils.const import DOMAIN

from .conftest import (
    BT_ENTITY,
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

SWITCH = "switch.bt_test_window_detection"


async def _set_detection(hass, on: bool) -> None:
    await hass.services.async_call(
        "switch", "turn_on" if on else "turn_off", {"entity_id": SWITCH}, blocking=True
    )
    await hass.async_block_till_done()


async def _started_with_window(hass, *, window: str, close_delay: int = 0):
    (head,) = await build_devices(hass, GENERIC_HEAT_TRV)
    # A room well below the target, so the heads heat whenever they may.
    set_room_sensor(hass, 17.0)
    hass.states.async_set(WINDOW_ID, window)
    template = make_entry(head.profile, with_window=True)
    entry = MockConfigEntry(
        domain=template.domain,
        version=template.version,
        title=template.title,
        data={**template.data, "window_off_delay_after": close_delay},
    )
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    return head, bt, entry


async def test_the_switch_exists_with_a_window_sensor_and_is_on(hass):
    """A configuration switch, on by default, mirrored on the thermostat."""
    with patch(WRITE_BUDGET, 0.0):
        await _started_with_window(hass, window="off")

    assert hass.states.get(SWITCH).state == STATE_ON
    entry = er.async_get(hass).async_get(SWITCH)
    assert entry is not None
    assert entry.entity_category is EntityCategory.CONFIG
    assert hass.states.get(BT_ENTITY).attributes["window_detection"] is True


async def test_no_switch_without_a_contact_sensor(hass, fake_trv):
    """Without a window or door sensor there is nothing to switch."""
    set_room_sensor(hass, 17.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)

    assert hass.states.get(SWITCH) is None
    assert "window_detection" not in hass.states.get(BT_ENTITY).attributes


async def test_turning_the_switch_off_heats_with_the_window_open(hass):
    """Off, the head heats although the window is open; on, it is off again."""
    with patch(WRITE_BUDGET, 0.0):
        head, bt, _ = await _started_with_window(hass, window="on")
        assert await wait_for(hass, lambda: head.hvac_mode == HVACMode.OFF)

        await _set_detection(hass, False)
        assert await wait_for(hass, lambda: head.hvac_mode == HVACMode.HEAT)
        state = hass.states.get(BT_ENTITY)
        assert state.attributes["window_detection"] is False
        # The window is still reported open: only the control ignores it.
        assert state.attributes["window_open"] is True
        assert hass.states.get(SWITCH).state == STATE_OFF

        await _set_detection(hass, True)
        assert await wait_for(hass, lambda: head.hvac_mode == HVACMode.OFF)
        assert bt.kernel_state.window_detection is True


async def test_turning_the_switch_off_skips_the_close_delay(hass):
    """A window just closed stops holding the heating off at once."""
    with patch(WRITE_BUDGET, 0.0):
        head, bt, _ = await _started_with_window(hass, window="on", close_delay=600)
        assert await wait_for(hass, lambda: head.hvac_mode == HVACMode.OFF)

        hass.states.async_set(WINDOW_ID, "off")
        await hass.async_block_till_done()
        # The close delay is running, so the window still counts as open.
        assert bt.kernel_state.window.pending_since is not None
        assert head.hvac_mode == HVACMode.OFF

        await _set_detection(hass, False)
        assert await wait_for(hass, lambda: head.hvac_mode == HVACMode.HEAT)
        assert bt.kernel_state.window.pending_since is None
        assert hass.states.get(BT_ENTITY).attributes["window_open"] is False


async def test_the_setting_survives_a_reload(hass):
    """The thermostat reads the switch's saved state before it controls.

    The switch's own restore is taken out, so only the thermostat's read
    can bring the setting back.
    """
    with patch(WRITE_BUDGET, 0.0):
        head, _, entry = await _started_with_window(hass, window="on")
        await _set_detection(hass, False)
        assert await wait_for(hass, lambda: head.hvac_mode == HVACMode.HEAT)

        with patch(
            "custom_components.better_thermostat.switch.set_window_detection",
            new=AsyncMock(),
        ):
            assert await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()
            bt = await wait_for_startup(hass, entry)

        assert bt.kernel_state.window_detection is False
        assert hass.states.get(SWITCH).state == STATE_OFF
        assert hass.states.get(BT_ENTITY).attributes["window_detection"] is False
        assert head.hvac_mode == HVACMode.HEAT


async def test_the_switch_restores_its_saved_state_after_a_restart(hass):
    """A switch saved off before a restart is off after it, and heats."""
    mock_restore_cache(hass, [State(SWITCH, STATE_OFF)])
    with patch(WRITE_BUDGET, 0.0):
        head, bt, _ = await _started_with_window(hass, window="on")
        assert await wait_for(hass, lambda: head.hvac_mode == HVACMode.HEAT)

    assert bt.kernel_state.window_detection is False
    assert hass.states.get(SWITCH).state == STATE_OFF


def _register_the_switch(hass) -> None:
    er.async_get(hass).async_get_or_create(
        "switch", DOMAIN, "bt_uid_window_detection", suggested_object_id="bt_test_wd"
    )


async def test_nothing_is_restored_for_an_unregistered_switch(hass):
    """A switch missing from the entity registry restores nothing.

    Its saved state cannot be found by unique id, so the switch restores
    itself once it is added.
    """
    mock_restore_cache(hass, [State("switch.bt_test_wd", STATE_OFF)])
    assert restored_window_detection(hass, "bt_uid") is None


async def test_nothing_is_restored_without_a_saved_state(hass):
    _register_the_switch(hass)
    assert restored_window_detection(hass, "bt_uid") is None


@pytest.mark.parametrize(
    ("saved", "extra", "expected"),
    [
        (STATE_OFF, {}, False),
        (STATE_ON, {}, True),
        (STATE_UNKNOWN, {}, None),
        (
            STATE_UNAVAILABLE,
            {LAST_AVAILABLE_STATE: State("switch.bt_test_wd", STATE_OFF).as_dict()},
            False,
        ),
        (STATE_UNAVAILABLE, {}, None),
    ],
)
async def test_the_saved_switch_state_is_restored(hass, saved, extra, expected):
    """On and off restore; a state saved while unavailable restores the one before."""
    _register_the_switch(hass)
    mock_restore_cache_with_extra_data(
        hass, [(State("switch.bt_test_wd", saved), extra)]
    )
    assert restored_window_detection(hass, "bt_uid") is expected
