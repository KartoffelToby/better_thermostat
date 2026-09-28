"""The child-lock switch keeps what the user last set, from wherever they set it.

The lock can be set from two places: the switch, and the child-lock option of
the TRV in the configuration. Whichever the user touched last is what the
thermostat holds after a reload or a restart, and a switch change has to
survive a restart on its own rather than through some later save of the
config entry.
"""

import copy

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import State
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)

from .conftest import DOMAIN, SENSOR_ID, make_entry, setup_entry, wait_for_startup

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
