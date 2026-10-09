"""Tests for the calibrator contract: capability nesting, strategies, healing."""

from unittest.mock import MagicMock

from homeassistant.core import State
import pytest

from custom_components.better_thermostat.core.calibrator import (
    Calibrator,
    CalibratorHealth,
    CapabilityLevel,
)
from custom_components.better_thermostat.core.fsm.control_mode import (
    ControlMode,
    ControlModeState,
)
from custom_components.better_thermostat.number import _PID_GAIN_SETTINGS
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.calibration.mpc import MpcOutput
from custom_components.better_thermostat.utils.calibration.mpc_v2 import MpcV2Output
from custom_components.better_thermostat.utils.calibration.pid import (
    PIDParams,
    PIDState,
    pid_gain,
    sanitize_pid_state,
    set_pid_gain,
)
from custom_components.better_thermostat.utils.calibration.strategies import (
    BalanceCalibrator,
    build_strategy_registry,
)
from custom_components.better_thermostat.utils.calibration.tpi import TpiOutput
from custom_components.better_thermostat.utils.const import CalibrationMode
from tests.factories import ThermostatStandIn, make_state


class TestCapabilityLevels:
    """READY implies HEALTHY implies CONFIGURED, by order."""

    def test_levels_are_ordered_by_strength(self):
        """Each level compares above every level it implies."""
        assert (
            CapabilityLevel.NONE
            < CapabilityLevel.CONFIGURED
            < CapabilityLevel.HEALTHY
            < CapabilityLevel.READY
        )


class _StubCalibrator:
    """Minimal structural implementation of the Calibrator protocol."""

    def __init__(self):
        self.observed = []
        self._ready = False

    def observe(self, snapshot, now):
        """Record the observation; readiness follows the data."""
        self.observed.append((snapshot, now))
        self._ready = True

    def is_ready(self):
        """Ready once something was observed."""
        return self._ready

    def actuate(self, snapshot):
        """Only emit when ready."""
        return 42.0 if self._ready else None

    def capability(self):
        """Report configured always; healthy/ready follow observations."""
        return CapabilityLevel.READY if self._ready else CapabilityLevel.CONFIGURED

    def health(self):
        """Report healthy unconditionally in the stub."""
        return CalibratorHealth.HEALTHY


class TestCalibratorProtocol:
    """The protocol is structural and the observe/actuate split holds."""

    def test_stub_satisfies_protocol(self):
        """A class with the right methods is a Calibrator."""
        assert isinstance(_StubCalibrator(), Calibrator)

    def test_observe_changes_state_actuate_only_when_ready(self):
        """observe() feeds the model; actuate() emits only when ready."""
        cal = _StubCalibrator()
        assert cal.actuate(None) is None
        cal.observe(None, 0.0)
        assert cal.is_ready() is True
        assert cal.actuate(None) == 42.0


