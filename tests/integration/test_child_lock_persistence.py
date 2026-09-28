"""The child-lock switch keeps what the user last set, from wherever they set it.

The lock can be set from two places: the switch, and the child-lock option of
the TRV in the configuration. Whichever the user touched last is what the
thermostat holds after a reload or a restart, and a switch change has to
survive a restart on its own rather than through some later save of the
config entry.
"""

import copy

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, STATE_OFF, STATE_ON
from homeassistant.core import CoreState, State
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
    setup_test_component_platform,
)

from .conftest import (
    DOMAIN,
    SENSOR_ID,
    FakeTrvEntity,
    make_entry,
    setup_entry,
    wait_for_startup,
)

SWITCH = "switch.bt_test_child_lock"


def _entry(child_lock: bool) -> MockConfigEntry:
    """Return the harness entry with the child-lock option set to ``child_lock``."""
    entry = make_entry()
    data = copy.deepcopy(dict(entry.data))
    data["thermostat"][0]["advanced"]["child_lock"] = child_lock
    return MockConfigEntry(
        domain=DOMAIN, version=entry.version, data=data, title=entry.title
    )


def _held(bt) -> bool:
    """Return the child-lock setting the thermostat applies to its TRV."""
    (trv,) = bt.real_trvs.values()
    return bool((trv.advanced or {}).get("child_lock"))


async def _started(hass, entry):
    hass.states.async_set(SENSOR_ID, "19.0", {"unit_of_measurement": "°C"})
    await setup_entry(hass, entry)
    return await wait_for_startup(hass, entry)


async def _reload(hass, entry, **advanced):
    """Reload ``entry``, with ``advanced`` merged into the TRV's options first."""
    if advanced:
        data = copy.deepcopy(dict(entry.data))
        data["thermostat"][0]["advanced"].update(advanced)
        hass.config_entries.async_update_entry(entry, data=data)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    return await wait_for_startup(hass, entry)


@pytest.mark.parametrize("wanted", [True, False])
async def test_a_switch_change_survives_a_restart(hass, fake_trv, wanted):
    """The switch state the user left before a restart is the one held after it."""
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(SWITCH, STATE_ON if wanted else STATE_OFF),
                {"configured": not wanted},
            )
        ],
    )

    bt = await _started(hass, _entry(not wanted))

    assert hass.states.get(SWITCH).state == (STATE_ON if wanted else STATE_OFF)
    assert _held(bt) is wanted


async def test_an_option_change_before_a_restart_wins_over_the_switch(hass, fake_trv):
    """An option set after the last switch change is held after a restart."""
    mock_restore_cache_with_extra_data(
        hass, [(State(SWITCH, STATE_OFF), {"configured": False})]
    )

    bt = await _started(hass, _entry(True))

    assert hass.states.get(SWITCH).state == STATE_ON
    assert _held(bt) is True


async def test_a_switch_change_does_not_touch_the_config_entry(hass, fake_trv):
    """Flipping the switch leaves the entry's stored option as it was."""
    entry = _entry(False)
    await _started(hass, entry)

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": SWITCH}, blocking=True
    )

    assert entry.data["thermostat"][0]["advanced"]["child_lock"] is False


@pytest.mark.parametrize("wanted", [True, False])
async def test_a_switch_change_survives_a_reload(hass, fake_trv, wanted):
    """A reload that changes nothing keeps the switch where the user put it."""
    entry = _entry(not wanted)
    await _started(hass, entry)
    await hass.services.async_call(
        "switch",
        "turn_on" if wanted else "turn_off",
        {"entity_id": SWITCH},
        blocking=True,
    )

    bt = await _reload(hass, entry)

    assert hass.states.get(SWITCH).state == (STATE_ON if wanted else STATE_OFF)
    assert _held(bt) is wanted


@pytest.mark.parametrize("wanted", [True, False])
async def test_an_option_change_after_the_switch_wins(hass, fake_trv, wanted):
    """Setting the option in the configuration overrides an earlier switch change."""
    entry = _entry(not wanted)
    await _started(hass, entry)
    await hass.services.async_call(
        "switch",
        "turn_off" if wanted else "turn_on",
        {"entity_id": SWITCH},
        blocking=True,
    )

    bt = await _reload(hass, entry, child_lock=wanted)

    assert hass.states.get(SWITCH).state == (STATE_ON if wanted else STATE_OFF)
    assert _held(bt) is wanted


DEVICE_LOCK = "lock.locking_trv_child_lock"


