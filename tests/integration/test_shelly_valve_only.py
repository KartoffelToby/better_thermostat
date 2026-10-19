"""A BLU TRV's thermostat must not compete with BT's valve controller."""

from dataclasses import replace
import logging
from unittest.mock import patch

from homeassistant.components.climate import HVACMode
from homeassistant.core import Context
from homeassistant.data_entry_flow import FlowResultType
import pytest

from custom_components.better_thermostat.adapters import shelly
from custom_components.better_thermostat.utils import controlling
from custom_components.better_thermostat.utils.const import (
    CONF_CALIBRATION,
    CONF_CALIBRATION_MODE,
    CalibrationMode,
    CalibrationOutput,
)
from custom_components.better_thermostat.utils.controlling import (
    compute_control_cycle,
    control_trv,
    desired_diverges,
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
from .device_profiles import SHELLY_BLU_VALVE_TRV, TRV_ID, VALVE_TRV, OffsetChannel


@pytest.fixture(autouse=True)
def immediate_writes():
    """Exercise device policy without waiting for ordinary write spacing."""
    with patch(WRITE_BUDGET, 0.0):
        yield


async def _set_target(hass, target):
    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": BT_ENTITY, "temperature": target, "hvac_mode": "heat"},
        blocking=True,
    )


@pytest.mark.parametrize(
    "fake_trv",
    [
        SHELLY_BLU_VALVE_TRV,
        replace(
            SHELLY_BLU_VALVE_TRV,
            target_temperature=25.0,
            hvac_mode=HVACMode.OFF,
            offset_channel=OffsetChannel.NUMBER_ENTITY,
        ),
    ],
    indirect=True,
    ids=["no-target", "cached-target-and-offset"],
)
@pytest.mark.parametrize(
    "controller",
    [CalibrationMode.HEATING_POWER_CALIBRATION, CalibrationMode.MPC_CALIBRATION],
    ids=["heating-power", "mpc"],
)
async def test_heating_and_off_use_only_the_valve(hass, fake_trv, controller, caplog):
    """Heating opens the valve and OFF closes it without starting its thermostat."""
    set_room_sensor(hass, 18.0)
    caplog.set_level(
        logging.DEBUG, logger="custom_components.better_thermostat.events.trv"
    )
    entry = make_entry(replace(fake_trv.profile, calibration_mode=controller))
    with (
        patch(WRITE_BUDGET, 0.0),
        patch.object(
            controlling,
            "set_temperature",
            autospec=True,
            side_effect=controlling.set_temperature,
        ) as setpoint_write,
        patch.object(
            controlling,
            "check_target_temperature",
            autospec=True,
            side_effect=controlling.check_target_temperature,
        ) as setpoint_confirmation,
    ):
        await setup_entry(hass, entry)
        bt = await wait_for_startup(hass, entry)
        assert bt.real_trvs[TRV_ID].uses_valve_only_control
        # Initial device setup is distinct from runtime thermostat regulation.
        if fake_trv.offset_number:
            fake_trv.offset_number.set_value_calls.clear()
        await _set_target(hass, 23.0)
        valve = fake_trv.valve_number
        assert valve is not None
        assert await wait_for(hass, lambda: valve.native_value > 0)
        assert not desired_diverges(
            bt, *compute_control_cycle(bt, record=False, commit=False)
        )
        await shelly.set_calibration_offset(bt, TRV_ID, 2.0)
        await hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": BT_ENTITY, "hvac_mode": "off"},
            blocking=True,
        )
        assert await wait_for(hass, lambda: valve.native_value == 0)
        await hass.async_block_till_done()
        setpoint_write.assert_not_called()
        setpoint_confirmation.assert_not_called()
        assert "because this device has no system mode off" not in caplog.text
        assert fake_trv.set_temperature_calls == []
        assert fake_trv.set_hvac_mode_calls == []
        if fake_trv.offset_number:
            assert fake_trv.offset_number.set_value_calls == []
        assert not desired_diverges(
            bt, *compute_control_cycle(bt, record=False, commit=False)
        )
        valve._attr_native_value = 25.0
        valve.async_write_ha_state()
        assert desired_diverges(
            bt, *compute_control_cycle(bt, record=False, commit=False)
        )


