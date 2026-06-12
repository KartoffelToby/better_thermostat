"""Pure tests for the decision kernel — snapshot in, desired out, no HA."""

from datetime import timedelta

from custom_components.better_thermostat.core.decide import KernelState, decide
from custom_components.better_thermostat.core.desired import Suppression
from custom_components.better_thermostat.core.fsm.control_mode import (
    ControlMode,
    ControlModeState,
)
from custom_components.better_thermostat.core.fsm.lifecycle import (
    LifecyclePhase,
    LifecycleState,
)
from custom_components.better_thermostat.core.fsm.mode import ModeState
from custom_components.better_thermostat.core.fsm.window import WindowPhase, WindowState
from custom_components.better_thermostat.core.snapshot import HvacMode, TrvReported
from tests.factories import make_snapshot, make_state


class TestLifecycleGate:
    """While startup runs, the kernel commands nothing."""

    def test_startup_produces_no_intent(self):
        """During startup (INITIALISING region) no TRV is addressed."""
        desired, _ = decide(make_snapshot(), make_state(lifecycle=LifecycleState()))
        assert dict(desired.trvs) == {}

    def test_startup_gate_beats_off_mode(self):
        """The lifecycle gate sits above the mode tier."""
        desired, _ = decide(
            make_snapshot(),
            make_state(
                lifecycle=LifecycleState(), mode=ModeState(hvac_mode=HvacMode.OFF)
            ),
        )
        assert dict(desired.trvs) == {}

    def test_starting_promotes_to_running_after_grace(self):
        """decide() ticks the lifecycle region without a dedicated caller.

        STARTING becomes RUNNING once the grace window has passed.
        """
        snapshot = make_snapshot()
        state = make_state(
            lifecycle=LifecycleState(
                phase=LifecyclePhase.STARTING,
                grace_until=snapshot.now - timedelta(seconds=1),
            )
        )
        _, state_after = decide(snapshot, state)
        assert state_after.lifecycle.phase == LifecyclePhase.RUNNING

    def test_starting_stays_within_grace(self):
        """STARTING holds while the grace deadline is in the future."""
        snapshot = make_snapshot()
        state = make_state(
            lifecycle=LifecycleState(
                phase=LifecyclePhase.STARTING,
                grace_until=snapshot.now + timedelta(minutes=5),
            )
        )
        _, state_after = decide(snapshot, state)
        assert state_after.lifecycle.phase == LifecyclePhase.STARTING


class TestModeOff:
    """OFF mode turns every TRV off."""

    def test_off_turns_all_trvs_off(self):
        """Both TRVs receive an OFF intent."""
        desired, _ = decide(
            make_snapshot(), make_state(mode=ModeState(hvac_mode=HvacMode.OFF))
        )
        assert set(desired.trvs) == {"climate.trv1", "climate.trv2"}
        assert all(t.hvac_mode == HvacMode.OFF for t in desired.trvs.values())

    def test_off_clears_call_for_heat(self):
        """OFF mode never calls for heat."""
        desired, _ = decide(
            make_snapshot(call_for_heat=True),
            make_state(mode=ModeState(hvac_mode=HvacMode.OFF)),
        )
        assert desired.call_for_heat is False

    def test_off_does_not_command_setpoints(self):
        """OFF intent carries no setpoint; translation is the shell's job."""
        desired, _ = decide(
            make_snapshot(), make_state(mode=ModeState(hvac_mode=HvacMode.OFF))
        )
        assert all(t.setpoint is None for t in desired.trvs.values())


class TestWindowOpen:
    """An open window suppresses heating without changing the mode."""

    def test_window_open_turns_all_trvs_off(self):
        """Both TRVs receive an OFF intent while the window is open."""
        desired, _ = decide(
            make_snapshot(), make_state(window=WindowState(phase=WindowPhase.OPEN))
        )
        assert all(t.hvac_mode == HvacMode.OFF for t in desired.trvs.values())

    def test_window_open_keeps_call_for_heat(self):
        """The room may still want heat; only the command is suppressed."""
        desired, _ = decide(
            make_snapshot(call_for_heat=True),
            make_state(window=WindowState(phase=WindowPhase.OPEN)),
        )
        assert desired.call_for_heat is True

    def test_mode_off_wins_over_window(self):
        """OFF mode (call_for_heat False) has precedence over the window tier."""
        desired, _ = decide(
            make_snapshot(),
            make_state(
                mode=ModeState(hvac_mode=HvacMode.OFF),
                window=WindowState(phase=WindowPhase.OPEN),
            ),
        )
        assert desired.call_for_heat is False


