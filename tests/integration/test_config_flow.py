"""The config and options flows, driven through Home Assistant's flow manager."""

from homeassistant.data_entry_flow import FlowResultType

from custom_components.better_thermostat.utils.const import CONF_HEATER, CONF_SENSOR

from .conftest import DOMAIN, SENSOR_ID, TRV_ID, wait_for_startup

ENTRY_NAME = "BT Test"


def _user_step_input() -> dict:
    """Return a submission for the user step, naming the entities it wires."""
    return {"name": ENTRY_NAME, CONF_HEATER: [TRV_ID], CONF_SENSOR: SENSOR_ID}


async def _create_entry(hass):
    """Run the create flow to its end and return the flow result."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_step_input()
    )
    assert result["step_id"] == "advanced", result
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "confirm", result
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    return result


async def test_options_flow_refuses_to_clear_the_room_sensor(hass, fake_trv):
    """The room sensor stays required when the settings are changed.

    An emptied selector sends no key, the same submission that clears an
    optional entity. For the room sensor the form asks for one again and the
    entry keeps the sensor it has, so the thermostat keeps running.
    """
    hass.states.async_set(SENSOR_ID, "19.0", {"unit_of_measurement": "°C"})
    result = await _create_entry(hass)
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    (entry,) = hass.config_entries.async_entries(DOMAIN)
    await wait_for_startup(hass, entry)
    submission = _user_step_input()
    del submission[CONF_SENSOR]

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], submission
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {CONF_SENSOR: "no_sensor"}
    assert entry.data[CONF_SENSOR] == SENSOR_ID

    # The corrected submission goes through on the same form.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _user_step_input()
    )
    assert result["step_id"] == "advanced"
