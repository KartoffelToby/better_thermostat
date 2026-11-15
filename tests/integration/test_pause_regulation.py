"""The optional regulation interlock through Home Assistant."""

from homeassistant.components.climate import HVACAction, HVACMode
import pytest

from .conftest import (
    BT_ENTITY,
    build_devices,
    make_entry,
    profile_id,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import GENERIC_HEAT_TRV

PAUSE_SENSOR = "input_boolean.regulation_pause"


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
    assert hass.states.get(BT_ENTITY).attributes["processing_paused"] is True

    if resumed_state is None:
        hass.states.async_remove(PAUSE_SENSOR)
    else:
        hass.states.async_set(PAUSE_SENSOR, resumed_state)
    await hass.async_block_till_done()
    assert not bt.regulation_paused
    assert bt.bt_hvac_mode == mode
    assert bt.heat_target_temperature == target
    assert hass.states.get(BT_ENTITY).attributes["processing_paused"] is False


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
