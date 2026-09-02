"""Smoke tests: the shared factories work against production functions."""

from homeassistant.components.climate.const import HVACAction

from custom_components.better_thermostat.calibration import (
    calculate_calibration_local,
    calculate_calibration_setpoint,
)
from custom_components.better_thermostat.core.decide import decide
from custom_components.better_thermostat.utils.const import CalibrationMode
from custom_components.better_thermostat.utils.scheduler import request_control_cycle
from tests.factories import DEFAULT_TRV_ID, make_bt, make_snapshot, make_state


def test_make_bt_runs_through_both_calibration_channels():
    """A factory-built entity feeds the real calibration functions."""
    bt = make_bt(
        hvac_action=HVACAction.IDLE,
        advanced={"calibration_mode": CalibrationMode.DEFAULT},
    )
    assert calculate_calibration_local(bt, DEFAULT_TRV_ID) is not None
    assert calculate_calibration_setpoint(bt, DEFAULT_TRV_ID) is not None


def test_make_bt_works_with_the_scheduler_facade():
    """The factory's control queue accepts a real cycle request."""
    bt = make_bt()
    request_control_cycle(bt)
    assert bt.control_queue_task.get_nowait() is bt


def test_make_snapshot_and_state_run_through_the_kernel():
    """The kernel input builders produce a decidable pair."""
    desired, _ = decide(make_snapshot(), make_state())
    assert set(desired.trvs) == {"climate.trv1", "climate.trv2"}