class _LockingTrv(FakeTrvEntity):
    """A head with a registry entry, whose device also carries a child lock."""

    _attr_name = "locking trv"
    _attr_unique_id = "locking_trv"


async def _device_with_a_child_lock(
    hass, locked: bool, *, reports: bool = True
) -> tuple[str, list[str]]:
    """Register the head on a device with a child lock; return its id and commands.

    With ``reports`` the lock's state follows each command at once; without
    it the lock keeps reporting what it did before, as a device whose
    reports lag behind its commands does.
    """
    head = _LockingTrv()
    setup_test_component_platform(hass, CLIMATE_DOMAIN, [head])
    assert await async_setup_component(
        hass, CLIMATE_DOMAIN, {CLIMATE_DOMAIN: {"platform": "test"}}
    )
    await hass.async_block_till_done()
    device_entry = MockConfigEntry(domain="test")
    device_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=device_entry.entry_id, identifiers={("test", "locking_trv")}
    )
    registry = er.async_get(hass)
    registry.async_update_entity(head.entity_id, device_id=device.id)
    registry.async_get_or_create(
        "lock",
        "test",
        "locking_trv_child_lock",
        device_id=device.id,
        suggested_object_id="locking_trv_child_lock",
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
    return head.entity_id, commands


def _device_locked(hass) -> bool:
    return hass.states.get(DEVICE_LOCK).state == "locked"


def _entry_on(trv_entity_id: str, child_lock: bool) -> MockConfigEntry:
    entry = _entry(child_lock)
    data = copy.deepcopy(dict(entry.data))
    data["thermostat"][0]["trv"] = trv_entity_id
    return MockConfigEntry(
        domain=DOMAIN, version=entry.version, data=data, title=entry.title
    )


@pytest.mark.parametrize("wanted", [True, False])
async def test_the_device_follows_the_switch_after_a_restart(hass, wanted):
    """After a restart the device holds the state the switch restored."""
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(SWITCH, STATE_ON if wanted else STATE_OFF),
                {"configured": not wanted},
            )
        ],
    )
    trv, _commands = await _device_with_a_child_lock(hass, locked=wanted)
    entry = _entry_on(trv, not wanted)
    hass.states.async_set(SENSOR_ID, "19.0", {"unit_of_measurement": "°C"})
    hass.set_state(CoreState.starting)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    await wait_for_startup(hass, entry)
    await hass.async_block_till_done()

    assert hass.states.get(SWITCH).state == (STATE_ON if wanted else STATE_OFF)
    assert _device_locked(hass) is wanted


@pytest.mark.parametrize("wanted", [True, False])
async def test_the_device_follows_the_switch_after_a_reload(hass, wanted):
    """A reload that changes nothing leaves the device as the switch shows."""
    trv, _commands = await _device_with_a_child_lock(hass, locked=not wanted)
    entry = _entry_on(trv, not wanted)
    await _started(hass, entry)
    await hass.services.async_call(
        "switch",
        "turn_on" if wanted else "turn_off",
        {"entity_id": SWITCH},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert _device_locked(hass) is wanted

    await _reload(hass, entry)
    await hass.async_block_till_done()

    assert hass.states.get(SWITCH).state == (STATE_ON if wanted else STATE_OFF)
    assert _device_locked(hass) is wanted


@pytest.mark.parametrize("wanted", [True, False])
async def test_the_device_follows_an_option_set_after_the_switch(hass, wanted):
    """An option changed after the switch is what the device ends up with."""
    trv, _commands = await _device_with_a_child_lock(hass, locked=not wanted)
    entry = _entry_on(trv, not wanted)
    await _started(hass, entry)
    await hass.services.async_call(
        "switch",
        "turn_off" if wanted else "turn_on",
        {"entity_id": SWITCH},
        blocking=True,
    )

    await _reload(hass, entry, child_lock=wanted)
    await hass.async_block_till_done()

    assert hass.states.get(SWITCH).state == (STATE_ON if wanted else STATE_OFF)
    assert _device_locked(hass) is wanted


async def test_the_last_command_is_the_switch_state_while_reports_lag(hass):
    """With the device's reports lagging, the last command sent is the switch's."""
    trv, commands = await _device_with_a_child_lock(hass, locked=True, reports=False)
    entry = _entry_on(trv, False)
    await _started(hass, entry)
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": SWITCH}, blocking=True
    )
    commands.clear()

    await _reload(hass, entry)
    await hass.async_block_till_done()

    assert commands, "nothing was sent to the device"
    assert commands[-1] == "lock"
