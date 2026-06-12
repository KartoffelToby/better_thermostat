"""Tests for the calibrator contract: capability nesting, strategies, healing."""

from unittest.mock import MagicMock

import pytest

from custom_components.better_thermostat.core.calibrator import (
    Calibrator,
    CalibratorHealth,
    Capability,
)
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.calibration.pid import (
    PIDParams,
    PIDState,
    sanitize_pid_state,
)
from custom_components.better_thermostat.utils.calibration.strategies import (
    BalanceCalibrator,
    build_strategy_registry,
)
from custom_components.better_thermostat.utils.const import CalibrationMode


class TestCapabilityNesting:
    """ready implies healthy implies configured, by construction."""

    def test_valid_levels(self):
        """All nested combinations construct fine."""
        Capability()
        Capability(configured=True)
        Capability(configured=True, healthy=True)
        Capability(configured=True, healthy=True, ready=True)

    def test_ready_requires_healthy(self):
        """Ready without healthy is rejected."""
        with pytest.raises(ValueError):
            Capability(configured=True, ready=True)

    def test_healthy_requires_configured(self):
        """Healthy without configured is rejected."""
        with pytest.raises(ValueError):
            Capability(healthy=True)


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
        return Capability(configured=True, healthy=self._ready, ready=self._ready)

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
        result = MagicMock()
        result.valve_percent = percent
        result.duty_cycle_pct = percent

        def compute(bt, entity_id):
            return result, use_valve

        def compute_pid(bt, entity_id):
            return percent, use_valve

        return build_strategy_registry(compute, compute, compute_pid)

    def test_modes_are_covered(self):
        """MPC, TPI, and PID have strategies; DEFAULT does not."""
        registry = self._registry()
        assert set(registry) == {
            CalibrationMode.MPC_CALIBRATION,
            CalibrationMode.TPI_CALIBRATION,
            CalibrationMode.PID_CALIBRATION,
        }

    @pytest.mark.parametrize(
        "mode",
        [
            CalibrationMode.MPC_CALIBRATION,
            CalibrationMode.TPI_CALIBRATION,
            CalibrationMode.PID_CALIBRATION,
        ],
    )
    def test_run_extracts_the_percent(self, mode):
        """Each strategy reads its own result shape into a plain percent."""
        registry = self._registry(percent=55.0)
        percent, use_valve = registry[mode].run(MagicMock(), "climate.trv")
        assert percent == 55.0
        assert use_valve is False

    def test_none_result_yields_no_percent(self):
        """A failed computation yields (None, use_valve)."""
        registry = build_strategy_registry(
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

        bt = MagicMock()
        bt.cur_temp = 20.0
        bt.bt_target_temp = 21.0
        bt.real_trvs = {"climate.trv": Trv(entity_id="climate.trv")}

        cap = strategy.capability(bt, "climate.trv")
        assert cap.configured and cap.healthy and not cap.ready

        bt.real_trvs["climate.trv"].calibration_balance = {"valve_percent": 40}
        cap = strategy.capability(bt, "climate.trv")
        assert cap.configured and cap.healthy and cap.ready

        bt.cur_temp = None
        cap = strategy.capability(bt, "climate.trv")
        assert cap.configured and not cap.healthy and not cap.ready


class TestBalanceCalibrator:
    """The production adapter lifts a BalanceStrategy onto the protocol."""

    def _adapter(self, *, percent=55.0, use_valve=False, balance=None):
        registry = build_strategy_registry(
            lambda bt, e: (MagicMock(valve_percent=percent), use_valve),
            lambda bt, e: (MagicMock(duty_cycle_pct=percent), use_valve),
            lambda bt, e: (percent, use_valve),
        )
        bt = MagicMock()
        bt.cur_temp = 20.0
        bt.bt_target_temp = 21.0
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
        cap = adapter.capability()
        assert cap.configured and cap.healthy and cap.ready
        bt.cur_temp = None
        assert adapter.capability().healthy is False

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

    def test_runaway_gains_reset_to_defaults(self):
        """Gains far outside their bounds fall back to the configured defaults."""
        state = PIDState(pid_kp=1e9, pid_ki=0.01, pid_kd=2000.0)
        healed, health = sanitize_pid_state(state, PIDParams())
        assert health == CalibratorHealth.RUNAWAY_GAINS
        assert healed.pid_kp is None
        assert healed.pid_ki is None
        assert healed.pid_kd is None

    def test_windup_resets_the_integrator(self):
        """An integrator outside its clamp resets and reports WINDUP_SUSPECT."""
        state = PIDState(pid_integral=1e6, pid_kp=60.0, pid_ki=0.01, pid_kd=2000.0)
        healed, health = sanitize_pid_state(state, PIDParams())
        assert health == CalibratorHealth.WINDUP_SUSPECT
        assert healed.pid_integral == 0.0
        assert healed.pid_kp == 60.0