class TestPurity:
    """decide() is a pure function of its inputs."""

    def test_decide_does_not_mutate_the_input_state(self):
        """The pre-decide state stays pristine; the result is a new object.

        The flight recorder relies on this: it records the input state
        after the decision and must see the state before it.
        """
        state = make_state()
        snapshot = make_snapshot()
        _, new_state = decide(snapshot, state)
        assert state.reachability == {}
        assert new_state is not state
        assert set(new_state.reachability) == {"climate.trv1", "climate.trv2"}

    def test_same_inputs_same_output(self):
        """Two identical calls produce equal DesiredStates."""
        a, _ = decide(
            make_snapshot(), make_state(window=WindowState(phase=WindowPhase.OPEN))
        )
        b, _ = decide(
            make_snapshot(), make_state(window=WindowState(phase=WindowPhase.OPEN))
        )
        assert a == b

    def test_state_is_returned(self):
        """A successor state carrying the input regions is handed back."""
        state = make_state()
        _, state_out = decide(make_snapshot(), state)
        assert isinstance(state_out, KernelState)
        assert state_out is not state
        assert state_out.mode is state.mode
        assert state_out.window is state.window

    def test_default_result_is_the_heating_branch(self):
        """With no upper tier firing, the kernel asks the TRVs to heat."""
        desired, _ = decide(make_snapshot(), make_state())
        assert desired.call_for_heat is True
        assert all(t.hvac_mode == HvacMode.HEAT for t in desired.trvs.values())