class TestStrategyRegistry:
    """The registry maps controller modes to balance strategies."""

    def _registry(self, percent=55.0, use_valve=False):
        def compute_mpc(bt, entity_id):
            return MagicMock(spec=MpcOutput, valve_percent=percent), use_valve

        def compute_mpc_v2(bt, entity_id):
            return MagicMock(spec=MpcV2Output, valve_percent=percent), use_valve

        def compute_tpi(bt, entity_id):
            return MagicMock(spec=TpiOutput, duty_cycle_percent=percent), use_valve

        def compute_pid(bt, entity_id):
            return percent, use_valve

        return build_strategy_registry(
            compute_mpc, compute_mpc_v2, compute_tpi, compute_pid
        )

    def test_modes_are_covered(self):
        """MPC, MPC v2, TPI, and PID have strategies; DEFAULT does not."""
        registry = self._registry()
        assert set(registry) == {
            CalibrationMode.MPC_CALIBRATION,
            CalibrationMode.MPC_V2_CALIBRATION,
            CalibrationMode.TPI_CALIBRATION,
            CalibrationMode.PID_CALIBRATION,
        }

    @pytest.mark.parametrize(
        "mode",
        [
            CalibrationMode.MPC_CALIBRATION,
            CalibrationMode.MPC_V2_CALIBRATION,
            CalibrationMode.TPI_CALIBRATION,
            CalibrationMode.PID_CALIBRATION,
        ],
    )
    def test_run_extracts_the_percent(self, mode):
        """Each strategy reads its own result shape into a plain percent.

        The percent also feeds the TRV's oscillation history.
        """
        registry = self._registry(percent=55.0)
        bt = ThermostatStandIn()
        bt.device_name = "Test BT"
        bt.real_trvs = {"climate.trv": Trv(entity_id="climate.trv")}

        percent, use_valve = registry[mode].run(bt, "climate.trv")

        assert percent == 55.0
        assert use_valve is False
        assert list(bt.real_trvs["climate.trv"].balance_percent_history) == [55.0]

    def test_none_result_yields_no_percent(self):
        """A failed computation yields (None, use_valve)."""
        registry = build_strategy_registry(
            lambda bt, e: (None, False),
            lambda bt, e: (None, False),
            lambda bt, e: (None, False),
            lambda bt, e: (None, True),
        )
        assert registry[CalibrationMode.MPC_CALIBRATION].run(None, "x") == (None, False)
        assert registry[CalibrationMode.PID_CALIBRATION].run(None, "x") == (None, True)

    def test_capability_is_monotone(self):
        """Ready implies healthy implies configured for strategy reports."""
        registry = self._registry()
        strategy = registry[CalibrationMode.MPC_CALIBRATION]

        bt = ThermostatStandIn()
        bt.room_temperature = 20.0
        bt.heat_target_temperature = 21.0
        bt.kernel_state = make_state()
        bt.real_trvs = {"climate.trv": Trv(entity_id="climate.trv")}

        cap = strategy.capability(bt, "climate.trv")
        assert cap == CapabilityLevel.HEALTHY

        bt.real_trvs["climate.trv"].calibration_balance = {"valve_percent": 40}
        cap = strategy.capability(bt, "climate.trv")
        assert cap == CapabilityLevel.READY

        bt.room_temperature = None
        cap = strategy.capability(bt, "climate.trv")
        assert cap == CapabilityLevel.CONFIGURED

    def test_capability_healthy_under_sensor_fallback(self):
        """SENSOR_FALLBACK keeps the strategy healthy on the TRV mean.

        The control law computes on ``effective_room_temperature`` (TRV-internal
        mean) when the room sensor is dead; the capability report must
        judge the same input instead of flagging the calibrator unhealthy
        while it is actively controlling.
        """
        registry = self._registry()
        strategy = registry[CalibrationMode.MPC_CALIBRATION]

        bt = ThermostatStandIn()
        bt.device_name = "Test BT"
        bt.room_temperature = None
        bt.heat_target_temperature = 21.0
        bt.kernel_state = make_state(
            control_mode=ControlModeState(mode=ControlMode.SENSOR_FALLBACK)
        )
        bt.real_trvs = {
            "climate.trv": Trv(entity_id="climate.trv", current_temperature=20.5)
        }
        bt.hass.states.get.side_effect = lambda entity_id: State(
            entity_id, "heat", {"current_temperature": 20.5}
        )
        bt.hass.config.units.temperature_unit = "°C"

        cap = strategy.capability(bt, "climate.trv")
        assert cap >= CapabilityLevel.HEALTHY

        # Without any TRV temperature either, the fallback has no input.
        bt.real_trvs["climate.trv"].current_temperature = None
        cap = strategy.capability(bt, "climate.trv")
        assert cap == CapabilityLevel.CONFIGURED


