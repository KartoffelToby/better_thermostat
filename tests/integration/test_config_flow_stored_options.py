"""The options the config and options flows write, key for key.

The entry a flow writes is read back by this version, by a downgrade to the
1.9 line, and by every later migration, so its exact shape is part of the
contract: which keys are stored, in which order, with which value types. A
test that reads one key at a time cannot tell an entry that gained, lost or
reordered a key from one that did not. These tests run both flows on inputs
that touch every field and compare the whole stored entry, serialised the way
Home Assistant stores it, with a frozen expectation.
"""

import json

from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import (
    DOMAIN,
    SENSOR_ID,
    WINDOW_ID,
    accepted_form,
    build_devices,
    set_room_sensor,
)
from .device_profiles import GENERIC_HEAT_TRV, SPARE_HEAT_TRV, SPARE_TRV_ID, TRV_ID

ENTRY_NAME = "Stored Room"


def _serialised(options) -> str:
    """Return the options the way the entry store writes them."""
    return json.dumps(dict(options))


async def _submit_all(hass, manager, flow_id, result, submissions):
    """Submit one step per item of ``submissions`` on top of the form's pre-fill."""
    for overrides in submissions:
        assert result["type"] is FlowResultType.FORM, result
        result = await manager.async_configure(
            flow_id, accepted_form(result, **overrides)
        )
    return result


async def _run_create_flow(hass, user_input, advanced_steps, refused_first=None):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    flow_id = result["flow_id"]
    if refused_first is not None:
        result = await hass.config_entries.flow.async_configure(flow_id, refused_first)
        assert result["step_id"] == "user", result
        assert result["errors"], result
    result = await hass.config_entries.flow.async_configure(flow_id, user_input)
    result = await _submit_all(
        hass, hass.config_entries.flow, flow_id, result, advanced_steps
    )
    assert result["step_id"] == "confirm", result
    result = await hass.config_entries.flow.async_configure(flow_id, {})
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    await hass.async_block_till_done()
    (entry,) = hass.config_entries.async_entries(DOMAIN)
    return entry


