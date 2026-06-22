"""Tests for the flight recorder: ring buffer, export, deterministic replay."""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
import json

import pytest

from custom_components.better_thermostat.core.decide import decide, running_kernel_state
from custom_components.better_thermostat.core.fsm.window import WindowPhase, WindowState
from custom_components.better_thermostat.core.recorder import (
    FlightRecorder,
    replay,
    snapshot_from_dict,
    state_from_dict,
)
from custom_components.better_thermostat.core.snapshot import (
    HvacMode,
    TrvReported,
    WorldSnapshot,
)


def _snapshot(target=21.0) -> WorldSnapshot:
    return WorldSnapshot(
        now=datetime(2026, 1, 10, 7, 0, tzinfo=UTC),
        now_monotonic=1000.0,
        target_temp=target,
        hvac_mode=HvacMode.HEAT,
        room_temp=19.0,
        call_for_heat=True,
        tolerance=0.3,
        trvs={
            "climate.trv": TrvReported(
                entity_id="climate.trv",
                available=True,
                hvac_mode=HvacMode.HEAT,
                current_temp=20.0,
                setpoint=21.0,
                min_temp=5.0,
                max_temp=30.0,
                valve_max_opening=80.0,
            )
        },
    )


def _record_one(recorder: FlightRecorder, snapshot: WorldSnapshot) -> None:
    state = replace(
        running_kernel_state(), window=WindowState(phase=WindowPhase.CLOSED)
    )
    desired, _ = decide(snapshot, state)
    recorder.record(snapshot, running_kernel_state(), desired)


class TestRingBuffer:
    """The buffer is bounded and ordered oldest-first."""

    def test_capacity_evicts_oldest(self):
        """Overflow drops the oldest entries."""
        recorder = FlightRecorder(capacity=3)
        for target in (20.0, 21.0, 22.0, 23.0):
            _record_one(recorder, _snapshot(target=target))
        assert len(recorder) == 3
        exported = recorder.export()
        assert [e["snapshot"]["target_temp"] for e in exported] == [21.0, 22.0, 23.0]

    def test_pre_decide_state_is_immutable(self):
        """The recorded kernel state cannot drift under the record.

        The aggregate is frozen, so later in-place mutation is
        impossible and rebinding the live variable leaves the record
        untouched.
        """
        recorder = FlightRecorder()
        state = running_kernel_state()
        snapshot = _snapshot()
        desired, state = decide(snapshot, state)
        recorder.record(snapshot, state, desired)
        with pytest.raises(FrozenInstanceError):
            state.last_control_monotonic = 999.0
        state = replace(state, last_control_monotonic=999.0)
        assert recorder.export()[0]["state"]["last_control_monotonic"] is None


class TestExport:
    """The export is JSON-serializable and structurally complete."""

    def test_export_survives_json(self):
        """The exported buffer round-trips through json.dumps."""
        recorder = FlightRecorder()
        _record_one(recorder, _snapshot())
        payload = json.loads(json.dumps(recorder.export()))
        entry = payload[0]
        assert entry["snapshot"]["trvs"]["climate.trv"]["current_temp"] == 20.0
        assert entry["desired"]["trvs"]["climate.trv"]["hvac_mode"] == "heat"
        assert entry["state"]["window"]["phase"] == "closed"


class TestReplay:
    """An exported tuple reproduces the decision deterministically."""

    def test_replay_matches_the_recorded_decision(self):
        """The kernel recomputes exactly what was recorded."""
        recorder = FlightRecorder()
        state = running_kernel_state()
        snapshot = _snapshot()
        desired, _ = decide(snapshot, running_kernel_state())
        recorder.record(snapshot, state, desired)

        entry = json.loads(json.dumps(recorder.export()))[0]
        matches, recomputed = replay(entry)
        assert matches is True
        assert recomputed.trvs["climate.trv"].hvac_mode == HvacMode.HEAT

    def test_replay_detects_a_diverging_record(self):
        """A tampered desired no longer matches the recomputation."""
        recorder = FlightRecorder()
        desired, _ = decide(_snapshot(), running_kernel_state())
        recorder.record(_snapshot(), running_kernel_state(), desired)
        entry = json.loads(json.dumps(recorder.export()))[0]
        entry["desired"]["call_for_heat"] = False
        matches, _ = replay(entry)
        assert matches is False

    def test_reconstruction_is_lossless_for_the_kernel(self):
        """Snapshot and state reconstruct into equal kernel inputs."""
        recorder = FlightRecorder()
        snapshot = _snapshot()
        desired, _ = decide(snapshot, running_kernel_state())
        recorder.record(snapshot, running_kernel_state(), desired)
        entry = json.loads(json.dumps(recorder.export()))[0]

        assert snapshot_from_dict(entry["snapshot"]) == snapshot
        rebuilt = state_from_dict(entry["state"])
        assert rebuilt == running_kernel_state()


