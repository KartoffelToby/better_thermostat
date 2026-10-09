"""The switch and number platforms against a running Home Assistant.

The child-lock switch reaches the device through the child-lock entity the
device's own integration exposes, a switch or a lock; the PID auto-tune
switch keeps its flag in the thermostat's learned state. Both platforms are
built on the thermostat entity and stay empty without it.
"""

from dataclasses import replace
import logging
from unittest.mock import patch

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    mock_restore_cache_with_extra_data,
)

from custom_components.better_thermostat import climate as climate_module
from custom_components.better_thermostat.utils.calibration.pid import (
    build_pid_key,
    resolve_unique_id,
)
from custom_components.better_thermostat.utils.const import CalibrationMode

from .conftest import (
    build_devices,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for_startup,
)
from .device_profiles import GENERIC_HEAT_TRV

SWITCH = "switch.bt_test_child_lock"
LOCKING_TRV = replace(
    GENERIC_HEAT_TRV, name="locking_trv", has_device_registry_entry=True
)
"""A head whose device carries a child-lock entity next to its thermostat."""


async def _give_the_device_a_child_lock(hass, domain: str, state: str) -> str:
    """Register a child-lock entity of ``domain`` on the head's device."""
    registry = er.async_get(hass)
    trv = registry.async_get(LOCKING_TRV.entity_id)
    assert trv is not None and trv.device_id is not None
    entity = registry.async_get_or_create(
        domain,
        "test",
        "fake_trv_child_lock",
        device_id=trv.device_id,
        suggested_object_id="fake_trv_child_lock",
    )
    hass.states.async_set(entity.entity_id, state)
    return entity.entity_id


async def test_a_device_switch_already_in_place_gets_no_command(hass):
    """The device's child-lock switch is commanded only where it differs.

    A switch-type child lock (as Zigbee2MQTT exposes it) that already
    reports the state the user picks is left alone; the opposite state is
    sent to it.
    """
    set_room_sensor(hass, 19.0)
    await build_devices(hass, LOCKING_TRV)
    device_switch = await _give_the_device_a_child_lock(hass, "switch", STATE_OFF)
    entry = make_entry(LOCKING_TRV)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    commands: list[str] = []
    hass.bus.async_listen(
        "call_service",
        lambda event: (
            commands.append(event.data["service"])
            if event.data["service_data"].get("entity_id") == device_switch
            else None
        ),
    )

    hass.states.async_set(device_switch, STATE_ON)
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": SWITCH}, blocking=True
    )
    await hass.async_block_till_done()
    assert commands == []

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": SWITCH}, blocking=True
    )
    await hass.async_block_till_done()

    assert commands == ["turn_off"]
    assert hass.states.get(SWITCH).state == STATE_OFF
    (trv,) = bt.real_trvs.values()
    assert trv.advanced["child_lock"] is False


async def test_a_refused_device_command_is_reported_and_the_switch_holds(hass, caplog):
    """A device integration that refuses the command leaves a warning.

    The switch keeps the state the user picked, so the next command the
    thermostat sends carries it.
    """
    set_room_sensor(hass, 19.0)
    await build_devices(hass, LOCKING_TRV)
    device_lock = await _give_the_device_a_child_lock(hass, "lock", "unlocked")
    entry = make_entry(LOCKING_TRV)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)

    with caplog.at_level(logging.WARNING):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": SWITCH}, blocking=True
        )
        await hass.async_block_till_done()

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING and "child lock" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert device_lock in warnings[0]
    assert hass.states.get(SWITCH).state == STATE_ON
    (trv,) = bt.real_trvs.values()
    assert trv.advanced["child_lock"] is True


async def test_a_switch_saved_unavailable_restores_the_option(hass):
    """A switch saved without a shown state restores to the configured option.

    It was saved as unavailable with no earlier state recorded next to it,
    which stands for no setting, so the option holds.
    """
    set_room_sensor(hass, 19.0)
    entry = make_entry(GENERIC_HEAT_TRV)
    entry.data["thermostat"][0]["advanced"]["child_lock"] = True
    mock_restore_cache_with_extra_data(
        hass, [(State(SWITCH, STATE_UNAVAILABLE), {"configured": True})]
    )
    await build_devices(hass, GENERIC_HEAT_TRV)

    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)

    assert hass.states.get(SWITCH).state == STATE_ON
    (trv,) = bt.real_trvs.values()
    assert trv.advanced["child_lock"] is True


PID_TRV = replace(
    GENERIC_HEAT_TRV, calibration_mode=CalibrationMode.PID_CALIBRATION.value
)


def _auto_tune_switch(hass, entry) -> str:
    registry = er.async_get(hass)
    (entity_id,) = [
        reg.entity_id
        for reg in er.async_entries_for_config_entry(registry, entry.entry_id)
        if reg.domain == "switch" and reg.unique_id.endswith("_pid_auto_tune")
    ]
    return entity_id


async def test_the_auto_tune_switch_sets_the_learned_flag(hass):
    """Switching auto-tune off and on again is what the PID state holds."""
    set_room_sensor(hass, 19.0)
    await build_devices(hass, PID_TRV)
    entry = make_entry(PID_TRV)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    assert bt.state_mgr is not None
    switch = _auto_tune_switch(hass, entry)
    key = build_pid_key(bt, PID_TRV.entity_id)

    for service, expected in (("turn_off", False), ("turn_on", True)):
        await hass.services.async_call(
            "switch", service, {"entity_id": switch}, blocking=True
        )
        await hass.async_block_till_done()

        assert hass.states.get(switch).state == (STATE_ON if expected else STATE_OFF)
        assert bt.state_mgr.state.pid[key].auto_tune is expected


async def test_the_auto_tune_switch_is_kept_after_a_pid_reset(hass):
    """A toggle with no learned PID state left starts that state with it.

    A reset drops every PID state of the head; the flag the user sets
    afterwards is not lost for want of a state to hold it.
    """
    set_room_sensor(hass, 19.0)
    await build_devices(hass, PID_TRV)
    entry = make_entry(PID_TRV)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    assert bt.state_mgr is not None
    switch = _auto_tune_switch(hass, entry)
    bt.state_mgr.reset_pid_states(f"{resolve_unique_id(bt)}:{PID_TRV.entity_id}:")

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": switch}, blocking=True
    )
    await hass.async_block_till_done()

    assert hass.states.get(switch).state == STATE_OFF
    assert bt.state_mgr.state.pid[build_pid_key(bt, PID_TRV.entity_id)].auto_tune is (
        False
    )


async def test_no_thermostat_means_no_switches_or_numbers(hass, caplog):
    """An entry whose thermostat entity failed to build offers no controls.

    The switches and numbers act on the thermostat; without it the number
    platform says why it adds nothing.
    """
    set_room_sensor(hass, 19.0)
    await build_devices(hass, PID_TRV)
    entry = make_entry(PID_TRV)

    with (
        caplog.at_level(logging.WARNING),
        patch.object(
            climate_module,
            "BetterThermostat",
            side_effect=RuntimeError("thermostat could not be built"),
        ),
    ):
        await setup_entry(hass, entry)

    assert entry.runtime_data.climate is None
    registry = er.async_get(hass)
    assert [
        reg.entity_id
        for reg in er.async_entries_for_config_entry(registry, entry.entry_id)
        if reg.domain in ("switch", "number")
    ] == []
    assert any(
        "Numbers will not be added" in record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
    )
