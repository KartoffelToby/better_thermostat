"""Every room has its anomaly sensors; the observer's exist where MPC v2 runs.

The anomalies a room's TRVs and room sensor are enough to find have a binary
sensor in every room. Three more report what the MPC v2 observer measures. A
room on another calibration mode has no observer, so those would never report
there and are not created.
"""

from typing import Any, cast

from homeassistant.const import STATE_OFF, STATE_UNAVAILABLE
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

ANOMALY_SENSORS = {"suspected_open_window", "no_heat_delivery", "valve_stuck_open"}
OBSERVER_SENSORS = {"heat_source_detected", "heat_loss_detected", "sustained_heat_loss"}


def _binary_sensor_keys(hass, entry) -> set[str | None]:
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


async def test_a_room_on_mpc_v2_has_the_observer_sensors_too(hass):
    """Setting the entry up creates the anomaly sensors and the observer's."""
    set_room_sensor(hass, 18.0)
    await build_devices(hass, MPC_V2_VALVE_TRV)
    entry = make_entry(MPC_V2_VALVE_TRV)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)

    assert _binary_sensor_keys(hass, entry) == ANOMALY_SENSORS | OBSERVER_SENSORS


async def test_a_room_on_another_mode_has_the_anomaly_sensors_only(hass, fake_trv):
    """Without an observer there is nothing for the observer's sensors to report."""
    set_room_sensor(hass, 18.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)

    assert _binary_sensor_keys(hass, entry) == ANOMALY_SENSORS


async def test_the_observer_sensors_follow_the_calibration_mode(hass):
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
    assert _binary_sensor_keys(hass, entry) == ANOMALY_SENSORS

    await _switch_calibration_mode(hass, entry, CalibrationMode.MPC_V2_CALIBRATION)
    assert _binary_sensor_keys(hass, entry) == ANOMALY_SENSORS | OBSERVER_SENSORS


def _entity_ids(hass, entry) -> dict[str | None, str]:
    """Return the entry's entity ids by translation key."""
    return {
        reg_entry.translation_key: reg_entry.entity_id
        for reg_entry in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
    }


async def test_the_anomaly_entities_follow_the_detection_switch(hass):
    """The entities say whether anybody watches the room, the moment it changes.

    With detection off they are unavailable, so no room reads as free of
    problems while nothing looks. Switching it on shows a watched room,
    switching it off takes that back, without a restart in between.
    """
    set_room_sensor(hass, 18.0)
    await build_devices(hass, MPC_V2_VALVE_TRV)
    entry = make_entry(MPC_V2_VALVE_TRV)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)
    await hass.async_block_till_done()

    ids = _entity_ids(hass, entry)
    problems = [ids[key] for key in sorted(ANOMALY_SENSORS | {"sustained_heat_loss"})]
    detection = ids["sustained_heat_loss_detection"]
    switch = ids["anomaly_detection"]

    def shown() -> set[str]:
        return {hass.states.get(entity_id).state for entity_id in problems}

    assert shown() == {STATE_UNAVAILABLE}
    assert hass.states.get(detection).state == STATE_UNAVAILABLE

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": switch}, blocking=True
    )
    await hass.async_block_till_done()

    assert shown() == {STATE_OFF}
    assert hass.states.get(detection).state == "monitoring"

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": switch}, blocking=True
    )
    await hass.async_block_till_done()

    assert shown() == {STATE_UNAVAILABLE}
    assert hass.states.get(detection).state == STATE_UNAVAILABLE
