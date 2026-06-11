"""The standby contract: observe as tracking, never as learning.

While heating is suppressed (open window, OFF), the entity-level state
estimates keep converging, the controllers do not integrate error or
learn parameters from disturbance-flagged samples, and re-entry resumes
from held controller state plus fresh estimates — which is what makes
the transfer bumpless.
"""

from unittest.mock import MagicMock

from homeassistant.components.climate.const import HVACMode

from custom_components.better_thermostat.calibration import _compute_pid_balance
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.calibration.mpc import (
    MpcInput,
    MpcParams,
    MpcState,
    compute_mpc,
)
from custom_components.better_thermostat.utils.calibration.tpi import (
    TpiInput,
    TpiParams,
    TpiState,
    compute_tpi,
)

ENTITY_ID = "climate.trv"


def _pid_bt(*, window_open):
    bt = MagicMock()
    bt.device_name = "Test BT"
    bt.bt_target_temp = 21.0
    bt.cur_temp = 18.0
    bt.window_open = window_open
    bt.bt_hvac_mode = HVACMode.HEAT
    bt.real_trvs = {ENTITY_ID: Trv(entity_id=ENTITY_ID)}
    return bt


def test_pid_does_not_run_during_an_open_window():
    """No PID step in standby.

    The integrator cannot wind up on the growing error of a cooling
    room.
    """
    bt = _pid_bt(window_open=True)
    result, use_valve = _compute_pid_balance(bt, ENTITY_ID)

    assert result is None
    assert use_valve is False
    bt.state_mgr.get_pid.assert_not_called()


def test_pid_does_not_run_when_off():
    """OFF is standby too."""
    bt = _pid_bt(window_open=False)
    bt.bt_hvac_mode = HVACMode.OFF
    result, _ = _compute_pid_balance(bt, ENTITY_ID)

    assert result is None
    bt.state_mgr.get_pid.assert_not_called()


def test_mpc_discards_the_interval_but_keeps_the_model():
    """A window-open sample drops the interval, not the model.

    Half-heated intervals would teach the model that heating does not
    work; the learned parameters survive for re-entry.
    """
    state = MpcState(
        gain_est=0.02,
        loss_est=0.01,
        last_learn_time=900.0,
        last_learn_temp=19.5,
        virtual_temp=19.8,
        last_percent=60.0,
    )
    inp = MpcInput(
        key="t",
        target_temp_C=21.0,
        current_temp_C=19.0,
        window_open=True,
        bt_name="Test BT",
        entity_id=ENTITY_ID,
    )
    out, new_state = compute_mpc(inp, MpcParams(), state=state, all_states={})

    assert out is not None and out.valve_percent == 0.0
    assert new_state.gain_est == 0.02
    assert new_state.loss_est == 0.01
    assert new_state.last_learn_time is None
    assert new_state.virtual_temp is None


def test_tpi_emits_zero_during_an_open_window():
    """TPI is integrator-free; standby simply emits no duty."""
    inp = TpiInput(
        key="t",
        target_temp_C=21.0,
        current_temp_C=19.0,
        window_open=True,
        bt_name="Test BT",
        entity_id=ENTITY_ID,
    )
    out, _ = compute_tpi(inp, TpiParams(), state=TpiState())

    assert out is not None and out.duty_cycle_pct == 0.0
