"""The child-lock switch keeps what the user last set, from wherever they set it.

The lock can be set from two places: the switch, and the child-lock option of
the TRV in the options flow. Whichever the user touched last is what the
thermostat holds after a reload or a restart, and a switch change has to
survive a restart on its own rather than through some later save of the
config entry.
"""

from dataclasses import replace
from unittest.mock import patch

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import (
    mock_restore_cache_with_extra_data,
)

from custom_components.better_thermostat import climate as climate_module

from .boot_sequence import finish_boot, set_up_during_boot
from .conftest import (
    build_devices,
    click_through_the_options,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for_startup,
)
from .device_profiles import GENERIC_HEAT_TRV

SWITCH = "switch.bt_test_child_lock"


def _held(bt) -> bool:
    """Return the child-lock setting the thermostat applies to its TRV."""
    (trv,) = bt.real_trvs.values()
    return bool((trv.advanced or {}).get("child_lock"))


async def _started(hass, entry):
    await setup_entry(hass, entry)
    return await wait_for_startup(hass, entry)


@pytest.mark.parametrize("wanted", [True, False])
async def test_a_switch_change_survives_a_restart(hass, wanted):
    """The switch state the user left before a restart is the one held after it."""
    set_room_sensor(hass, 19.0)
    entry = make_entry(GENERIC_HEAT_TRV)
    entry.data["thermostat"][0]["advanced"]["child_lock"] = not wanted
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(SWITCH, STATE_ON if wanted else STATE_OFF),
                {"configured": not wanted},
            )
        ],
    )
    await build_devices(hass, GENERIC_HEAT_TRV)

    bt = await _started(hass, entry)

    assert hass.states.get(SWITCH).state == (STATE_ON if wanted else STATE_OFF)
    assert _held(bt) is wanted


async def test_a_switch_change_does_not_touch_the_config_entry(hass, fake_trv):
    """Flipping the switch leaves the entry's stored option as it was."""
    set_room_sensor(hass, 19.0)
    entry = make_entry(fake_trv.profile)
    await _started(hass, entry)

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": SWITCH}, blocking=True
    )

    assert entry.data["thermostat"][0]["advanced"]["child_lock"] is False


@pytest.mark.parametrize("wanted", [True, False])
async def test_a_switch_change_survives_a_reload(hass, fake_trv, wanted):
    """A reload that changes nothing keeps the switch where the user put it."""
    set_room_sensor(hass, 19.0)
    entry = make_entry(fake_trv.profile)
    entry.data["thermostat"][0]["advanced"]["child_lock"] = not wanted
    await _started(hass, entry)
    await hass.services.async_call(
        "switch",
        "turn_on" if wanted else "turn_off",
        {"entity_id": SWITCH},
        blocking=True,
    )

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    bt = await wait_for_startup(hass, entry)

    assert hass.states.get(SWITCH).state == (STATE_ON if wanted else STATE_OFF)
    assert _held(bt) is wanted


@pytest.mark.parametrize("wanted", [True, False])
async def test_an_options_change_after_the_switch_wins(hass, fake_trv, wanted):
    """Setting the option in the options flow overrides an earlier switch change."""
    set_room_sensor(hass, 19.0)
    entry = make_entry(fake_trv.profile)
    entry.data["thermostat"][0]["advanced"]["child_lock"] = not wanted
    await _started(hass, entry)
    await hass.services.async_call(
        "switch",
        "turn_off" if wanted else "turn_on",
        {"entity_id": SWITCH},
        blocking=True,
    )

    await click_through_the_options(hass, entry, child_lock=wanted)
    await hass.async_block_till_done()
    bt = await wait_for_startup(hass, entry)

    assert hass.states.get(SWITCH).state == (STATE_ON if wanted else STATE_OFF)
    assert _held(bt) is wanted


async def test_an_options_change_before_a_restart_wins_over_the_switch(hass):
    """An option set after the last switch change is held after a restart."""
    set_room_sensor(hass, 19.0)
    entry = make_entry(GENERIC_HEAT_TRV)
    entry.data["thermostat"][0]["advanced"]["child_lock"] = True
    mock_restore_cache_with_extra_data(
        hass, [(State(SWITCH, STATE_OFF), {"configured": False})]
    )
    await build_devices(hass, GENERIC_HEAT_TRV)

    bt = await _started(hass, entry)

    assert hass.states.get(SWITCH).state == STATE_ON
    assert _held(bt) is True


DEVICE_LOCK = "lock.fake_trv_child_lock"
LOCKING_TRV = replace(
    GENERIC_HEAT_TRV, name="locking_trv", has_device_registry_entry=True
)
"""A head whose device carries a child-lock entity next to its thermostat."""


async def _device_with_a_child_lock(
    hass, locked: bool, *, reports: bool = True
) -> list[str]:
    """Build the head and give its device a child lock; return its commands.

    With ``reports`` the lock's state follows each command at once; without
    it the lock keeps reporting ``locked`` as it did before, as a device
    whose reports lag behind its commands does.
    """
    await build_devices(hass, LOCKING_TRV)
    registry = er.async_get(hass)
    trv = registry.async_get(LOCKING_TRV.entity_id)
    assert trv is not None and trv.device_id is not None
    registry.async_get_or_create(
        "lock",
        "test",
        "fake_trv_child_lock",
        device_id=trv.device_id,
        suggested_object_id="fake_trv_child_lock",
    )
    hass.states.async_set(DEVICE_LOCK, "locked" if locked else "unlocked")

    commands: list[str] = []

    async def _apply(call):
        commands.append(call.service)
        if reports:
            hass.states.async_set(
                DEVICE_LOCK, "locked" if call.service == "lock" else "unlocked"
            )

    hass.services.async_register("lock", "lock", _apply)
    hass.services.async_register("lock", "unlock", _apply)
    return commands