class TestReplayValidation:
    """Malformed exports fail loudly instead of replaying garbage."""

    def _entry(self):
        recorder = FlightRecorder()
        desired, _ = decide(_snapshot(), running_kernel_state())
        recorder.record(_snapshot(), running_kernel_state(), desired)
        return json.loads(json.dumps(recorder.export()))[0]

    def test_missing_now_is_rejected(self):
        """A snapshot without a parseable 'now' raises."""
        entry = self._entry()
        entry["snapshot"]["now"] = None
        with pytest.raises(ValueError, match="now"):
            replay(entry)

    def test_wrong_container_types_are_rejected(self):
        """Non-mapping sections raise instead of replaying."""
        entry = self._entry()
        entry["state"]["window"] = "not-a-dict"
        with pytest.raises(ValueError, match="mapping"):
            replay(entry)

    def test_wrong_scalar_types_are_rejected(self):
        """Strings where numbers or bools belong raise."""
        entry = self._entry()
        entry["snapshot"]["call_for_heat"] = "yes"
        with pytest.raises(ValueError, match="bool"):
            replay(entry)

        entry = self._entry()
        entry["snapshot"]["tolerance"] = "warm"
        with pytest.raises(ValueError, match="number"):
            replay(entry)

        entry = self._entry()
        entry["snapshot"]["tolerance"] = None
        with pytest.raises(ValueError, match="number"):
            replay(entry)

        entry = self._entry()
        entry["state"]["window"]["phase"] = 7
        with pytest.raises(ValueError, match="string"):
            replay(entry)

    def test_wrong_reachability_types_are_rejected(self):
        """A non-integer retry count raises."""
        entry = self._entry()
        entry["state"]["reachability"] = {
            "climate.trv": {
                "online": True,
                "offline_since": None,
                "retry_count": "three",
                "retry_at": None,
            }
        }
        with pytest.raises(ValueError, match="integer"):
            replay(entry)

    def test_unavailable_sensors_must_be_a_list(self):
        """A scalar where the sensor list belongs raises."""
        entry = self._entry()
        entry["state"]["control_mode"]["unavailable_sensors"] = "sensor.x"
        with pytest.raises(ValueError, match="list"):
            replay(entry)


def test_replay_roundtrips_reachability_and_null_window_state():
    """Reachability entries survive the roundtrip."""
    from custom_components.better_thermostat.core.snapshot import (
        TrvReported as _TrvReported,
    )

    recorder = FlightRecorder()
    snapshot = WorldSnapshot(
        now=datetime(2026, 1, 10, 7, 0, tzinfo=UTC),
        now_monotonic=1000.0,
        target_temp=21.0,
        hvac_mode=HvacMode.HEAT,
        room_temp=19.0,
        call_for_heat=True,
        trvs={"climate.t": _TrvReported(entity_id="climate.t", available=False)},
    )
    state = running_kernel_state()
    desired, state = decide(snapshot, state)
    recorder.record(snapshot, running_kernel_state(), desired)
    # Record a second tuple whose pre-decide state carries reachability.
    desired2, _ = decide(snapshot, state)
    recorder.record(snapshot, state, desired2)

    entry = json.loads(json.dumps(recorder.export()))[1]
    matches, _ = replay(entry)
    assert matches is True
    rebuilt = state_from_dict(entry["state"])
    assert rebuilt.reachability["climate.t"].online is False


