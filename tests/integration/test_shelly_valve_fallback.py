"""Valve-only devices need limited heating on sensor loss and closure on invalid setup."""

from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch

from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
)

from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.fsm.control_mode import (
    ControlMode,
    LadderParams,
)
from custom_components.better_thermostat.utils.const import CalibrationMode
from custom_components.better_thermostat.utils.controlling import control_trv
from custom_components.better_thermostat.utils.helpers import (
    entry_issue_id,
    entry_settings,
)
from custom_components.better_thermostat.utils.watcher import (
    check_and_update_degraded_mode,
)

from .conftest import (
    BT_ENTITY,
    DOMAIN,
    SENSOR_ID,
    WINDOW_ID,
    WRITE_BUDGET,
    make_entry,
    profile_id,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import SHELLY_BLU_VALVE_TRV, TRV_ID, VALVE_TRV


async def _heat(hass):
    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": BT_ENTITY, "temperature": 23.0, "hvac_mode": "heat"},
        blocking=True,
    )


@pytest.mark.parametrize(
    "fake_trv",
    [replace(SHELLY_BLU_VALVE_TRV, calibration_mode=CalibrationMode.NO_CALIBRATION)],
    indirect=True,
    ids=profile_id,
)
async def test_released_valve_is_left_alone_until_control_is_taken_back(hass, fake_trv):
    """A stored invalid controller must not take a released device away from its user."""
    set_room_sensor(hass, 18.0)
    mock_restore_cache(
        hass,
        [State(BT_ENTITY, "heat", {"temperature": 23.0, "devices_released": True})],
    )
    valve = fake_trv.valve_number
    valve._attr_native_value = 72.0
    valve.async_write_ha_state()
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    assert bt.devices_released
    await control_trv(bt, TRV_ID)
    issue_id = entry_issue_id(entry.entry_id, "invalid_valve_calibration", TRV_ID)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    assert valve.native_value == 72.0
    assert valve.set_value_calls == []
    assert fake_trv.set_temperature_calls == []
    assert fake_trv.set_hvac_mode_calls == []

    await bt.async_set_devices_released(False)
    assert await wait_for(hass, lambda: valve.native_value == 0)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None


@pytest.mark.parametrize(
    "fake_trv",
    [
        replace(SHELLY_BLU_VALVE_TRV, calibration_mode=mode)
        for mode in (
            CalibrationMode.DEFAULT,
            CalibrationMode.AGGRESSIVE_CALIBRATION,
            CalibrationMode.NO_CALIBRATION,
            "unknown_controller",
        )
    ],
    indirect=True,
    ids=lambda profile: str(profile.calibration_mode),
)
@pytest.mark.parametrize("fail_first_close", [False, True])
async def test_invalid_stored_controller_closes_and_reports_until_reconfigured(
    hass, fake_trv, fail_first_close
):
    """An old incompatible configuration must not leave a valve open or choose an algorithm."""
    set_room_sensor(hass, 18.0)
    hass.states.async_set(WINDOW_ID, "off")
    valve = fake_trv.valve_number
    valve._attr_native_value = 72.0
    valve.async_write_ha_state()
    entry = make_entry(fake_trv.profile, with_window=True)
    issue_id = entry_issue_id(entry.entry_id, "invalid_valve_calibration", TRV_ID)
    write = valve.async_set_native_value
    attempts = []

    async def sometimes_unreachable(value):
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
        attempts.append(value)
        if fail_first_close and len(attempts) == 1:
            raise HomeAssistantError("Valve did not answer")
        await write(value)

    with patch.object(
        valve, "async_set_native_value", side_effect=sometimes_unreachable
    ):
        await setup_entry(hass, entry)
        bt = await wait_for_startup(hass, entry)
        assert await wait_for(hass, lambda: valve.native_value == 0)
    assert attempts == ([0.0, 0.0] if fail_first_close else [0.0])
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.is_fixable is False
    assert issue.translation_key == "invalid_valve_calibration"
    assert await wait_for(hass, lambda: valve.native_value == 0)
    assert valve.set_value_calls == [0.0]

    await _heat(hass)
    await control_trv(bt, TRV_ID)
    assert valve.native_value == 0
    await hass.services.async_call(
        "climate",
        "set_preset_mode",
        {"entity_id": BT_ENTITY, "preset_mode": "boost"},
        blocking=True,
    )
    await control_trv(bt, TRV_ID)
    assert valve.native_value == 0
    # A configuration error still requires closure if all temperatures are lost.
    clock = FakeClock(monotonic_value=bt.clock.monotonic())
    bt.clock = clock
    fake_trv._attr_current_temperature = None
    fake_trv.async_write_ha_state()
    hass.states.async_set(SENSOR_ID, "unavailable")
    await hass.async_block_till_done()
    await check_and_update_degraded_mode(bt)
    clock.advance(LadderParams().down_debounce_seconds + 1)
    await check_and_update_degraded_mode(bt)
    assert bt.kernel_state.control_mode.mode == ControlMode.HOLD
    await control_trv(bt, TRV_ID)
    assert valve.native_value == 0
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    set_room_sensor(hass, 18.0)
    fake_trv._attr_current_temperature = 19.5
    fake_trv.async_write_ha_state()
    await hass.async_block_till_done()
    await check_and_update_degraded_mode(bt)
    clock.advance(LadderParams().up_stability_seconds + 1)
    await check_and_update_degraded_mode(bt)
    assert bt.kernel_state.control_mode.mode == ControlMode.OPTIMAL
    for action in ("off", "window"):
        valve._attr_native_value = 72.0
        valve.async_write_ha_state()
        if action == "off":
            await hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": BT_ENTITY, "hvac_mode": "off"},
                blocking=True,
            )
        else:
            hass.states.async_set(WINDOW_ID, "on")
            await _heat(hass)
        await control_trv(bt, TRV_ID)
        assert valve.native_value == 0
    assert all(value == 0 for value in valve.set_value_calls)
    assert fake_trv.set_temperature_calls == []
    assert fake_trv.set_hvac_mode_calls == []
    settings = deepcopy(entry_settings(entry))
    thermostats = settings["thermostat"]
    assert isinstance(thermostats, list)
    advanced = thermostats[0]["advanced"]
    assert advanced["calibration_mode"] == fake_trv.profile.calibration_mode
    advanced["calibration_mode"] = CalibrationMode.HEATING_POWER_CALIBRATION
    hass.config_entries.async_update_entry(entry, options=settings)
    await hass.async_block_till_done()
    await wait_for_startup(hass, entry)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    hass.states.async_set(WINDOW_ID, "off")
    with patch(WRITE_BUDGET, 0.0):
        await _heat(hass)
        assert await wait_for(hass, lambda: valve.native_value > 0)


