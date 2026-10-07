"""The PID numbers and the auto-tune switch show the learned state as it is now.

No entity of the integration is polled. The PID gains and the auto-tune flag
live in the state store and in no attribute of the thermostat, so a cycle
that changes only them changes no state the entities could follow; the
thermostat announces its learned state after every cycle and every reset
instead.
"""

from dataclasses import replace

from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from custom_components.better_thermostat.entity import (
    LEARNED_STATE_SIGNAL,
    announce_learned_state,
)
from custom_components.better_thermostat.utils.calibration.pid import (
    DEFAULT_PID_KP,
    build_pid_key,
)
from custom_components.better_thermostat.utils.const import (
    SERVICE_RESET_PID_LEARNINGS,
    CalibrationMode,
)
from custom_components.better_thermostat.utils.scheduler import request_control_cycle

from .conftest import (
    DOMAIN,
    build_devices,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for_startup,
)
from .device_profiles import GENERIC_HEAT_TRV

PID_TRV = replace(
    GENERIC_HEAT_TRV, calibration_mode=CalibrationMode.PID_CALIBRATION.value
)


def _entity_id(hass, bt, platform, suffix) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{bt.unique_id}_{PID_TRV.entity_id}_{suffix}"
    )
    assert entity_id is not None, suffix
    return entity_id


async def _started(hass):
    set_room_sensor(hass, 19.0)
    await build_devices(hass, PID_TRV)
    entry = make_entry(PID_TRV)
    await setup_entry(hass, entry)
    return await wait_for_startup(hass, entry)


def _stored_pid(bt):
    return bt.state_mgr.get_pid(build_pid_key(bt, PID_TRV.entity_id))


async def test_every_control_cycle_announces_the_learned_state(hass):
    bt = await _started(hass)
    announced = []
    async_dispatcher_connect(
        hass, LEARNED_STATE_SIGNAL.format(bt.unique_id), lambda: announced.append(1)
    )

    request_control_cycle(bt)
    await bt.control_queue_task.join()
    await hass.async_block_till_done()

    assert announced


async def test_a_gain_changed_in_the_store_shows_once_announced(hass):
    """The number follows the store although the thermostat's state is unchanged."""
    bt = await _started(hass)
    kp = _entity_id(hass, bt, "number", "pid_kp")
    pid_state = _stored_pid(bt)
    pid_state.pid_kp = 123.4
    bt.state_mgr.set_pid(build_pid_key(bt, PID_TRV.entity_id), pid_state)
    assert float(hass.states.get(kp).state) != 123.4

    announce_learned_state(hass, bt.unique_id)
    await hass.async_block_till_done()

    assert float(hass.states.get(kp).state) == 123.4


async def test_an_auto_tune_flag_changed_in_the_store_shows_once_announced(hass):
    bt = await _started(hass)
    switch = _entity_id(hass, bt, "switch", "pid_auto_tune")
    before = hass.states.get(switch).state
    pid_state = _stored_pid(bt)
    pid_state.auto_tune = before != "on"
    bt.state_mgr.set_pid(build_pid_key(bt, PID_TRV.entity_id), pid_state)

    announce_learned_state(hass, bt.unique_id)
    await hass.async_block_till_done()

    assert hass.states.get(switch).state != before


async def test_a_reset_shows_the_default_gain_at_once(hass):
    """Resetting the learnings returns the numbers to the defaults they now read."""
    bt = await _started(hass)
    kp = _entity_id(hass, bt, "number", "pid_kp")
    await hass.services.async_call(
        "number", "set_value", {"entity_id": kp, "value": 77.0}, blocking=True
    )
    await hass.async_block_till_done()
    assert float(hass.states.get(kp).state) == 77.0

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RESET_PID_LEARNINGS,
        {"entity_id": bt.entity_id, "apply_pid_defaults": False},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert float(hass.states.get(kp).state) == DEFAULT_PID_KP
