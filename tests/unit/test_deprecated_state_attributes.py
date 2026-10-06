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
    "D_hat_K_per_min": 0.002,
    "tau_room_min": 180.0,
    "group_valve_pct": 42.0,
    "reid_tau_room": 200.0,
}


@pytest.mark.parametrize(
    ("debug", "renamed"), [(_PID_DEBUG, 4), (_MPC_V2_DEBUG, 3)], ids=["pid", "mpc_v2"]
)
def test_controller_telemetry_is_published_under_both_names(debug, renamed):
    """A controller's telemetry carries its deprecated names with the same values."""
    entity = make_state_attributes_bt(real_trvs={"climate.trv": _trv(debug)})

    attrs = BetterThermostat.extra_state_attributes.fget(entity)

    mirrored = [
        name
        for name in DEPRECATED_STATE_ATTRIBUTES
        if name in TELEMETRY_ATTRIBUTES and name in attrs
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