def _device_locked(hass) -> bool:
    return hass.states.get(DEVICE_LOCK).state == "locked"


def _entry_with_option(child_lock: bool):
    entry = make_entry(LOCKING_TRV)
    entry.data["thermostat"][0]["advanced"]["child_lock"] = child_lock
    return entry


@pytest.mark.parametrize("wanted", [True, False])
async def test_the_device_follows_the_switch_after_a_restart(hass, wanted):
    """After a restart the device holds the state the switch restored."""
    set_room_sensor(hass, 19.0)
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(SWITCH, STATE_ON if wanted else STATE_OFF),
                {"configured": not wanted},
            )
        ],
    )
    commands = await _device_with_a_child_lock(hass, locked=not wanted)
    entry = _entry_with_option(not wanted)

    await set_up_during_boot(hass, entry)
    await finish_boot(hass, entry)
    await hass.async_block_till_done()

    assert hass.states.get(SWITCH).state == (STATE_ON if wanted else STATE_OFF)
    assert commands == ["lock" if wanted else "unlock"]


@pytest.mark.parametrize("wanted", [True, False])
@pytest.mark.parametrize("how", ["reload", "options_save"])
async def test_the_device_follows_the_switch_after_a_reload(hass, wanted, how):
    """A reload, or saving the options unchanged, sends the device nothing.

    The device already holds the switch's state; a command to the configured
    option would unlock it for a moment.
    """
    set_room_sensor(hass, 19.0)
    commands = await _device_with_a_child_lock(hass, locked=not wanted)
    entry = _entry_with_option(not wanted)
    await _started(hass, entry)
    await hass.services.async_call(
        "switch",
        "turn_on" if wanted else "turn_off",
        {"entity_id": SWITCH},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert _device_locked(hass) is wanted
    commands.clear()

    if how == "reload":
        assert await hass.config_entries.async_reload(entry.entry_id)
    else:
        await click_through_the_options(hass, entry)
    await hass.async_block_till_done()
    await wait_for_startup(hass, entry)
    await hass.async_block_till_done()

    assert hass.states.get(SWITCH).state == (STATE_ON if wanted else STATE_OFF)
    assert commands == []


@pytest.mark.parametrize("wanted", [True, False])
async def test_the_device_follows_an_option_set_after_the_switch(hass, wanted):
    """An option changed after the switch reaches the device in one command."""
    set_room_sensor(hass, 19.0)
    commands = await _device_with_a_child_lock(hass, locked=not wanted)
    entry = _entry_with_option(not wanted)
    await _started(hass, entry)
    await hass.services.async_call(
        "switch",
        "turn_off" if wanted else "turn_on",
        {"entity_id": SWITCH},
        blocking=True,
    )
    await hass.async_block_till_done()
    commands.clear()

    await click_through_the_options(hass, entry, child_lock=wanted)
    await hass.async_block_till_done()
    await wait_for_startup(hass, entry)
    await hass.async_block_till_done()

    assert hass.states.get(SWITCH).state == (STATE_ON if wanted else STATE_OFF)
    assert commands == ["lock" if wanted else "unlock"]


@pytest.mark.parametrize("wanted", [True, False])
async def test_no_command_contradicts_the_switch_while_reports_lag(hass, wanted):
    """With the device's reports lagging, every command sent is the switch's."""
    set_room_sensor(hass, 19.0)
    commands = await _device_with_a_child_lock(hass, locked=True, reports=False)
    entry = _entry_with_option(not wanted)
    await _started(hass, entry)
    await hass.services.async_call(
        "switch",
        "turn_on" if wanted else "turn_off",
        {"entity_id": SWITCH},
        blocking=True,
    )
    commands.clear()

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await wait_for_startup(hass, entry)
    await hass.async_block_till_done()

    assert all(command == ("lock" if wanted else "unlock") for command in commands)


@pytest.mark.parametrize("wanted", [True, False])
async def test_the_switch_sets_the_device_when_the_startup_could_not(hass, wanted):
    """Without the switch's restored state at startup, the switch still sets the device.

    The startup then sends the option; the switch restores after it and has
    to put its own state on the device.
    """
    set_room_sensor(hass, 19.0)
    await _device_with_a_child_lock(hass, locked=not wanted)
    entry = _entry_with_option(not wanted)
    await _started(hass, entry)
    await hass.services.async_call(
        "switch",
        "turn_on" if wanted else "turn_off",
        {"entity_id": SWITCH},
        blocking=True,
    )
    await hass.async_block_till_done()

    with patch.object(
        climate_module, "restored_child_lock", autospec=True, return_value=None
    ):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        await wait_for_startup(hass, entry)
        await hass.async_block_till_done()

    assert _device_locked(hass) is wanted
