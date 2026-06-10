"""Tests for the flight recorder: ring buffer, export, deterministic replay."""

from datetime import UTC, datetime
import json

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


def _snapshot(target=21.0, window_open=False) -> WorldSnapshot:
    return WorldSnapshot(
        now=datetime(2026, 1, 10, 7, 0, tzinfo=UTC),
        now_monotonic=1000.0,
        target_temp=target,
        hvac_mode=HvacMode.HEAT,
        room_temp=19.0,
        window_open=window_open,
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
    state = running_kernel_state()
    state.window = WindowState(phase=WindowPhase.CLOSED)
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

    def test_pre_decide_state_is_copied(self):
        """Mutating the live kernel state later does not alter the record."""
        recorder = FlightRecorder()
        state = running_kernel_state()
        snapshot = _snapshot()
        desired, state = decide(snapshot, state)
        recorder.record(snapshot, state, desired)
        state.last_control_monotonic = 999.0
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
        snapshot = _snapshot(window_open=True)
        desired, _ = decide(snapshot, running_kernel_state())
        recorder.record(snapshot, running_kernel_state(), desired)
        entry = json.loads(json.dumps(recorder.export()))[0]

        assert snapshot_from_dict(entry["snapshot"]) == snapshot
        rebuilt = state_from_dict(entry["state"])
        assert rebuilt == running_kernel_state()
