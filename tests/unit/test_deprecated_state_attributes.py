"""State attributes are published under their current and deprecated names.

Every entry of ``DEPRECATED_STATE_ATTRIBUTES`` pairs a current attribute name
with the name 1.9 published. The entity writes each value under both, so
templates that read the old name keep working and 1.9 can restore from it
after a rollback. Restoring from either name is covered next to the other
restore tests in ``test_climate_startup.py``.
"""

import json

import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.const import (
    ATTR_STATE_PRESET_COOL_TEMPERATURE,
    ATTR_STATE_PRESET_HEAT_TEMPERATURES,
    DEPRECATED_STATE_ATTRIBUTES,
)
from custom_components.better_thermostat.utils.telemetry import TELEMETRY_ATTRIBUTES
from tests.factories import make_state_attributes_bt, make_trv


def test_every_deprecated_attribute_is_published_with_the_current_value():
    """Each deprecated name carries the value of its current name."""
    entity = make_state_attributes_bt(
        _preset_cool_temperature=24.5,
        _preset_cool_temperatures={"comfort": 25.0},
        temp_slope=0.0012,
        heating_power_normalized=0.42,
    )
    entity.preset_mgr.temperatures = {"comfort": 21.0}

    attrs = BetterThermostat.extra_state_attributes.fget(entity)

    assert attrs[ATTR_STATE_PRESET_COOL_TEMPERATURE] == 24.5
    assert json.loads(attrs[ATTR_STATE_PRESET_HEAT_TEMPERATURES]) == {"comfort": 21.0}
    for name, deprecated_name in DEPRECATED_STATE_ATTRIBUTES.items():
        if name.startswith(("pid_", "mpc_v2_")):
            continue
        assert name in attrs
        assert attrs[deprecated_name] == attrs[name]


# Every name 1.9 published that 2.x writes next to its current name, spelled
# out here so a typo or a dropped entry in the table fails.
_PUBLISHED_BY_1_9 = {
    "preset_cool_temperature": "bt_preset_cool_temperature",
    "preset_cool_temperatures": "bt_preset_cool_temperatures",
    "preset_heat_temperatures": "bt_preset_heat_temperatures",
    "room_temperature_filtered": "external_temp_ema",
    "temperature_slope_kelvin_per_min": "temp_slope_K_min",
    "heating_power_normalized": "heating_power_norm",
    "pid_error_kelvin": "pid_e_K",
    "pid_measurement_filtered": "pid_meas_smooth_C",
    "pid_measurement_slope_kelvin_per_min": "pid_d_meas_K_per_min",
    "pid_dt_seconds": "pid_dt_s",
    "mpc_v2_room_temperature_estimate": "mpc_v2_T_room_hat",
    "mpc_v2_radiator_temperature_estimate": "mpc_v2_T_rad_hat",
    "mpc_v2_radiator_room_coupling": "mpc_v2_coupling_rad_room",
    "mpc_v2_disturbance_kelvin_per_min": "mpc_v2_D_hat_K_per_min",
    "mpc_v2_tau_room_minutes": "mpc_v2_tau_room_min",
    "mpc_v2_group_valve_percent": "mpc_v2_group_valve_pct",
}


def test_the_table_holds_every_name_1_9_published():
    """Each renamed attribute keeps the exact spelling 1.9 published."""
    assert DEPRECATED_STATE_ATTRIBUTES == _PUBLISHED_BY_1_9


def _trv(debug: dict[str, object]) -> Trv:
    return make_trv(calibration_balance={"debug": debug})


_PID_DEBUG = {
    "mode": "pid",
    "e_K": 0.4,
    "meas_smooth_C": 20.1,
    "d_meas_per_s": 0.001,
    "dt_s": 30.0,
}
_MPC_V2_DEBUG = {
    "controller_version": "v2",
    "T_room_hat": 20.5,
    "T_rad_hat": 35.0,
    "coupling_rad_room": 0.7,
    "D_hat_K_per_min": 0.002,
    "tau_room_min": 180.0,
    "group_valve_pct": 42.0,
    "reid_tau_room": 200.0,
}


@pytest.mark.parametrize(
    ("debug", "renamed"), [(_PID_DEBUG, 4), (_MPC_V2_DEBUG, 6)], ids=["pid", "mpc_v2"]
)
def test_controller_telemetry_is_published_under_both_names(debug, renamed):
    """A controller's telemetry carries its deprecated names with the same values."""
    entity = make_state_attributes_bt(real_trvs={"climate.trv": _trv(debug)})

    attrs = BetterThermostat.extra_state_attributes.fget(entity)

    mirrored = [
        name
        for name in DEPRECATED_STATE_ATTRIBUTES
        if name.startswith(("pid_", "mpc_v2_")) and name in attrs
    ]
    assert len(mirrored) == renamed
    for name in mirrored:
        assert attrs[DEPRECATED_STATE_ATTRIBUTES[name]] == attrs[name]


def test_deprecated_telemetry_names_stay_out_of_the_recorder():
    """The old name of a telemetry attribute is as unrecorded as the new one."""
    unrecorded = BetterThermostat._unrecorded_attributes

    for name, deprecated_name in DEPRECATED_STATE_ATTRIBUTES.items():
        if name in TELEMETRY_ATTRIBUTES:
            assert deprecated_name in unrecorded
