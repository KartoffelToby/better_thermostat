"""The optional regulation interlock through Home Assistant."""

from dataclasses import replace
from unittest.mock import patch

from homeassistant.components.climate import HVACAction, HVACMode
from homeassistant.core import Context
import pytest

from custom_components.better_thermostat.utils.const import CalibrationMode

from .conftest import (
    BT_ENTITY,
    WRITE_BUDGET,
    build_devices,
    make_entry,
    profile_id,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import GENERIC_HEAT_TRV, VALVE_TRV

PAUSE_SENSOR = "input_boolean.regulation_pause"
NO_OFF_TRV = replace(GENERIC_HEAT_TRV, name="pause_no_off", hvac_modes=(HVACMode.HEAT,))


@pytest.mark.parametrize(
    "fake_trv",
    [
        replace(
            VALVE_TRV, name=f"pause_passive_{mode.value}", calibration_mode=mode.value
        )
        for mode in (
            CalibrationMode.NO_CALIBRATION,
            CalibrationMode.DEFAULT,
            CalibrationMode.AGGRESSIVE_CALIBRATION,
        )
    ],
    indirect=True,
    ids=profile_id,
)
async def test_pause_restores_thermostat_controlled_valve(hass, fake_trv):
    """A thermostat-driven valve closes on pause and resumes its previous position."""
    set_room_sensor(hass, 17.0)
    hass.states.async_set(PAUSE_SENSOR, "off")
    entry = make_entry(fake_trv.profile, pause_sensor_entity_id=PAUSE_SENSOR)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    valve = fake_trv.valve_number
    assert valve is not None
    with patch(WRITE_BUDGET, 0.0):
        await bt.async_set_hvac_mode(HVACMode.HEAT)
        await bt.async_set_temperature(temperature=19.0)
        assert await wait_for(hass, lambda: fake_trv.hvac_mode == HVACMode.HEAT)
        assert await wait_for(hass, lambda: not bt.ignore_states)
        valve._attr_native_value = 60.0
        valve.async_write_ha_state()
        valve.set_value_calls.clear()
        hass.states.async_set(PAUSE_SENSOR, "on")
        assert await wait_for(hass, lambda: fake_trv.hvac_mode == HVACMode.OFF)
        assert await wait_for(hass, lambda: valve.native_value == 0.0)
        assert await wait_for(hass, lambda: not bt.ignore_states)
        hass.states.async_set(PAUSE_SENSOR, "off")
        assert await wait_for(hass, lambda: fake_trv.hvac_mode == HVACMode.HEAT)
        assert await wait_for(hass, lambda: valve.native_value == 60.0)
        assert await wait_for(hass, lambda: not bt.ignore_states)
        assert valve.set_value_calls == [0.0, 60.0]
        assert valve.native_value == 60.0


@pytest.mark.parametrize(
    "fake_trv",
    [
        replace(
            VALVE_TRV, name="pause_passive_reload", calibration_mode="no_calibration"
        )
    ],
    indirect=True,
    ids=profile_id,
)
async def test_paused_thermostat_controlled_valve_restores_after_reload(hass, fake_trv):
    """The pre-pause position survives a reload while the sensor stays on."""
    set_room_sensor(hass, 17.0)
    hass.states.async_set(PAUSE_SENSOR, "off")
    entry = make_entry(fake_trv.profile, pause_sensor_entity_id=PAUSE_SENSOR)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    valve = fake_trv.valve_number
    assert valve is not None
    with patch(WRITE_BUDGET, 0.0):
        await bt.async_set_hvac_mode(HVACMode.HEAT)
        await bt.async_set_temperature(temperature=19.0)
        assert await wait_for(hass, lambda: not bt.ignore_states)
        valve._attr_native_value = 60.0
        valve.async_write_ha_state()
        hass.states.async_set(PAUSE_SENSOR, "on")
        assert await wait_for(hass, lambda: valve.native_value == 0)
        assert await wait_for(hass, lambda: not bt.ignore_states)
        assert await hass.config_entries.async_reload(entry.entry_id)
        bt = await wait_for_startup(hass, entry)
        assert bt.regulation_paused
        assert valve.native_value == 0
        hass.states.async_set(PAUSE_SENSOR, "off")
        assert await wait_for(hass, lambda: valve.native_value == 60.0)


@pytest.mark.parametrize(
    "fake_trv",
    [
        replace(
            VALVE_TRV, name="pause_unknown_reload", calibration_mode="no_calibration"
        )
    ],
    indirect=True,
    ids=profile_id,
)
async def test_unknown_pre_pause_valve_position_survives_reload(hass, fake_trv):
    """A paused zero is not mistaken for an unknown original position after reload."""
    set_room_sensor(hass, 17.0)
    hass.states.async_set(PAUSE_SENSOR, "off")
    entry = make_entry(fake_trv.profile, pause_sensor_entity_id=PAUSE_SENSOR)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    valve = fake_trv.valve_number
    assert valve is not None
    with patch(WRITE_BUDGET, 0.0):
        await bt.async_set_hvac_mode(HVACMode.HEAT)
        assert await wait_for(hass, lambda: not bt.ignore_states)
        hass.states.async_set(valve.entity_id, "unknown")
        hass.states.async_set(PAUSE_SENSOR, "on")
        assert await wait_for(
            hass, lambda: bt.real_trvs[fake_trv.entity_id].pause_valve_restore_captured
        )
        assert await wait_for(hass, lambda: valve.native_value == 0)
        assert await wait_for(hass, lambda: not bt.ignore_states)
        saved_valves = bt.extra_state_attributes["pause_valve_restore"]
        assert isinstance(saved_valves, dict)
        assert saved_valves[fake_trv.entity_id] is None
        assert await hass.config_entries.async_reload(entry.entry_id)
        bt = await wait_for_startup(hass, entry)
        trv = bt.real_trvs[fake_trv.entity_id]
        assert trv.pause_valve_restore_captured
        assert trv.pause_valve_restore_percent is None
        valve.set_value_calls.clear()
        hass.states.async_set(PAUSE_SENSOR, "off")
        assert await wait_for(hass, lambda: fake_trv.hvac_mode == HVACMode.HEAT)
        assert await wait_for(hass, lambda: not bt.ignore_states)
        assert valve.set_value_calls == []


@pytest.mark.parametrize(
    "fake_trv",
    [
        replace(VALVE_TRV, name=f"pause_{mode.value}", calibration_mode=mode.value)
        for mode in (
            CalibrationMode.HEATING_POWER_CALIBRATION,
            CalibrationMode.PID_CALIBRATION,
            CalibrationMode.TPI_CALIBRATION,
            CalibrationMode.MPC_CALIBRATION,
            CalibrationMode.MPC_V2_CALIBRATION,
        )
    ],
    indirect=True,
    ids=profile_id,
)
async def test_pause_resume_reopens_direct_valve(hass, fake_trv):
    """The valve resumes heating when the pause clears in a cold room."""
    set_room_sensor(hass, 17.0)
    hass.states.async_set(PAUSE_SENSOR, "off")
    entry = make_entry(fake_trv.profile, pause_sensor_entity_id=PAUSE_SENSOR)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    valve = fake_trv.valve_number
    assert valve is not None
    with patch(WRITE_BUDGET, 0.0):
        await bt.async_set_hvac_mode(HVACMode.HEAT)
        await bt.async_set_temperature(temperature=19.0)
        assert await wait_for(hass, lambda: valve.native_value > 0)
        hass.states.async_set(PAUSE_SENSOR, "on")
        assert await wait_for(hass, lambda: valve.native_value == 0)
        assert await wait_for(hass, lambda: not bt.ignore_states)
        hass.states.async_set(PAUSE_SENSOR, "off")
        assert await wait_for(hass, lambda: valve.native_value > 0), (
            valve.set_value_calls,
            bt.real_trvs[fake_trv.entity_id].calibration_balance,
        )


@pytest.mark.parametrize("fake_trv", [VALVE_TRV], indirect=True, ids=profile_id)
@pytest.mark.parametrize("restart_while_paused", [False, True])
async def test_direct_valve_recovers_after_reload(hass, fake_trv, restart_while_paused):
    """Reload keeps a pause closed and resumes heating after the signal clears."""
    set_room_sensor(hass, 17.0)
    hass.states.async_set(PAUSE_SENSOR, "off")
    entry = make_entry(fake_trv.profile, pause_sensor_entity_id=PAUSE_SENSOR)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    valve = fake_trv.valve_number
    assert valve is not None
    with patch(WRITE_BUDGET, 0.0):
        await bt.async_set_hvac_mode(HVACMode.HEAT)
        await bt.async_set_temperature(temperature=19.0)
        assert await wait_for(hass, lambda: valve.native_value > 0)
        hass.states.async_set(PAUSE_SENSOR, "on")
        assert await wait_for(hass, lambda: valve.native_value == 0)
        assert await wait_for(hass, lambda: not bt.ignore_states)
        if not restart_while_paused:
            hass.states.async_set(PAUSE_SENSOR, "off")
            assert await wait_for(hass, lambda: valve.native_value > 0)
        assert await hass.config_entries.async_reload(entry.entry_id)
        bt = await wait_for_startup(hass, entry)
        if restart_while_paused:
            assert bt.regulation_paused
            assert valve.native_value == 0
            hass.states.async_set(PAUSE_SENSOR, "off")
        assert await wait_for(hass, lambda: valve.native_value > 0)


@pytest.mark.parametrize("fake_trv", [NO_OFF_TRV], indirect=True, ids=profile_id)
async def test_paused_knob_turn_restores_minimum_setpoint(hass, fake_trv):
    """A no-OFF TRV returns to its minimum after a physical knob turn."""
    set_room_sensor(hass, 18.0)
    hass.states.async_set(PAUSE_SENSOR, "off")
    entry = make_entry(fake_trv.profile, pause_sensor_entity_id=PAUSE_SENSOR)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    await bt.async_set_hvac_mode(HVACMode.HEAT)
    hass.states.async_set(PAUSE_SENSOR, "on")
    assert await wait_for(
        hass, lambda: fake_trv.set_temperature_calls[-1:] == [fake_trv.profile.min_temp]
    )
    assert await wait_for(
        hass,
        lambda: (
            not bt.ignore_states
            and not bt.real_trvs[fake_trv.entity_id].ignore_trv_states
        ),
    )
    target = bt.heat_target_temperature
    fake_trv.set_temperature_calls.clear()
    fake_trv._attr_native_target_temperature = 27.0
    fake_trv.async_set_context(Context())
    fake_trv.async_write_ha_state()

    assert await wait_for(
        hass, lambda: fake_trv.set_temperature_calls[-1:] == [fake_trv.profile.min_temp]
    )
    assert bt.heat_target_temperature == target
    assert bt.bt_hvac_mode == HVACMode.HEAT


@pytest.mark.parametrize("fake_trv", [GENERIC_HEAT_TRV], indirect=True, ids=profile_id)
@pytest.mark.parametrize("resumed_state", ["off", "unknown", "unavailable", None])
async def test_pause_sends_off_and_resume_restores_heating(
    hass, fake_trv, resumed_state
):
    """The interlock controls the device while preserving the room mode."""
    set_room_sensor(hass, 18.0)
    hass.states.async_set(PAUSE_SENSOR, "off")
    entry = make_entry(fake_trv.profile, pause_sensor_entity_id=PAUSE_SENSOR)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    await bt.async_set_hvac_mode(HVACMode.HEAT)
    assert await wait_for(hass, lambda: fake_trv.set_temperature_calls)

    hass.states.async_set(PAUSE_SENSOR, "on")
    assert await wait_for(
        hass, lambda: fake_trv.set_hvac_mode_calls[-1:] == [HVACMode.OFF]
    )
    assert bt.bt_hvac_mode == HVACMode.HEAT

    if resumed_state is None:
        hass.states.async_remove(PAUSE_SENSOR)
    else:
        hass.states.async_set(PAUSE_SENSOR, resumed_state)
    assert await wait_for(
        hass, lambda: fake_trv.set_hvac_mode_calls[-1:] == [HVACMode.HEAT]
    )


@pytest.mark.parametrize("resumed_state", ["off", "unknown", "unavailable", None])
async def test_pause_signal_and_resume(hass, resumed_state):
    """Only on pauses; other states resume without changing the room intent."""
    set_room_sensor(hass, 18.0)
    hass.states.async_set(PAUSE_SENSOR, "off")
    await build_devices(hass, GENERIC_HEAT_TRV)
    entry = make_entry(GENERIC_HEAT_TRV, pause_sensor_entity_id=PAUSE_SENSOR)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    mode, target = bt.bt_hvac_mode, bt.heat_target_temperature

    hass.states.async_set(PAUSE_SENSOR, "on")
    await hass.async_block_till_done()
    assert bt.regulation_paused
    assert bt.hvac_action == HVACAction.IDLE
    assert bt.bt_hvac_mode == mode
    assert bt.heat_target_temperature == target
    assert hass.states.get(BT_ENTITY).attributes["regulation_paused"] is True

    if resumed_state is None:
        hass.states.async_remove(PAUSE_SENSOR)
    else:
        hass.states.async_set(PAUSE_SENSOR, resumed_state)
    await hass.async_block_till_done()
    assert not bt.regulation_paused
    assert bt.bt_hvac_mode == mode
    assert bt.heat_target_temperature == target
    assert hass.states.get(BT_ENTITY).attributes["regulation_paused"] is False


@pytest.mark.parametrize("initial_state", [None, "unknown", "unavailable"])
async def test_missing_sensor_does_not_pause_on_startup(hass, initial_state):
    """An absent or unreadable sensor does not pause regulation at startup."""
    set_room_sensor(hass, 18.0)
    if initial_state is not None:
        hass.states.async_set(PAUSE_SENSOR, initial_state)
    await build_devices(hass, GENERIC_HEAT_TRV)
    entry = make_entry(GENERIC_HEAT_TRV, pause_sensor_entity_id=PAUSE_SENSOR)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    assert not bt.regulation_paused
    hass.states.async_set(PAUSE_SENSOR, "on")
    assert await wait_for(hass, lambda: bt.regulation_paused)
