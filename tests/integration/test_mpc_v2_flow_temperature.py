"""MPC v2 models the room with a fixed flow temperature.

The settings form offers no flow temperature. An entry that stores one runs
on the default all the same, and the next save of the options drops it.
"""

from dataclasses import replace
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.better_thermostat.utils.calibration.plant_preset import (
    DEFAULT_FLOW_TEMPERATURE,
)
from custom_components.better_thermostat.utils.const import (
    CONF_TEMPERATURE_SENSOR,
    CONF_THERMOSTAT,
    CalibrationMode,
)
from custom_components.better_thermostat.utils.scheduler import request_control_cycle

from .conftest import (
    BT_ENTITY,
    DOMAIN,
    SENSOR_ID,
    WRITE_BUDGET,
    build_devices,
    click_through_the_options,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import GENERIC_HEAT_TRV, VALVE_TRV

STORED_KEY = "mpc_v2_flow_temperature"

MPC_V2_VALVE_TRV = replace(
    VALVE_TRV,
    name="valve_trv_mpc_v2",
    calibration_mode=CalibrationMode.MPC_V2_CALIBRATION.value,
)


async def test_the_settings_form_offers_no_flow_temperature(hass):
    """The advanced step lists every MPC v2 option but the flow temperature."""
    set_room_sensor(hass, 19.0)
    (trv,) = await build_devices(hass, GENERIC_HEAT_TRV)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Flow Room",
            CONF_THERMOSTAT: [trv.entity_id],
            CONF_TEMPERATURE_SENSOR: SENSOR_ID,
        },
    )

    assert result["step_id"] == "advanced", result
    fields = [str(marker) for marker in result["data_schema"].schema]
    assert "mpc_v2_plant_preset" in fields
    assert STORED_KEY not in fields


def _entry_with_flow_temperature(profile, stored: float) -> MockConfigEntry:
    """Return the entry ``make_entry`` builds, with a stored flow temperature."""
    template = make_entry(profile)
    data = {
        **template.data,
        CONF_THERMOSTAT: [
            {**bundle, "advanced": dict(bundle["advanced"])}
            for bundle in template.data[CONF_THERMOSTAT]
        ],
    }
    data[CONF_THERMOSTAT][0]["advanced"][STORED_KEY] = stored
    return MockConfigEntry(domain=DOMAIN, version=18, data=data, title=template.title)


async def _live_controllers(hass, bt, target: float):
    """Heat the room toward ``target`` and return the live MPC v2 states."""
    with patch(WRITE_BUDGET, 0.0):
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": BT_ENTITY, "temperature": target},
            blocking=True,
        )
        request_control_cycle(bt)
        await bt.control_queue_task.join()
        await hass.async_block_till_done()
    assert await wait_for(hass, lambda: bool(bt.state_mgr._mpc_v2_live))
    live = dict(bt.state_mgr._mpc_v2_live)
    assert all(state.controller is not None for state in live.values())
    return live


@pytest.mark.parametrize("fake_trv", [MPC_V2_VALVE_TRV], indirect=True)
async def test_a_stored_flow_temperature_is_ignored_and_dropped(hass, fake_trv):
    """The controller models 65 °C, and saving the options removes the key."""
    set_room_sensor(hass, 17.0)
    entry = _entry_with_flow_temperature(fake_trv.profile, 38.0)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)

    live = await _live_controllers(hass, bt, 24.0)

    assert {
        state.controller.plant_coarse.params.T_water for state in live.values()
    } == {DEFAULT_FLOW_TEMPERATURE}

    await click_through_the_options(hass, entry)

    assert all(
        STORED_KEY not in bundle["advanced"]
        for bundle in entry.options[CONF_THERMOSTAT]
    )
