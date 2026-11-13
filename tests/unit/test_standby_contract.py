"""The standby contract: observe as tracking, never as learning.

While heating is suppressed (open window, OFF), the entity-level state
estimates keep converging, the controllers do not integrate error or
learn parameters from disturbance-flagged samples, and re-entry resumes
from held controller state plus fresh estimates — which is what makes
the transfer bumpless.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from homeassistant.components.climate.const import HVACAction, HVACMode
import pytest

from custom_components.better_thermostat.calibration import (
    _compute_mpc_v2_balance,
    _compute_pid_balance,
)
from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.fsm.anomaly import (
    AnomalyState,
    FaultPhase,
    FaultSlot,
)
from custom_components.better_thermostat.core.snapshot import ThermalEvidence
from custom_components.better_thermostat.utils.calibration.mpc import (
    MpcInput,
    MpcParams,
    MpcState,
    compute_mpc,
)
from custom_components.better_thermostat.utils.calibration.pid import (
    PIDState,
    build_pid_loop_key,
)
from custom_components.better_thermostat.utils.calibration.tpi import (
    TpiInput,
    TpiParams,
    TpiState,
    compute_tpi,
)
from custom_components.better_thermostat.utils.const import CalibrationMode
from custom_components.better_thermostat.utils.controlling import compute_control_cycle
from custom_components.better_thermostat.utils.thermal_learning import (
    HeatingPowerTracker,
    HeatLossTracker,
)
from tests.factories import ThermostatStandIn, make_state, trv_record
from tests.unit.test_contact_gates_controllers import _bt, _valve_trv

ENTITY_ID = "climate.trv"


def _pid_bt(*, window_open):
    bt = ThermostatStandIn()
    bt.device_name = "Test BT"
    bt.heat_target_temperature = 21.0
    bt.room_temperature = 18.0
    bt.room_temperature_filtered = None
    bt.window_open = window_open
    bt.door_open = False
    bt.contact_open = bool(window_open)
    bt.bt_hvac_mode = HVACMode.HEAT
    bt.clock = FakeClock(monotonic_value=5000.0)
    bt.kernel_state = make_state()
    bt.real_trvs = {ENTITY_ID: trv_record(entity_id=ENTITY_ID)}
    bt.state_mgr = MagicMock()
    bt.state_mgr.state.pid = {
        build_pid_loop_key(bt, ENTITY_ID): PIDState(
            pid_integral=12.0, pid_last_meas=19.6, pid_last_time=1000.0
        )
    }
    return bt


def test_pid_observes_but_does_not_actuate_during_an_open_window():
    """Standby separates observation from actuation.

    No PID step runs (the integrator cannot wind up on the growing
    error of a cooling room), but the measurement chain keeps tracking
    the room so re-entry sees a fresh measurement and a small dt —
    bumpless transfer.
    """
    bt = _pid_bt(window_open=True)
    result, use_valve = _compute_pid_balance(bt, ENTITY_ID)

    assert result is None
    assert use_valve is False
    saved = bt.state_mgr.set_pid.call_args[0][1]
    assert saved.pid_integral == 12.0  # frozen: no windup in standby
    assert saved.pid_last_time == 5000.0  # but time keeps tracking
    assert saved.pid_last_meas != 19.6  # and the measurement follows the room


def test_pid_observes_but_does_not_actuate_when_off():
    """OFF is standby too: observation only."""
    bt = _pid_bt(window_open=False)
    bt.bt_hvac_mode = HVACMode.OFF
    result, _ = _compute_pid_balance(bt, ENTITY_ID)

    assert result is None
    saved = bt.state_mgr.set_pid.call_args[0][1]
    assert saved.pid_integral == 12.0
    assert saved.pid_last_time == 5000.0


def test_mpc_discards_the_interval_but_keeps_the_model():
    """A window-open sample drops the interval, not the model.

    Half-heated intervals would teach the model that heating does not
    work; the learned parameters survive for re-entry.
    """
    state = MpcState(
        gain_est=0.02,
        loss_est=0.01,
        last_learn_time=900.0,
        last_learn_temperature=19.5,
        virtual_temperature=19.8,
        last_percent=60.0,
    )
    inp = MpcInput(
        key="t",
        target_temperature=21.0,
        room_temperature=19.0,
        window_open=True,
        bt_name="Test BT",
        entity_id=ENTITY_ID,
    )
    out, new_state = compute_mpc(inp, MpcParams(), state=state, all_states={})

    assert out is not None and out.valve_percent == 0.0
    assert new_state.gain_est == 0.02
    assert new_state.loss_est == 0.01
    assert new_state.last_learn_time is None
    assert new_state.virtual_temperature is None


def test_tpi_emits_zero_during_an_open_window():
    """TPI is integrator-free; standby simply emits no duty."""
    inp = TpiInput(
        key="t",
        target_temperature=21.0,
        room_temperature=19.0,
        window_open=True,
        bt_name="Test BT",
        entity_id=ENTITY_ID,
    )
    out, _ = compute_tpi(inp, TpiParams(), state=TpiState())

    assert out is not None and out.duty_cycle_percent == 0.0


def test_anomaly_hold_freezes_learners_and_cuts_reid_buffer():
    """Active or recovering anomalies set learning_held and freeze all learners.

    The onset cycle commits ``learning_held = True`` inside
    ``compute_control_cycle``, freezing MPC, MPC v2, PID, TPI, and the thermal
    trackers while cutting the MPC v2 re-identification buffer with a paused
    sample.
    """
    trvs = {ENTITY_ID: _valve_trv(ENTITY_ID, CalibrationMode.MPC_V2_CALIBRATION)}
    bt = _bt(trvs, window_open=False, door_open=False)
    bt.contact_sensed_open = False
    bt.is_removed = False
    bt.room_temperature = 19.5
    bt.real_trvs[ENTITY_ID].valve_position = 40.0

    # Normal step records an unpaused re-id sample and warms MPC v2.
    out_normal, _ = _compute_mpc_v2_balance(bt, ENTITY_ID)
    assert out_normal is not None
    (reid_runtime,) = bt.state_mgr._mpc_v2_reid.values()
    assert len(reid_runtime.buffer.samples) == 1
    assert reid_runtime.buffer.samples[-1].learning_paused is False
    (v2_state,) = bt.state_mgr._mpc_v2.values()
    assert v2_state.controller is not None
    held_before = (
        v2_state.controller.dob.D_hat_K_per_min,
        v2_state.controller.optimiser.e_integral_K_min,
    )

    # Commit a cycle with an active anomaly: learning_held turns True immediately.
    bt_cycle = _pid_bt(window_open=False)
    bt_cycle.cool_target_temperature = None
    bt_cycle.temperature_slope = -0.08
    bt_cycle.call_for_heat = True
    bt_cycle.preset_mode = None
    bt_cycle.tolerance = 0.0
    bt_cycle.outdoor_sensor_entity_id = None
    bt_cycle.weather_entity_id = None
    bt_cycle.bt_min_temp = 5.0
    bt_cycle.bt_max_temp = 30.0
    bt_cycle.window_sensor_entity_id = None
    bt_cycle.clock = FakeClock(
        monotonic_value=1200.0, now_value=datetime(2026, 1, 15, 8, 20, tzinfo=UTC)
    )
    bt_cycle.kernel_state = make_state(
        anomaly_detection=True,
        anomaly=AnomalyState(
            rapid_loss=FaultSlot(phase=FaultPhase.SUSPECT, since=1000.0)
        ),
    )
    bt_cycle.anomaly_evidence.observe_snapshot = MagicMock(
        return_value=(
            ThermalEvidence(
                slope_residual_kelvin_per_min=-0.08,
                slope_residual_sigma_kelvin_per_min=0.01,
            ),
            {},
        )
    )
    _, desired = compute_control_cycle(bt_cycle)
    assert len(desired.anomalies) == 1
    assert bt_cycle.learning_held is True

    # Propagate the anomaly hold to all learners and verify each holds its parameters.
    bt.learning_held = bt_cycle.learning_held
    bt.clock.advance(seconds=300.0)
    out_held, _ = _compute_mpc_v2_balance(bt, ENTITY_ID)
    assert out_held is not None
    assert (
        v2_state.controller.dob.D_hat_K_per_min,
        v2_state.controller.optimiser.e_integral_K_min,
    ) == pytest.approx(held_before)
    assert len(reid_runtime.buffer.samples) == 2
    assert reid_runtime.buffer.samples[-1].learning_paused is True

    # MPC v1 holds gain/loss and restarts its learning interval.
    mpc_state = MpcState(
        gain_est=0.025,
        loss_est=0.012,
        last_learn_time=900.0,
        last_learn_temperature=19.5,
    )
    mpc_out, mpc_after = compute_mpc(
        MpcInput(
            key="t",
            target_temperature=21.0,
            room_temperature=19.5,
            bt_name="Test BT",
            entity_id=ENTITY_ID,
            learning_paused=bt.learning_held,
        ),
        MpcParams(),
        state=mpc_state,
        all_states={},
    )
    assert mpc_out is not None
    assert (mpc_after.gain_est, mpc_after.loss_est) == (0.025, 0.012)

    # PID holds its integral and gains while still actuating.
    bt_cycle.learning_held = True
    pid_out, _ = _compute_pid_balance(bt_cycle, ENTITY_ID)
    assert pid_out is not None
    saved_pid = bt_cycle.state_mgr.set_pid.call_args[0][1]
    assert saved_pid.pid_integral == 12.0

    # HeatingPowerTracker and HeatLossTracker discard in-flight stretches under the hold.
    hp_tracker = HeatingPowerTracker(heating_power=0.05)
    start_ts = datetime(2026, 1, 15, 8, 0, tzinfo=UTC)
    hp_tracker.update(
        19.0,
        HVACAction.HEATING,
        start_ts,
        heat_target_temperature=21.0,
        learning_paused=bt.learning_held,
    )
    assert hp_tracker.heating_power == 0.05
    assert not hp_tracker.cycles

    hl_tracker = HeatLossTracker(heat_loss_rate=0.02)
    hl_tracker.update(
        21.0,
        HVACAction.IDLE,
        start_ts + timedelta(minutes=10),
        learning_paused=bt.learning_held,
    )
    assert hl_tracker.heat_loss_rate == 0.02
    assert not hl_tracker.cycles