class TestBalanceCalibrator:
    """The production adapter lifts a BalanceStrategy onto the protocol."""

    def _adapter(self, *, percent=55.0, use_valve=False, balance=None):
        registry = build_strategy_registry(
            lambda bt, e: (MagicMock(spec=MpcOutput, valve_percent=percent), use_valve),
            lambda bt, e: (
                MagicMock(spec=MpcV2Output, valve_percent=percent),
                use_valve,
            ),
            lambda bt, e: (
                MagicMock(spec=TpiOutput, duty_cycle_percent=percent),
                use_valve,
            ),
            lambda bt, e: (percent, use_valve),
        )
        bt = ThermostatStandIn()
        bt.room_temperature = 20.0
        bt.heat_target_temperature = 21.0
        bt.kernel_state = make_state()
        bt.real_trvs = {
            "climate.trv": Trv(entity_id="climate.trv", calibration_balance=balance)
        }
        strategy = registry[CalibrationMode.MPC_CALIBRATION]
        return BalanceCalibrator(bt, "climate.trv", strategy), bt

    def test_satisfies_the_protocol(self):
        """The adapter is a structural Calibrator."""
        adapter, _ = self._adapter()
        assert isinstance(adapter, Calibrator)

    def test_actuate_returns_the_observed_percent(self):
        """observe() runs the balance computation; actuate() emits it."""
        adapter, _ = self._adapter(percent=40.0)
        assert adapter.actuate(None) is None
        adapter.observe(None, 0.0)
        assert adapter.actuate(None) == 40.0

    def test_use_valve_results_are_not_emitted_as_percent(self):
        """A use_valve result carries no setpoint-channel percentage."""
        adapter, _ = self._adapter(percent=None, use_valve=True)
        adapter.observe(None, 0.0)
        assert adapter.actuate(None) is None

    def test_capability_delegates_to_the_strategy(self):
        """Capability comes from the strategy's report on the live entity."""
        adapter, bt = self._adapter(balance={"valve_percent": 40})
        assert adapter.capability() == CapabilityLevel.READY
        bt.room_temperature = None
        assert adapter.capability() == CapabilityLevel.CONFIGURED

    def test_readiness_means_a_finite_observed_result(self):
        """is_ready() gates actuation on a usable observed result.

        Narrower than capability: an annunciated grade does not drop
        control to passthrough, only a missing or non-finite result.
        """
        adapter, _ = self._adapter(percent=40.0)
        assert adapter.is_ready() is False  # nothing observed yet
        adapter.observe(None, 0.0)
        assert adapter.is_ready() is True

        nan_adapter, _ = self._adapter(percent=float("nan"))
        nan_adapter.observe(None, 0.0)
        assert nan_adapter.is_ready() is False
        assert nan_adapter.actuate(None) is None

    def test_health_flags_non_finite_results(self):
        """A non-finite observed percentage degrades the health grade."""
        adapter, _ = self._adapter(percent=float("nan"))
        assert adapter.health() == CalibratorHealth.HEALTHY
        adapter.observe(None, 0.0)
        assert adapter.health() == CalibratorHealth.NON_FINITE


