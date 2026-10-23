"""The MPC v2 flow temperature, from the settings form to the controller.

The flow temperature is entered and stored in the system unit, and the
controller models the room with it in °C. An options change reloads the entry,
so the new value reaches a rebuilt controller that carries on with the state
the old one stored.
"""

from dataclasses import replace
from unittest.mock import patch

from homeassistant.const import UnitOfTemperature
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.better_thermostat.utils.calibration.mpc_v2 import (
    coupling_for_flow_temperature,
)
from custom_components.better_thermostat.utils.const import (
    CONF_MPC_V2_FLOW_TEMPERATURE,
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
    form_default,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import GENERIC_HEAT_TRV, VALVE_TRV

MPC_V2_VALVE_TRV = replace(
    VALVE_TRV,
    name="valve_trv_mpc_v2",
    calibration_mode=CalibrationMode.MPC_V2_CALIBRATION.value,
)


async def _open_advanced_form(hass, thermostat: str):
    """Start the create flow and return its advanced step for ``thermostat``."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Flow Room",
            CONF_THERMOSTAT: [thermostat],
            CONF_TEMPERATURE_SENSOR: SENSOR_ID,
        },
    )
    assert result["step_id"] == "advanced", result
    return result


async def _finish(hass, form, advanced):
    """Submit the advanced step and the confirm step; return the stored value."""
    result = await hass.config_entries.flow.async_configure(form["flow_id"], advanced)
    assert result["step_id"] == "confirm", result
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    await hass.async_block_till_done()
    (entry,) = hass.config_entries.async_entries(DOMAIN)
    return entry.options[CONF_THERMOSTAT][0]["advanced"][CONF_MPC_V2_FLOW_TEMPERATURE]


def _field_config(form, key: str) -> dict[str, object]:
    """Return the selector configuration of one field of ``form``."""
    for marker, field in form["data_schema"].schema.items():
        if marker == key:
            return dict(field.config)
    raise AssertionError(f"{key} is not on the form")


@pytest.mark.parametrize(
    ("unit", "default", "lower", "upper", "entered"),
    [
        (UnitOfTemperature.CELSIUS, 65.0, 25.0, 90.0, 40),
        (UnitOfTemperature.FAHRENHEIT, 149.0, 77.0, 194.0, 104),
    ],
    ids=["celsius", "fahrenheit"],
)
async def test_the_flow_temperature_is_entered_in_the_system_unit(
    hass, unit, default, lower, upper, entered
):
    """The form offers 65 °C in the system unit and stores what is entered."""
    if unit is UnitOfTemperature.FAHRENHEIT:
        hass.config.units = US_CUSTOMARY_SYSTEM
    set_room_sensor(hass, 19.0)
    (trv,) = await build_devices(hass, GENERIC_HEAT_TRV)

    form = await _open_advanced_form(hass, trv.entity_id)

    assert form_default(form, CONF_MPC_V2_FLOW_TEMPERATURE) == pytest.approx(default)
    config = _field_config(form, CONF_MPC_V2_FLOW_TEMPERATURE)
    assert config["unit_of_measurement"] == unit
    assert config["min"] == pytest.approx(lower)
    assert config["max"] == pytest.approx(upper)
    assert await _finish(
        hass, form, {CONF_MPC_V2_FLOW_TEMPERATURE: entered}
    ) == pytest.approx(entered)


async def test_an_untouched_flow_temperature_stores_the_default(hass):
    """Accepting the form leaves the room on 65 °C supply water."""
    set_room_sensor(hass, 19.0)
    (trv,) = await build_devices(hass, GENERIC_HEAT_TRV)

    form = await _open_advanced_form(hass, trv.entity_id)

    assert await _finish(hass, form, {}) == 65.0


@pytest.mark.parametrize("entered", [24, 91], ids=["below", "above"])
async def test_a_flow_temperature_outside_the_range_is_refused(hass, entered):
    """No radiator system runs below 25 °C or above 90 °C supply water."""
    set_room_sensor(hass, 19.0)
    (trv,) = await build_devices(hass, GENERIC_HEAT_TRV)
    form = await _open_advanced_form(hass, trv.entity_id)

    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_MPC_V2_FLOW_TEMPERATURE: entered}
        )


def _entry_with_flow_temperature(profile, stored: float | None) -> MockConfigEntry:
    """Return the entry ``make_entry`` builds, with a stored flow temperature."""
    template = make_entry(profile)
    data = {
        **template.data,
        CONF_THERMOSTAT: [
            {**bundle, "advanced": dict(bundle["advanced"])}
            for bundle in template.data[CONF_THERMOSTAT]
        ],
    }
    if stored is not None:
        data[CONF_THERMOSTAT][0]["advanced"][CONF_MPC_V2_FLOW_TEMPERATURE] = stored
    return MockConfigEntry(domain=DOMAIN, version=18, data=data, title=template.title)


async def _live_controllers(hass, bt, target: float):
    """Heat the room toward ``target`` and return the live MPC v2 states.

    The states are keyed by target bucket, so the startup target and
    ``target`` can each hold one.
    """
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


@pytest.mark.parametrize(
    ("fake_trv", "stored", "modelled", "target"),
    [
        (MPC_V2_VALVE_TRV, None, 65.0, 24.0),
        (MPC_V2_VALVE_TRV, 40.0, 40.0, 24.0),
        (
            replace(
                MPC_V2_VALVE_TRV,
                name="valve_trv_mpc_v2_fahrenheit",
                system_unit=UnitOfTemperature.FAHRENHEIT,
            ),
            104.0,
            40.0,
            75.0,
        ),
    ],
    indirect=["fake_trv"],
    ids=["default", "celsius", "fahrenheit"],
)
async def test_the_controller_models_the_stored_flow_temperature(
    hass, fake_trv, stored, modelled, target
):
    """The stored flow temperature, in °C, is the supply water of the model."""
    set_room_sensor(hass, 17.0)
    entry = _entry_with_flow_temperature(fake_trv.profile, stored)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)

    live = await _live_controllers(hass, bt, target)

    assert {
        key: (
            state.controller.plant_coarse.params.T_water,
            state.controller.plant_coarse.params.coupling_rad_room,
        )
        for key, state in live.items()
    } == {
        key: (
            pytest.approx(modelled),
            pytest.approx(coupling_for_flow_temperature(modelled)),
        )
        for key in live
    }


@pytest.mark.parametrize("fake_trv", [MPC_V2_VALVE_TRV], indirect=True)
async def test_a_changed_flow_temperature_reaches_the_controller_with_its_state(
    hass, fake_trv
):
    """The options change reloads the entry, and the room is not relearned.

    The controller the reload builds models the new flow temperature and
    continues from the state the old one stored on unload, its command
    history included.
    """
    set_room_sensor(hass, 17.0)
    entry = _entry_with_flow_temperature(fake_trv.profile, 55.0)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    before = await _live_controllers(hass, bt, 24.0)
    planned = {key: list(state.controller._u_history) for key, state in before.items()}
    assert all(planned.values())
    assert {
        state.controller.plant_coarse.params.T_water for state in before.values()
    } == {55.0}

    await click_through_the_options(hass, entry, **{CONF_MPC_V2_FLOW_TEMPERATURE: 38.0})
    await hass.async_block_till_done()
    reloaded = await wait_for_startup(hass, entry)
    assert reloaded is not bt

    after = await _live_controllers(hass, reloaded, 24.0)
    for key, state in after.items():
        assert state.controller.plant_coarse.params.T_water == 38.0
        assert state.controller.plant_coarse.params.coupling_rad_room == (
            coupling_for_flow_temperature(38.0)
        )
        assert state.controller is not before[key].controller
        assert state.created_ts == before[key].created_ts
        history = list(state.controller._u_history)
        assert history[: len(planned[key])] == planned[key]