@pytest.mark.parametrize(
    "fake_trv", [SHELLY_BLU_VALVE_TRV], indirect=True, ids=profile_id
)
async def test_device_settings_do_not_replace_room_settings(hass, fake_trv):
    """BT keeps its target and mode while accepting the device's temperature."""
    set_room_sensor(hass, 18.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    await _set_target(hass, 23.0)
    valve = fake_trv.valve_number
    assert valve is not None
    assert await wait_for(hass, lambda: valve.native_value > 0)
    trv = bt.real_trvs[TRV_ID]
    trv.last_internal_sensor_change = None
    fake_trv._attr_hvac_mode = HVACMode.OFF
    fake_trv._attr_target_temperature = 16.0
    fake_trv._attr_current_temperature = 20.5
    fake_trv.async_set_context(Context())
    fake_trv.async_write_ha_state()
    assert await wait_for(hass, lambda: trv.current_temperature == 20.5)
    await hass.async_block_till_done()
    assert bt.heat_target_temperature == 23.0
    assert bt.bt_hvac_mode == HVACMode.HEAT
    assert fake_trv.set_temperature_calls == []
    assert fake_trv.set_hvac_mode_calls == []


@pytest.mark.parametrize(
    "fake_trv",
    [
        replace(
            SHELLY_BLU_VALVE_TRV,
            calibration=output,
            calibration_mode=CalibrationMode.DEFAULT,
            target_temperature=19.0,
            current_temperature=22.0,
            hvac_modes=(HVACMode.OFF, HVACMode.HEAT),
            offset_channel=OffsetChannel.NUMBER_ENTITY,
        )
        for output in (
            CalibrationOutput.TARGET_TEMP_BASED,
            CalibrationOutput.LOCAL_BASED,
        )
    ],
    indirect=True,
    ids=["target-based", "offset-based"],
)
async def test_blu_thermostat_control_keeps_commands_and_device_reports(hass, fake_trv):
    """A discovered valve must not change a BLU's setpoint or offset control."""
    set_room_sensor(hass, 18.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    trv = bt.real_trvs[TRV_ID]
    assert trv.valve_position_writable is True
    assert not trv.uses_valve_only_control
    fake_trv.set_temperature_calls.clear()

    await _set_target(hass, 23.0)
    assert await wait_for(hass, lambda: fake_trv.set_temperature_calls)
    if fake_trv.profile.calibration == CalibrationOutput.LOCAL_BASED:
        assert await wait_for(hass, lambda: fake_trv.offset_number.native_value < 0)
    for mode in (HVACMode.OFF, HVACMode.HEAT):
        await hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": BT_ENTITY, "hvac_mode": mode},
            blocking=True,
        )
        assert await wait_for(hass, lambda: fake_trv.hvac_mode == mode)
        assert fake_trv.set_hvac_mode_calls[-1] == mode
        await hass.async_block_till_done()
        assert await wait_for(hass, lambda: not bt.ignore_states)

    fake_trv._attr_target_temperature = 25.0
    fake_trv.async_set_context(Context())
    fake_trv.async_write_ha_state()
    assert await wait_for(hass, lambda: bt.heat_target_temperature == 25.0)
    await hass.async_block_till_done()
    assert await wait_for(hass, lambda: not bt.ignore_states)
    fake_trv._attr_hvac_mode = HVACMode.OFF
    fake_trv.async_set_context(Context())
    fake_trv.async_write_ha_state()
    assert await wait_for(hass, lambda: bt.bt_hvac_mode == HVACMode.OFF)
    assert fake_trv.valve_number.set_value_calls == []


@pytest.mark.parametrize("fake_trv", [VALVE_TRV], indirect=True, ids=profile_id)
async def test_other_direct_valve_adapters_keep_thermostat_commands(hass, fake_trv):
    """A Zigbee2MQTT valve keeps its existing setpoint and mode writes."""
    set_room_sensor(hass, 18.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    assert not bt.real_trvs[TRV_ID].uses_valve_only_control
    fake_trv.set_temperature_calls.clear()
    await _set_target(hass, 23.0)
    assert await wait_for(hass, lambda: fake_trv.valve_number.native_value > 0)
    assert await wait_for(hass, lambda: fake_trv.set_temperature_calls)
    await hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {"entity_id": BT_ENTITY, "hvac_mode": "off"},
        blocking=True,
    )
    assert await wait_for(hass, lambda: fake_trv.hvac_mode == HVACMode.OFF)
    assert fake_trv.set_hvac_mode_calls[-1] == HVACMode.OFF
    assert fake_trv.valve_number.native_value == 0


@pytest.mark.parametrize(
    "fake_trv", [SHELLY_BLU_VALVE_TRV], indirect=True, ids=profile_id
)
async def test_open_window_closes_the_only_actuator(hass, fake_trv):
    """A window stop reaches a BLU valve without an HVAC OFF command."""
    set_room_sensor(hass, 18.0)
    hass.states.async_set(WINDOW_ID, "off")
    entry = make_entry(fake_trv.profile, with_window=True)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)
    await _set_target(hass, 23.0)
    valve = fake_trv.valve_number
    assert valve is not None
    assert await wait_for(hass, lambda: valve.native_value > 0)
    hass.states.async_set(WINDOW_ID, "on")
    assert await wait_for(hass, lambda: valve.native_value == 0)
    assert fake_trv.set_hvac_mode_calls == []


