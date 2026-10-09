"""A restart keeps the settings of an entity that was unavailable at the stop.

The thermostat goes unavailable without any TRV to drive, and the child lock
and the valve cap go unavailable with their TRV. Home Assistant saves an
unavailable entity as ``unavailable``, without attributes, so each of them
saves the last state it showed while available next to it and restores that.
"""

from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import ATTR_TEMPERATURE, STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import State
import pytest
from pytest_homeassistant_custom_component.common import (
    mock_restore_cache_with_extra_data,
)

from custom_components.better_thermostat.entity import LAST_AVAILABLE_STATE
from custom_components.better_thermostat.utils.const import CalibrationOutput

from .conftest import (
    BT_ENTITY,
    CRITICAL_GRACE,
    build_devices,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import GENERIC_HEAT_TRV, VALVE_TRV

NO_GRACE = timedelta(seconds=0)
SWITCH = "switch.bt_test_child_lock"
VALVE_CAP = "number.bt_test_valve_max_opening"
DRIVEN_VALVE_TRV = replace(
    VALVE_TRV, calibration=CalibrationOutput.DIRECT_VALVE_BASED.value
)


def _saved_unavailable(shown: State, **extra) -> tuple[State, dict[str, object]]:
    """Return what Home Assistant saved for an entity unavailable at the stop."""
    return (
        State(shown.entity_id, STATE_UNAVAILABLE),
        {**extra, LAST_AVAILABLE_STATE: shown.as_dict()},
    )


async def _started(hass, profile):
    """Start a room for ``profile``; return the thermostat and the device."""
    set_room_sensor(hass, 19.0)
    (device,) = await build_devices(hass, profile)
    entry = make_entry(profile)
    await setup_entry(hass, entry)
    return await wait_for_startup(hass, entry), device


async def test_a_room_unavailable_at_the_stop_restores_its_target(hass):
    mock_restore_cache_with_extra_data(
        hass,
        [_saved_unavailable(State(BT_ENTITY, HVACMode.HEAT, {ATTR_TEMPERATURE: 23.5}))],
    )

    bt, _ = await _started(hass, GENERIC_HEAT_TRV)

    assert bt.heat_target_temperature == 23.5


async def test_a_room_that_goes_unavailable_saves_what_it_showed_before(hass):
    """The state saved next to the unavailable one is the last one shown."""
    with patch(CRITICAL_GRACE, NO_GRACE):
        bt, device = await _started(hass, GENERIC_HEAT_TRV)
    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": BT_ENTITY, ATTR_TEMPERATURE: 22.0},
        blocking=True,
    )

    device.set_available(False)
    assert await wait_for(
        hass, lambda: hass.states.get(BT_ENTITY).state == STATE_UNAVAILABLE
    )

    saved = State.from_dict(bt.extra_restore_state_data.as_dict()[LAST_AVAILABLE_STATE])
    assert saved is not None
    assert saved.state == HVACMode.HEAT
    assert saved.attributes[ATTR_TEMPERATURE] == 22.0


async def test_a_valve_cap_unavailable_at_the_stop_restores_its_value(hass):
    mock_restore_cache_with_extra_data(
        hass, [_saved_unavailable(State(VALVE_CAP, "40.0"))]
    )

    bt, _ = await _started(hass, DRIVEN_VALVE_TRV)

    (trv,) = bt.real_trvs.values()
    assert trv.valve_max_opening == 40.0


@pytest.mark.parametrize("wanted", [True, False])
async def test_a_child_lock_unavailable_at_the_stop_restores_its_state(hass, wanted):
    """The switch state shown before the outage wins over the option."""
    mock_restore_cache_with_extra_data(
        hass,
        [
            _saved_unavailable(
                State(SWITCH, STATE_ON if wanted else STATE_OFF), configured=not wanted
            )
        ],
    )
    set_room_sensor(hass, 19.0)
    await build_devices(hass, GENERIC_HEAT_TRV)
    entry = make_entry(GENERIC_HEAT_TRV)
    entry.data["thermostat"][0]["advanced"]["child_lock"] = not wanted
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)

    (trv,) = bt.real_trvs.values()
    assert bool(trv.advanced.get("child_lock")) is wanted
    assert hass.states.get(SWITCH).state == (STATE_ON if wanted else STATE_OFF)
