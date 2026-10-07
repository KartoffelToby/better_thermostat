"""The recorder keeps the thermostat's annunciation, not its controller telemetry.

The climate entity exposes the internals of its controller as state
attributes: PID terms, MPC estimates, cycle records and balance summaries.
Several change on nearly every write, so recording them gives every state
change a fresh attribute row. The live state keeps showing all of them.
"""

from dataclasses import replace
from datetime import timedelta
from functools import partial

from homeassistant.components.recorder import history
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.better_thermostat.utils.const import CalibrationMode

from .conftest import (
    build_devices,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import GENERIC_HEAT_TRV

PID = CalibrationMode.PID_CALIBRATION.value

# Telemetry the entity writes outside the PID and MPC v2 families.
_TELEMETRY = {
    "degraded_for_seconds",
    "temperature_slope_kelvin_per_min",
    "temp_slope_K_min",
    "calibration_balance",
    "heating_cycle_count",
    "heating_cycle_last",
    "heat_loss_cycle_count",
    "heat_loss_cycle_last",
    "heat_loss_stats",
    "heating_power_normalized",
    "heating_power_norm",
}


def _is_telemetry(key: str) -> bool:
    return key.startswith(("pid_", "mpc_v2_")) or key in _TELEMETRY


async def _recorded_attributes(hass, state) -> dict:
    """Return the attributes the recorder stored for the live ``state``.

    Most writes of the climate entity change attributes only, so the query
    keeps insignificant rows and picks the one whose update time is the
    live state's.
    """
    await async_wait_recording_done(hass)
    start = dt_util.utcnow() - timedelta(hours=1)
    states = await get_instance(hass).async_add_executor_job(
        partial(
            history.get_significant_states,
            hass,
            start,
            None,
            [state.entity_id],
            significant_changes_only=False,
        )
    )
    rows = [
        row
        for row in states[state.entity_id]
        if row.last_updated_timestamp == state.last_updated_timestamp
    ]
    assert len(rows) == 1
    return dict(rows[0].attributes)


async def test_controller_telemetry_stays_out_of_the_recorder(hass):
    """PID terms show on the state but not in the recorded attributes."""
    profile = replace(GENERIC_HEAT_TRV, calibration_mode=PID)
    set_room_sensor(hass, 18.0)
    await build_devices(hass, profile)
    entry = make_entry(profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    await bt.async_set_temperature(temperature=22.0)
    assert await wait_for(
        hass, lambda: "pid_u" in hass.states.get(bt.entity_id).attributes
    )
    live = hass.states.get(bt.entity_id)

    recorded = await _recorded_attributes(hass, live)

    assert recorded["temperature"] == 22.0
    assert "control_mode" in recorded
    assert sorted(key for key in recorded if _is_telemetry(key)) == []