@pytest.mark.parametrize(
    "fake_trv", [SHELLY_BLU_VALVE_TRV, VALVE_TRV], indirect=True, ids=profile_id
)
async def test_hold_limits_the_exclusive_valve_and_recovery_resumes_control(
    hass, fake_trv
):
    """Loss of all temperatures must preserve limited heating without a device thermostat."""
    set_room_sensor(hass, 18.0)
    door_entity_id = "binary_sensor.door"
    hass.states.async_set(WINDOW_ID, "off")
    hass.states.async_set(door_entity_id, "off")
    entry = make_entry(fake_trv.profile, with_window=True)
    entry_data = dict(entry.data)
    entry_data.update(
        door_sensors=door_entity_id, door_off_delay=0, door_off_delay_after=0
    )
    entry = MockConfigEntry(
        domain=DOMAIN, version=entry.version, data=entry_data, title=entry.title
    )
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    with patch(WRITE_BUDGET, 0.0):
        await _heat(hass)
        valve = fake_trv.valve_number
        assert await wait_for(hass, lambda: valve.native_value > 25)
        opening = valve.native_value
        clock = FakeClock(monotonic_value=bt.clock.monotonic())
        bt.clock = clock
        fake_trv._attr_current_temperature = None
        fake_trv.async_write_ha_state()
        hass.states.async_set(SENSOR_ID, "unavailable")
        await hass.async_block_till_done()
        await check_and_update_degraded_mode(bt)
        clock.advance(LadderParams().down_debounce_seconds + 1)
        await check_and_update_degraded_mode(bt)
        assert bt.kernel_state.control_mode.mode == ControlMode.HOLD
        await control_trv(bt, TRV_ID)
        assert valve.native_value == (
            25 if bt.real_trvs[TRV_ID].uses_valve_only_control else opening
        )
        if bt.real_trvs[TRV_ID].uses_valve_only_control:
            assert fake_trv.set_temperature_calls == []
            assert fake_trv.set_hvac_mode_calls == []
        set_room_sensor(hass, 18.0)
        fake_trv._attr_current_temperature = 19.5
        fake_trv.async_write_ha_state()
        await hass.async_block_till_done()
        await check_and_update_degraded_mode(bt)
        if bt.real_trvs[TRV_ID].uses_valve_only_control:
            # Temperatures have returned, but HOLD's stability period has not
            # elapsed. Boost must not replace the emergency opening.
            await hass.services.async_call(
                "climate",
                "set_preset_mode",
                {"entity_id": BT_ENTITY, "preset_mode": "boost"},
                blocking=True,
            )
            await control_trv(bt, TRV_ID)
            assert valve.native_value == 25
            await hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": BT_ENTITY, "hvac_mode": "off"},
                blocking=True,
            )
            await control_trv(bt, TRV_ID)
            assert valve.native_value == 0
            await _heat(hass)
            await control_trv(bt, TRV_ID)
            assert valve.native_value == 25
            for contact_entity_id in (WINDOW_ID, door_entity_id):
                hass.states.async_set(contact_entity_id, "on")
                await hass.async_block_till_done()
                await control_trv(bt, TRV_ID)
                assert valve.native_value == 0
                hass.states.async_set(contact_entity_id, "off")
                await hass.async_block_till_done()
                await control_trv(bt, TRV_ID)
                assert valve.native_value == 25
        clock.advance(LadderParams().up_stability_seconds + 1)
        await check_and_update_degraded_mode(bt)
        assert bt.kernel_state.control_mode.mode == ControlMode.OPTIMAL
        await control_trv(bt, TRV_ID)
        assert await wait_for(hass, lambda: valve.native_value > 25)