class TestPidSelfHealing:
    """Pathological persisted PID state heals before it reaches control."""

    def test_healthy_state_passes_through(self):
        """A sane state is untouched and HEALTHY."""
        state = PIDState(pid_integral=5.0, pid_kp=60.0, pid_ki=0.01, pid_kd=2000.0)
        healed, health = sanitize_pid_state(state, PIDParams())
        assert health == CalibratorHealth.HEALTHY
        assert healed.pid_integral == 5.0
        assert healed.pid_kp == 60.0

    def test_nan_state_is_dropped(self):
        """Non-finite values reset to defaults and report NON_FINITE."""
        state = PIDState(pid_integral=float("nan"), pid_kp=float("inf"))
        healed, health = sanitize_pid_state(state, PIDParams())
        assert health == CalibratorHealth.NON_FINITE
        assert healed.pid_integral == 0.0
        assert healed.pid_kp is None

    @pytest.mark.parametrize("gain", ["kp", "ki", "kd"])
    def test_a_non_finite_gain_alone_is_dropped(self, gain):
        """Only the gain that went non-finite falls back to its default."""
        state = PIDState(pid_kp=60.0, pid_ki=0.01, pid_kd=2000.0)
        set_pid_gain(state, gain, float("nan"))
        healed, health = sanitize_pid_state(state, PIDParams())
        assert health == CalibratorHealth.NON_FINITE
        assert pid_gain(healed, gain) is None
        kept = {"kp": 60.0, "ki": 0.01, "kd": 2000.0}
        del kept[gain]
        assert {name: pid_gain(healed, name) for name in kept} == kept

    def test_each_gain_accessor_reaches_its_own_field(self):
        """The accessors read and write the field named after the gain."""
        state = PIDState()
        set_pid_gain(state, "kp", 1.0)
        set_pid_gain(state, "ki", 2.0)
        set_pid_gain(state, "kd", 3.0)
        assert (state.pid_kp, state.pid_ki, state.pid_kd) == (1.0, 2.0, 3.0)
        assert [pid_gain(state, gain) for gain in ("kp", "ki", "kd")] == [1.0, 2.0, 3.0]

    def test_runaway_gains_reset_to_defaults(self):
        """Gains far outside their bounds fall back to the configured defaults."""
        state = PIDState(pid_kp=1e9, pid_ki=0.01, pid_kd=2000.0)
        healed, health = sanitize_pid_state(state, PIDParams())
        assert health == CalibratorHealth.RUNAWAY_GAINS
        assert healed.pid_kp is None
        assert healed.pid_ki is None
        assert healed.pid_kd is None

    # The range each gain's number entity offers. A gain set anywhere in it
    # by hand is a setting the controller has to run with.
    _GAIN_RANGES = {"kp": (0.0, 1000.0), "ki": (0.0, 100.0), "kd": (0.0, 10000.0)}

    @pytest.mark.parametrize("gain", ["kp", "ki", "kd"])
    def test_the_number_offers_the_gain_range(self, gain):
        """Each gain's number spans exactly the range the controller accepts."""
        low, high = self._GAIN_RANGES[gain]
        assert _PID_GAIN_SETTINGS[gain][:2] == (low, high)

    @pytest.mark.parametrize("bound", [0, 1], ids=["lowest", "highest"])
    @pytest.mark.parametrize("gain", ["kp", "ki", "kd"])
    def test_every_gain_its_number_accepts_is_kept(self, gain, bound):
        """A gain set by hand anywhere in its number's range is not runaway.

        Auto-tuning keeps to narrower ranges; a PI controller (Kd 0) or a
        Kp below 10 is still a setting the controller has to run with.
        """
        value = self._GAIN_RANGES[gain][bound]
        gains = {"pid_kp": 60.0, "pid_ki": 0.01, "pid_kd": 2000.0}
        gains[f"pid_{gain}"] = value
        healed, health = sanitize_pid_state(PIDState(**gains), PIDParams())
        assert health == CalibratorHealth.HEALTHY
        assert getattr(healed, f"pid_{gain}") == value

    @pytest.mark.parametrize("side", ["below", "above"])
    @pytest.mark.parametrize("gain", ["kp", "ki", "kd"])
    def test_a_gain_outside_its_number_range_resets_all_gains(self, gain, side):
        """Outside the range its number offers, a gain is a poisoned state."""
        low, high = self._GAIN_RANGES[gain]
        gains = {"pid_kp": 60.0, "pid_ki": 0.01, "pid_kd": 2000.0}
        gains[f"pid_{gain}"] = low - 0.001 if side == "below" else high * 1.001
        healed, health = sanitize_pid_state(PIDState(**gains), PIDParams())
        assert health == CalibratorHealth.RUNAWAY_GAINS
        assert (healed.pid_kp, healed.pid_ki, healed.pid_kd) == (None, None, None)

    def test_windup_resets_the_integrator(self):
        """An integrator outside its clamp resets and reports WINDUP_SUSPECT."""
        state = PIDState(pid_integral=1e6, pid_kp=60.0, pid_ki=0.01, pid_kd=2000.0)
        healed, health = sanitize_pid_state(state, PIDParams())
        assert health == CalibratorHealth.WINDUP_SUSPECT
        assert healed.pid_integral == 0.0
        assert healed.pid_kp == 60.0

    def test_windup_is_healed_even_when_another_pathology_grades_first(self):
        """A wound-up integrator still resets when NON_FINITE wins the grade."""
        state = PIDState(pid_integral=1e6, pid_kp=float("inf"))
        healed, health = sanitize_pid_state(state, PIDParams())
        # The reported grade keeps the first pathology's priority...
        assert health == CalibratorHealth.NON_FINITE
        # ...but every unsafe field is still healed.
        assert healed.pid_integral == 0.0
        assert healed.pid_kp is None

    def test_non_finite_measurement_chain_is_dropped(self):
        """A non-finite stored measurement or error restarts the D channel."""
        state = PIDState(
            pid_integral=5.0,
            pid_last_meas=float("nan"),
            pid_last_error=float("-inf"),
            pid_kp=60.0,
            pid_ki=0.01,
            pid_kd=2000.0,
        )
        healed, health = sanitize_pid_state(state, PIDParams())
        assert health == CalibratorHealth.NON_FINITE
        assert healed.pid_last_meas is None
        assert healed.pid_last_error is None
        assert healed.pid_integral == 5.0
        assert healed.pid_kp == 60.0

    def test_runaway_gains_reset_when_non_finite_grades_first(self):
        """Gains outside their bounds reset under a NON_FINITE grade too."""
        state = PIDState(pid_last_meas=float("nan"), pid_kp=1e9, pid_ki=0.01)
        healed, health = sanitize_pid_state(state, PIDParams())
        assert health == CalibratorHealth.NON_FINITE
        assert (healed.pid_kp, healed.pid_ki, healed.pid_kd) == (None, None, None)