class TestRoundtripCompleteness:
    """Every field of every recorded type survives export and reconstruct.

    The kwargs below deliberately set every field to a non-default value
    and the field-set assertions force this test to grow with the
    dataclasses — a field missing from the reconstructors cannot drift
    past it silently.
    """

    def test_every_field_roundtrips(self):
        """Fully populated decision tuples reconstruct exactly."""
        from dataclasses import fields

        from custom_components.better_thermostat.core.decide import KernelState
        from custom_components.better_thermostat.core.desired import (
            DesiredState,
            Suppression,
            TrvDesired,
        )
        from custom_components.better_thermostat.core.fsm.control_mode import (
            ControlMode,
            ControlModeState,
        )
        from custom_components.better_thermostat.core.fsm.lifecycle import (
            LifecyclePhase,
            LifecycleState,
        )
        from custom_components.better_thermostat.core.fsm.maintenance import (
            MaintenancePhase,
            MaintenanceState,
        )
        from custom_components.better_thermostat.core.fsm.mode import ModeState
        from custom_components.better_thermostat.core.fsm.reachability import (
            ReachabilityState,
        )
        from custom_components.better_thermostat.core.recorder import desired_from_dict

        trv_reported_kwargs = {
            "entity_id": "climate.trv",
            "available": False,
            "hvac_mode": HvacMode.HEAT,
            "current_temp": 20.5,
            "setpoint": 21.5,
            "min_temp": 6.0,
            "max_temp": 29.0,
            "valve_max_opening": 80.0,
            "local_calibration_min": -4.0,
            "local_calibration_max": 4.0,
        }
        assert set(trv_reported_kwargs) == {f.name for f in fields(TrvReported)}

        snapshot_kwargs = {
            "now": datetime(2026, 1, 10, 7, 0, tzinfo=UTC),
            "now_monotonic": 1000.0,
            "target_temp": 21.0,
            "target_cooltemp": 24.0,
            "hvac_mode": HvacMode.HEAT,
            "room_temp": 19.0,
            "room_temp_filtered": 19.1,
            "temp_slope": 0.02,
            "call_for_heat": True,
            "window_open": True,
            "preset_mode": "eco",
            "tolerance": 0.3,
            "outdoor_temp": 5.5,
            "is_day": False,
            "solar_intensity": 0.4,
            "min_temp": 5.0,
            "max_temp": 30.0,
            "trvs": {"climate.trv": TrvReported(**trv_reported_kwargs)},
        }
        assert set(snapshot_kwargs) == {f.name for f in fields(WorldSnapshot)}
        snapshot = WorldSnapshot(**snapshot_kwargs)

        state_kwargs = {
            "window": WindowState(phase=WindowPhase.OPENING, pending_since=900.0),
            "maintenance": MaintenanceState(
                phase=MaintenancePhase.RUNNING,
                next_due=datetime(2026, 2, 1, tzinfo=UTC),
                running_since=950.0,
            ),
            "lifecycle": LifecycleState(
                phase=LifecyclePhase.RUNNING,
                grace_until=datetime(2026, 1, 10, 8, 0, tzinfo=UTC),
            ),
            "mode": ModeState(hvac_mode=HvacMode.HEAT, preset="eco"),
            "control_mode": ControlModeState(
                mode=ControlMode.SENSOR_FALLBACK,
                unavailable_sensors=("sensor.room",),
                degraded_since=800.0,
                down_pending_since=810.0,
                up_pending_since=820.0,
            ),
            "reachability": {
                "climate.trv": ReachabilityState(
                    online=False, offline_since=700.0, retry_count=2, retry_at=1100.0
                )
            },
            "last_control_monotonic": 990.0,
        }
        assert set(state_kwargs) == {f.name for f in fields(KernelState)}
        state = KernelState(**state_kwargs)

        trv_desired_kwargs = {
            "entity_id": "climate.trv",
            "hvac_mode": HvacMode.HEAT,
            "setpoint": 21.5,
            "valve_percent": 60.0,
            "offset": -1.5,
            "suppression": Suppression.WINDOW,
        }
        assert set(trv_desired_kwargs) == {f.name for f in fields(TrvDesired)}
        desired = DesiredState(
            call_for_heat=True, trvs={"climate.trv": TrvDesired(**trv_desired_kwargs)}
        )

        recorder = FlightRecorder()
        recorder.record(snapshot, state, desired)
        entry = json.loads(json.dumps(recorder.export()))[0]

        assert snapshot_from_dict(entry["snapshot"]) == snapshot
        assert state_from_dict(entry["state"]) == state
        assert desired_from_dict(entry["desired"]) == desired
