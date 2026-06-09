"""Pure tests for the decision kernel — snapshot in, desired out, no HA."""

from datetime import UTC, datetime

from custom_components.better_thermostat.core.decide import KernelState, decide
from custom_components.better_thermostat.core.desired import DesiredState
from custom_components.better_thermostat.core.snapshot import (
    HvacMode,
    TrvReported,
    WorldSnapshot,
)


def make_snapshot(**overrides) -> WorldSnapshot:
    """Return a heating-mode snapshot with two TRVs; overridable per test."""
    defaults = {
        "now": datetime(2026, 1, 2, 8, 30, tzinfo=UTC),
        "now_monotonic": 1000.0,
        "target_temp": 21.0,
        "hvac_mode": HvacMode.HEAT,
        "room_temp": 19.5,
        "window_open": False,
        "call_for_heat": True,
        "tolerance": 0.3,
        "startup_running": False,
        "trvs": {
            "climate.trv1": TrvReported(entity_id="climate.trv1"),
            "climate.trv2": TrvReported(entity_id="climate.trv2"),
        },
    }
    defaults.update(overrides)
    return WorldSnapshot(**defaults)


class TestLifecycleGate:
    """While startup runs, the kernel commands nothing."""

    def test_startup_produces_no_intent(self):
        """During startup no TRV is addressed."""
        desired, _ = decide(make_snapshot(startup_running=True), KernelState())
        assert dict(desired.trvs) == {}

    def test_startup_gate_beats_off_mode(self):
        """The lifecycle gate sits above the mode tier."""
        desired, _ = decide(
            make_snapshot(startup_running=True, hvac_mode=HvacMode.OFF), KernelState()
        )
        assert dict(desired.trvs) == {}


class TestModeOff:
    """OFF mode turns every TRV off."""

    def test_off_turns_all_trvs_off(self):
        """Both TRVs receive an OFF intent."""
        desired, _ = decide(make_snapshot(hvac_mode=HvacMode.OFF), KernelState())
        assert set(desired.trvs) == {"climate.trv1", "climate.trv2"}
        assert all(t.hvac_mode == HvacMode.OFF for t in desired.trvs.values())

    def test_off_clears_call_for_heat(self):
        """OFF mode never calls for heat."""
        desired, _ = decide(
            make_snapshot(hvac_mode=HvacMode.OFF, call_for_heat=True), KernelState()
        )
        assert desired.call_for_heat is False

    def test_off_does_not_command_setpoints(self):
        """OFF intent carries no setpoint; translation is the shell's job."""
        desired, _ = decide(make_snapshot(hvac_mode=HvacMode.OFF), KernelState())
        assert all(t.setpoint is None for t in desired.trvs.values())


class TestWindowOpen:
    """An open window suppresses heating without changing the mode."""

    def test_window_open_turns_all_trvs_off(self):
        """Both TRVs receive an OFF intent while the window is open."""
        desired, _ = decide(make_snapshot(window_open=True), KernelState())
        assert all(t.hvac_mode == HvacMode.OFF for t in desired.trvs.values())

    def test_window_open_keeps_call_for_heat(self):
        """The room may still want heat; only the command is suppressed."""
        desired, _ = decide(
            make_snapshot(window_open=True, call_for_heat=True), KernelState()
        )
        assert desired.call_for_heat is True

    def test_mode_off_wins_over_window(self):
        """OFF mode (call_for_heat False) has precedence over the window tier."""
        desired, _ = decide(
            make_snapshot(hvac_mode=HvacMode.OFF, window_open=True), KernelState()
        )
        assert desired.call_for_heat is False


class TestPurity:
    """decide() is a pure function of its inputs."""

    def test_same_inputs_same_output(self):
        """Two identical calls produce equal DesiredStates."""
        a, _ = decide(make_snapshot(window_open=True), KernelState())
        b, _ = decide(make_snapshot(window_open=True), KernelState())
        assert a == b

    def test_state_is_returned(self):
        """The threaded state is handed back to the caller."""
        state = KernelState()
        _, state_out = decide(make_snapshot(), KernelState())
        assert isinstance(state_out, KernelState)
        _, same_state = decide(make_snapshot(), state)
        assert same_state is state

    def test_default_result_is_neutral(self):
        """With no upper tier firing, the kernel produces no intent yet."""
        desired, _ = decide(make_snapshot(), KernelState())
        assert desired == DesiredState(call_for_heat=True)


class TestMaintenancePreempt:
    """Valve maintenance pre-empts control entirely."""

    def test_maintenance_produces_no_intent(self):
        """During maintenance no TRV is addressed."""
        desired, _ = decide(make_snapshot(in_maintenance=True), KernelState())
        assert dict(desired.trvs) == {}

    def test_maintenance_beats_off_mode(self):
        """Even OFF mode is not commanded while maintenance owns the valves."""
        desired, _ = decide(
            make_snapshot(in_maintenance=True, hvac_mode=HvacMode.OFF), KernelState()
        )
        assert dict(desired.trvs) == {}


class TestReachability:
    """Unreachable TRVs receive no intent."""

    def _snapshot(self, **overrides):
        return make_snapshot(
            trvs={
                "climate.up": TrvReported(entity_id="climate.up", available=True),
                "climate.down": TrvReported(entity_id="climate.down", available=False),
            },
            **overrides,
        )

    def test_offline_trv_is_skipped_in_off_mode(self):
        """Only the reachable TRV is addressed when the mode is OFF."""
        desired, _ = decide(self._snapshot(hvac_mode=HvacMode.OFF), KernelState())
        assert set(desired.trvs) == {"climate.up"}

    def test_offline_trv_is_skipped_on_open_window(self):
        """Only the reachable TRV is addressed while the window is open."""
        desired, _ = decide(self._snapshot(window_open=True), KernelState())
        assert set(desired.trvs) == {"climate.up"}

    def test_boost_keeps_commanding_offline_trvs(self):
        """Active boost heating overrides the reachability skip."""
        desired, _ = decide(
            self._snapshot(
                window_open=True, preset_mode="boost", room_temp=18.0, target_temp=22.0
            ),
            KernelState(),
        )
        assert set(desired.trvs) == {"climate.up", "climate.down"}

    def test_boost_without_heat_demand_does_not_override(self):
        """Boost at/above target does not force-command offline TRVs."""
        desired, _ = decide(
            self._snapshot(
                window_open=True, preset_mode="boost", room_temp=22.5, target_temp=22.0
            ),
            KernelState(),
        )
        assert set(desired.trvs) == {"climate.up"}


class TestDegradedIsAnnunciationOnly:
    """Characterization: degraded mode does not alter the control law (pre-M8)."""

    def test_degraded_changes_nothing_in_off_mode(self):
        """The OFF decision is identical with and without degraded."""
        a, _ = decide(make_snapshot(hvac_mode=HvacMode.OFF), KernelState())
        b, _ = decide(
            make_snapshot(hvac_mode=HvacMode.OFF, degraded=True), KernelState()
        )
        assert a == b

    def test_degraded_changes_nothing_in_heating_branch(self):
        """The default decision is identical with and without degraded."""
        a, _ = decide(make_snapshot(), KernelState())
        b, _ = decide(make_snapshot(degraded=True), KernelState())
        assert a == b