class TestMaintenancePreempt:
    """Valve maintenance pre-empts control entirely."""

    def _running_maintenance(self):
        from custom_components.better_thermostat.core.fsm.maintenance import (
            MaintenancePhase,
            MaintenanceState,
        )

        return MaintenanceState(phase=MaintenancePhase.RUNNING, running_since=900.0)

    def test_maintenance_produces_no_intent(self):
        """During maintenance no TRV is addressed."""
        desired, _ = decide(
            make_snapshot(), make_state(maintenance=self._running_maintenance())
        )
        assert dict(desired.trvs) == {}

    def test_maintenance_beats_off_mode(self):
        """Even OFF mode is not commanded while maintenance owns the valves."""
        desired, _ = decide(
            make_snapshot(),
            make_state(
                maintenance=self._running_maintenance(),
                mode=ModeState(hvac_mode=HvacMode.OFF),
            ),
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
        desired, _ = decide(
            self._snapshot(), make_state(mode=ModeState(hvac_mode=HvacMode.OFF))
        )
        assert set(desired.trvs) == {"climate.up"}

    def test_offline_trv_is_skipped_on_open_window(self):
        """Only the reachable TRV is addressed while the window is open."""
        desired, _ = decide(
            self._snapshot(), make_state(window=WindowState(phase=WindowPhase.OPEN))
        )
        assert set(desired.trvs) == {"climate.up"}

    def test_boost_keeps_commanding_offline_trvs(self):
        """Active boost heating overrides the reachability skip."""
        desired, _ = decide(
            self._snapshot(preset_mode="boost", room_temp=18.0, target_temp=22.0),
            make_state(window=WindowState(phase=WindowPhase.OPEN)),
        )
        assert set(desired.trvs) == {"climate.up", "climate.down"}

    def test_boost_without_heat_demand_does_not_override(self):
        """Boost at/above target does not force-command offline TRVs."""
        desired, _ = decide(
            self._snapshot(preset_mode="boost", room_temp=22.5, target_temp=22.0),
            make_state(window=WindowState(phase=WindowPhase.OPEN)),
        )
        assert set(desired.trvs) == {"climate.up"}


class TestDegradedIsAnnunciationOnly:
    """Characterization: degraded annunciation does not alter the control law.

    Unavailable optional sensors are annunciated through the
    control-mode region; only the ladder rung (HOLD) has an effect.
    """

    def _degraded_region(self):
        return ControlModeState(unavailable_sensors=("sensor.outdoor",))

    def test_degraded_changes_nothing_in_off_mode(self):
        """The OFF decision is identical with and without degraded."""
        a, _ = decide(
            make_snapshot(), make_state(mode=ModeState(hvac_mode=HvacMode.OFF))
        )
        b, _ = decide(
            make_snapshot(),
            make_state(
                mode=ModeState(hvac_mode=HvacMode.OFF),
                control_mode=self._degraded_region(),
            ),
        )
        assert a == b

    def test_degraded_changes_nothing_in_heating_branch(self):
        """The default decision is identical with and without degraded."""
        a, _ = decide(make_snapshot(), make_state())
        b, _ = decide(make_snapshot(), make_state(control_mode=self._degraded_region()))
        assert a == b


class TestCallForHeat:
    """Without heat demand every addressed TRV is turned off."""

    def test_no_call_for_heat_turns_trvs_off(self):
        """call_for_heat False yields OFF intents."""
        desired, _ = decide(make_snapshot(call_for_heat=False), make_state())
        assert desired.call_for_heat is False
        assert all(t.hvac_mode == HvacMode.OFF for t in desired.trvs.values())

    def test_window_tier_wins_over_call_for_heat(self):
        """An open window decides before the call-for-heat tier."""
        desired, _ = decide(
            make_snapshot(call_for_heat=False),
            make_state(window=WindowState(phase=WindowPhase.OPEN)),
        )
        # Window tier reports the room's heat demand unchanged.
        assert desired.call_for_heat is False
        assert all(t.hvac_mode == HvacMode.OFF for t in desired.trvs.values())


class TestHeatingBranch:
    """With heat demand the addressed TRVs are asked to heat to the target."""

    def test_heating_intent_carries_mode_and_target(self):
        """Each reachable TRV heats towards the room target."""
        desired, _ = decide(make_snapshot(), make_state())
        assert desired.call_for_heat is True
        assert set(desired.trvs) == {"climate.trv1", "climate.trv2"}
        for trv in desired.trvs.values():
            assert trv.hvac_mode == HvacMode.HEAT
            assert trv.setpoint == 21.0

    def test_heating_skips_unreachable_trvs(self):
        """Unreachable TRVs get no heating intent."""
        desired, _ = decide(
            make_snapshot(
                trvs={
                    "climate.up": TrvReported(entity_id="climate.up", available=True),
                    "climate.down": TrvReported(
                        entity_id="climate.down", available=False
                    ),
                }
            ),
            make_state(),
        )
        assert set(desired.trvs) == {"climate.up"}

    def test_heat_cool_mode_is_passed_through(self):
        """HEAT_COOL intent reaches the TRVs unchanged."""
        desired, _ = decide(
            make_snapshot(), make_state(mode=ModeState(hvac_mode=HvacMode.HEAT_COOL))
        )
        assert all(t.hvac_mode == HvacMode.HEAT_COOL for t in desired.trvs.values())


class TestSuppression:
    """OFF intents carry why they are OFF.

    The shell needs the distinction to pick between a literal OFF
    (suppression) and the device-specific remap of the BT mode; it must
    not re-derive it from the kernel's regions.
    """

    def test_window_off_is_marked_as_suppression(self):
        """An open window suppresses heating."""
        desired, _ = decide(
            make_snapshot(), make_state(window=WindowState(phase=WindowPhase.OPEN))
        )
        assert all(t.suppression == Suppression.WINDOW for t in desired.trvs.values())

    def test_no_heat_demand_is_marked_as_suppression(self):
        """Missing heat demand suppresses heating."""
        desired, _ = decide(make_snapshot(call_for_heat=False), make_state())
        assert all(
            t.suppression == Suppression.NO_CALL_FOR_HEAT for t in desired.trvs.values()
        )

    def test_mode_off_is_not_a_suppression(self):
        """OFF as the selected mode is the mode, not a suppression."""
        desired, _ = decide(
            make_snapshot(), make_state(mode=ModeState(hvac_mode=HvacMode.OFF))
        )
        assert all(t.suppression is None for t in desired.trvs.values())

    def test_heating_carries_no_suppression(self):
        """Heating intents carry no suppression."""
        desired, _ = decide(make_snapshot(), make_state())
        assert all(t.suppression is None for t in desired.trvs.values())


class TestHoldRung:
    """Under HOLD the kernel keeps the mode but adjusts nothing."""

    def test_hold_emits_raw_target_passthrough(self):
        """The heating tier locks the raw user target under HOLD.

        No calibration numbers (valve, offset) — but the setpoint
        carries the last known target so the device stays locked on it
        and a device-side loss heals; the safety hull enforces the
        frost floor.
        """
        desired, _ = decide(
            make_snapshot(),
            make_state(control_mode=ControlModeState(mode=ControlMode.HOLD)),
        )
        assert set(desired.trvs) == {"climate.trv1", "climate.trv2"}
        for trv in desired.trvs.values():
            assert trv.hvac_mode == HvacMode.HEAT
            assert trv.setpoint == 21.0
            assert trv.valve_percent is None
            assert trv.offset is None

    def test_hold_keeps_mode_suppression(self):
        """The OFF/window tiers stay above the rung."""
        desired, _ = decide(
            make_snapshot(),
            make_state(
                window=WindowState(phase=WindowPhase.OPEN),
                control_mode=ControlModeState(mode=ControlMode.HOLD),
            ),
        )
        assert all(t.hvac_mode == HvacMode.OFF for t in desired.trvs.values())

    def test_sensor_fallback_keeps_the_setpoint(self):
        """SENSOR_FALLBACK still adjusts — only HOLD pauses the numbers."""
        desired, _ = decide(
            make_snapshot(),
            make_state(control_mode=ControlModeState(mode=ControlMode.SENSOR_FALLBACK)),
        )
        assert all(t.setpoint == 21.0 for t in desired.trvs.values())


class TestRegionIntegration:
    """decide() consumes the FSM regions threaded through KernelState."""

    def test_reachability_regions_are_advanced(self):
        """Each TRV's reachability region is stepped from the snapshot."""
        state = make_state()
        snapshot = make_snapshot(
            trvs={
                "climate.up": TrvReported(entity_id="climate.up", available=True),
                "climate.down": TrvReported(entity_id="climate.down", available=False),
            }
        )
        _, state = decide(snapshot, state)
        assert state.reachability["climate.up"].online is True
        assert state.reachability["climate.down"].online is False
        assert state.reachability["climate.down"].offline_since == 1000.0

    def test_recovered_trv_resets_its_region(self):
        """A TRV reporting available again resets its reachability region."""
        state = make_state()
        offline = make_snapshot(
            trvs={"climate.t": TrvReported(entity_id="climate.t", available=False)}
        )
        _, state = decide(offline, state)
        assert state.reachability["climate.t"].online is False

        online = make_snapshot(
            trvs={"climate.t": TrvReported(entity_id="climate.t", available=True)}
        )
        _, state = decide(online, state)
        assert state.reachability["climate.t"].online is True

    def test_window_region_suppresses_heating(self):
        """An OPEN window region turns the TRVs off."""
        state = make_state(window=WindowState(phase=WindowPhase.OPEN))
        desired, _ = decide(make_snapshot(), state)
        assert all(t.hvac_mode == HvacMode.OFF for t in desired.trvs.values())

    def test_stale_maintenance_run_stops_blocking(self):
        """The maintenance liveness invariant reaches the kernel gate."""
        from custom_components.better_thermostat.core.fsm.maintenance import (
            MaintenancePhase,
            MaintenanceState,
        )

        running = MaintenanceState(
            phase=MaintenancePhase.RUNNING, running_since=-10_000.0
        )
        state = make_state(maintenance=running)
        desired, _ = decide(make_snapshot(), state)
        # running_since is 11000 s before now_monotonic=1000 -> stale -> not blocking
        assert desired.trvs != {}

    def test_fresh_maintenance_run_blocks(self):
        """A live RUNNING maintenance region pre-empts control."""
        from custom_components.better_thermostat.core.fsm.maintenance import (
            MaintenancePhase,
            MaintenanceState,
        )

        running = MaintenanceState(phase=MaintenancePhase.RUNNING, running_since=900.0)
        state = make_state(maintenance=running)
        desired, _ = decide(make_snapshot(), state)
        assert dict(desired.trvs) == {}