@pytest.mark.parametrize(
    "fake_trv", [SHELLY_BLU_VALVE_TRV], indirect=True, ids=profile_id
)
async def test_valve_outage_does_not_fall_back_to_the_thermostat(hass, fake_trv):
    """An unavailable valve must not re-enable the competing thermostat."""
    set_room_sensor(hass, 18.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    valve = fake_trv.valve_number
    assert valve is not None
    valve._attr_available = False
    valve.async_write_ha_state()
    with patch(WRITE_BUDGET, 0.0):
        await _set_target(hass, 23.0)
        await control_trv(bt, TRV_ID)
    assert fake_trv.set_temperature_calls == []
    assert fake_trv.set_hvac_mode_calls == []
    valve._attr_available = True
    valve.async_write_ha_state()
    with patch(WRITE_BUDGET, 0.0):
        await control_trv(bt, TRV_ID)
    assert await wait_for(hass, lambda: valve.native_value > 0)


@pytest.mark.parametrize(
    "fake_trv",
    [replace(SHELLY_BLU_VALVE_TRV, offset_channel=OffsetChannel.NUMBER_ENTITY)],
    indirect=True,
    ids=profile_id,
)
@pytest.mark.parametrize("options", [False, True], ids=["create", "options"])
@pytest.mark.parametrize("accepted_output", list(CalibrationOutput))
@pytest.mark.parametrize(
    "algorithm",
    [
        CalibrationMode.DEFAULT,
        CalibrationMode.AGGRESSIVE_CALIBRATION,
        CalibrationMode.NO_CALIBRATION,
    ],
)
async def test_flow_requires_an_algorithm_with_valve_output(
    hass, fake_trv, options, algorithm, accepted_output
):
    """A BLU valve configuration must not save a controller with no valve output."""
    set_room_sensor(hass, 18.0)
    if options:
        entry = make_entry(fake_trv.profile)
        await setup_entry(hass, entry)
        await wait_for_startup(hass, entry)
        manager = hass.config_entries.options
        result = await manager.async_init(entry.entry_id)
    else:
        manager = hass.config_entries.flow
        result = await manager.async_init(DOMAIN, context={"source": "user"})
    # The harness registers devices under its simulated integration. Discovery
    # selects Shelly here; probing and writing still use the real adapter.
    with patch(
        "custom_components.better_thermostat.config_flow.get_trv_intigration",
        return_value="shelly",
    ):
        result = await manager.async_configure(
            result["flow_id"],
            {
                "name": "BLU room",
                "thermostat": [TRV_ID],
                "temperature_sensor": SENSOR_ID,
            },
        )
    assert result["step_id"] == "advanced"
    result = await manager.async_configure(
        result["flow_id"],
        {
            CONF_CALIBRATION: CalibrationOutput.DIRECT_VALVE_BASED,
            CONF_CALIBRATION_MODE: algorithm,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "advanced"
    assert result["errors"] == {CONF_CALIBRATION_MODE: "valve_only_calibration_mode"}
    result = await manager.async_configure(
        result["flow_id"],
        {
            CONF_CALIBRATION: accepted_output,
            CONF_CALIBRATION_MODE: (
                CalibrationMode.HEATING_POWER_CALIBRATION
                if accepted_output == CalibrationOutput.DIRECT_VALVE_BASED
                else algorithm
            ),
        },
    )
    if options:
        assert result.get("errors", {}) == {}
        assert result["type"] is FlowResultType.CREATE_ENTRY
    else:
        assert result["step_id"] == "confirm"
        # The BLU reports HEAT only, so setup retains the existing confirmation.
        assert result["errors"] == {"base": "no_off_mode"}
