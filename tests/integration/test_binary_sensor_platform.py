"""The heat source and heat loss sensors exist for the rooms MPC v2 controls.

The two binary sensors report what the MPC v2 observer measures. A room on
another calibration mode has no observer, so the sensors would stay
unavailable for good and are not created there.
"""

from typing import Any, cast

from homeassistant.helpers import entity_registry as er

from custom_components.better_thermostat.utils.const import (
    CONF_CALIBRATION_MODE,
    CONF_THERMOSTAT,
    CalibrationMode,
)
from custom_components.better_thermostat.utils.helpers import entry_settings

from .conftest import (
    build_devices,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for_startup,
)
from .device_profiles import MPC_V2_VALVE_TRV

SOURCE_SENSORS = {"heat_source_detected", "heat_loss_detected"}


def _source_sensor_keys(hass, entry) -> set[str | None]:
    """Return the translation keys of the entry's binary sensors."""
    return {
        reg_entry.translation_key
        for reg_entry in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
        if reg_entry.domain == "binary_sensor"
    }


async def _switch_calibration_mode(hass, entry, mode: CalibrationMode) -> None:
    """Save the options with every TRV on ``mode`` and wait for the thermostat."""
    settings = entry_settings(entry)
    trvs = cast("list[dict[str, Any]]", settings[CONF_THERMOSTAT])
    hass.config_entries.async_update_entry(
        entry,
        options={
            **settings,
            CONF_THERMOSTAT: [
                {**trv, "advanced": {**trv["advanced"], CONF_CALIBRATION_MODE: mode}}
                for trv in trvs
            ],
        },
    )
    await hass.async_block_till_done()
    await wait_for_startup(hass, entry)


async def test_a_room_on_mpc_v2_has_both_source_sensors(hass):
    """Setting the entry up creates the heat source and the heat loss sensor."""
    set_room_sensor(hass, 18.0)
    await build_devices(hass, MPC_V2_VALVE_TRV)
    entry = make_entry(MPC_V2_VALVE_TRV)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)

    assert _source_sensor_keys(hass, entry) == SOURCE_SENSORS


async def test_a_room_on_another_mode_has_no_source_sensors(hass, fake_trv):
    """Without an observer there is nothing for the sensors to report."""
    set_room_sensor(hass, 18.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)

    assert _source_sensor_keys(hass, entry) == set()


async def test_the_source_sensors_follow_the_calibration_mode(hass):
    """Saving another calibration mode takes the sensors, MPC v2 gives them back.

    The user does not restart Home Assistant in between: the sensors are
    there, or gone, as soon as the options are saved.
    """
    set_room_sensor(hass, 18.0)
    await build_devices(hass, MPC_V2_VALVE_TRV)
    entry = make_entry(MPC_V2_VALVE_TRV)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)

    await _switch_calibration_mode(hass, entry, CalibrationMode.PID_CALIBRATION)
    assert _source_sensor_keys(hass, entry) == set()

    await _switch_calibration_mode(hass, entry, CalibrationMode.MPC_V2_CALIBRATION)
    assert _source_sensor_keys(hass, entry) == SOURCE_SENSORS