async def _run_options_flow(hass, entry, user_input, advanced_steps):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    flow_id = result["flow_id"]
    result = await hass.config_entries.options.async_configure(flow_id, user_input)
    result = await _submit_all(
        hass, hass.config_entries.options, flow_id, result, advanced_steps
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    await hass.async_block_till_done()


CREATE_USER_INPUT = {
    "name": ENTRY_NAME,
    "thermostat": [TRV_ID, SPARE_TRV_ID],
    "temperature_sensor": SENSOR_ID,
    "window_sensors": WINDOW_ID,
    "window_off_delay": {"hours": 0, "minutes": 2, "seconds": 5},
    "window_off_delay_after": {"hours": 0, "minutes": 0, "seconds": 30},
    "off_temperature": 18,
    "presets": ["comfort", "eco"],
    "tolerance": 0.4,
    "target_temp_min": "min_max_10",
    "target_temp_max": "min_max_28",
    "target_temp_step": "step_0_5",
}

CREATED_OPTIONS = {
    "name": ENTRY_NAME,
    "thermostat": [
        {
            "trv": TRV_ID,
            "integration": "generic_thermostat",
            "model": "generic",
            "adapter": None,
            "advanced": {
                "calibration": "target_temp_based",
                "calibration_mode": "pid_calibration",
                "mpc_v2_plant_preset": "auto",
                "protect_overheating": True,
                "no_off_system_mode": False,
                "heat_auto_swapped": False,
                "valve_maintenance": False,
                "child_lock": True,
                "homematicip": False,
            },
        },
        {
            "trv": SPARE_TRV_ID,
            "integration": "generic_thermostat",
            "model": "generic",
            "adapter": None,
            "advanced": {
                "calibration": "target_temp_based",
                "calibration_mode": "heating_power_calibration",
                "mpc_v2_plant_preset": "small_room",
                "protect_overheating": False,
                "no_off_system_mode": False,
                "heat_auto_swapped": False,
                "valve_maintenance": False,
                "child_lock": False,
                "homematicip": False,
            },
        },
    ],
    "cooler": None,
    "temperature_sensor": SENSOR_ID,
    "window_sensors": WINDOW_ID,
    "door_sensors": None,
    "humidity_sensor": None,
    "outdoor_sensor": None,
    "weather": None,
    "window_off_delay": 125,
    "window_off_delay_after": 30,
    "door_off_delay": 0,
    "door_off_delay_after": 0,
    "off_temperature": 18,
    "presets": ["comfort", "eco"],
    "tolerance": 0.4,
    "target_temp_min": "10.0",
    "target_temp_max": "28.0",
    "target_temp_step": "0.5",
    "model": "generic/generic",
}


async def test_the_create_flow_writes_the_frozen_options(hass):
    set_room_sensor(hass, 19.0)
    await build_devices(hass, GENERIC_HEAT_TRV, SPARE_HEAT_TRV)

    entry = await _run_create_flow(
        hass,
        CREATE_USER_INPUT,
        [
            {"child_lock": True, "calibration_mode": "pid_calibration"},
            {"protect_overheating": False, "mpc_v2_plant_preset": "small_room"},
        ],
    )

    assert entry.data == {}
    assert entry.options == CREATED_OPTIONS
    assert _serialised(entry.options) == _serialised(CREATED_OPTIONS)


def _entry_with_unknown_keys(*, in_data: bool) -> MockConfigEntry:
    settings = {
        "name": ENTRY_NAME,
        "thermostat": [
            {
                "trv": TRV_ID,
                "integration": "generic_thermostat",
                "model": "Generic",
                "trv_extra": {"kept": True},
                "advanced": {
                    "calibration": "target_temp_based",
                    "calibration_mode": "default",
                    "protect_overheating": "false",
                    "child_lock": 1,
                    "fix_calibration": True,
                    "balance_mode": "pid",
                },
            }
        ],
        "custom_top_level": [1, 2],
        "temperature_sensor": SENSOR_ID,
        "window_sensors": WINDOW_ID,
        "window_off_delay": 15,
        "model": "Generic",
        "tolerance": "0.3",
        "off_temperature": "7",
        "target_temp_step": 0.5,
        "presets": ["eco"],
    }
    return MockConfigEntry(
        domain=DOMAIN,
        version=18,
        minor_version=1 if in_data else 2,
        data=settings if in_data else {},
        options={} if in_data else settings,
        title=ENTRY_NAME,
    )


UPDATED_OPTIONS = {
    "name": "Renamed Room",
    "thermostat": [
        {
            "trv": TRV_ID,
            "integration": "generic_thermostat",
            "model": "Generic",
            "trv_extra": {"kept": True},
            # The advanced options are the ones the step submitted: a key the
            # step does not publish is not written back.
            "advanced": {
                "calibration": "target_temp_based",
                "calibration_mode": "pid_calibration",
                "mpc_v2_plant_preset": "auto",
                "protect_overheating": False,
                "no_off_system_mode": False,
                "heat_auto_swapped": False,
                "valve_maintenance": False,
                "child_lock": True,
                "homematicip": False,
            },
            "adapter": None,
        },
        {
            "trv": SPARE_TRV_ID,
            "integration": "generic_thermostat",
            "model": "generic",
            "adapter": None,
            "advanced": {
                "calibration": "target_temp_based",
                "calibration_mode": "tpi_calibration",
                "mpc_v2_plant_preset": "auto",
                "protect_overheating": True,
                "no_off_system_mode": False,
                "heat_auto_swapped": False,
                "valve_maintenance": False,
                "child_lock": False,
                "homematicip": True,
            },
        },
    ],
    "custom_top_level": [1, 2],
    "temperature_sensor": SENSOR_ID,
    "window_sensors": None,
    "window_off_delay": 60,
    "model": "Generic",
    "tolerance": 0.2,
    "off_temperature": 7,
    "target_temp_step": "0.5",
    "presets": ["eco"],
    "cooler": None,
    "door_sensors": None,
    "humidity_sensor": None,
    "outdoor_sensor": None,
    "weather": None,
    "window_off_delay_after": 0,
    "door_off_delay": 0,
    "door_off_delay_after": 0,
    "target_temp_min": "-1.0",
    "target_temp_max": "25.0",
}


async def _update(hass, *, in_data: bool):
    set_room_sensor(hass, 19.0)
    await build_devices(hass, GENERIC_HEAT_TRV, SPARE_HEAT_TRV)
    entry = _entry_with_unknown_keys(in_data=in_data)
    entry.add_to_hass(hass)

    await _run_options_flow(
        hass,
        entry,
        {
            "name": "Renamed Room",
            "thermostat": [TRV_ID, SPARE_TRV_ID],
            "temperature_sensor": SENSOR_ID,
            "window_off_delay": {"hours": 0, "minutes": 1, "seconds": 0},
            "tolerance": 0.2,
            "target_temp_max": "min_max_25",
        },
        [{}, {"calibration_mode": "tpi_calibration", "homematicip": True}],
    )
    return entry


async def test_the_options_flow_writes_the_frozen_options(hass):
    entry = await _update(hass, in_data=False)

    assert entry.data == {}
    assert entry.options == UPDATED_OPTIONS
    assert _serialised(entry.options) == _serialised(UPDATED_OPTIONS)


async def test_the_options_flow_moves_settings_from_the_data_unchanged(hass):
    entry = await _update(hass, in_data=True)

    assert entry.data == {}
    assert entry.options == UPDATED_OPTIONS
    assert _serialised(entry.options) == _serialised(UPDATED_OPTIONS)


async def test_a_refused_first_submission_leaves_nothing_behind(hass):
    """The form sent back after an error starts from the refused submission.

    The second submission is normalised on top of the first, so a key the
    first one set and the second one changed must come out as the second one
    has it.
    """
    set_room_sensor(hass, 19.0)
    await build_devices(hass, GENERIC_HEAT_TRV, SPARE_HEAT_TRV)

    entry = await _run_create_flow(
        hass,
        CREATE_USER_INPUT,
        [
            {"child_lock": True, "calibration_mode": "pid_calibration"},
            {"protect_overheating": False, "mpc_v2_plant_preset": "small_room"},
        ],
        refused_first=CREATE_USER_INPUT
        | {"target_temp_min": "min_max_30", "tolerance": 1.5, "presets": ["away"]},
    )

    assert entry.options == CREATED_OPTIONS
    assert _serialised(entry.options) == _serialised(CREATED_OPTIONS)
