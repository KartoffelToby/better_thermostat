"""Golden tests pinning decide() through the flight-recorder replay path.

Each scenario builds a (snapshot, kernel-state) pair, runs it through
the kernel, and compares the recorder export against a committed golden
file in ``tests/fixtures/replay_corpus/``. This pins three things at
once: the decision itself, the recorder's serialization, and the
replay reconstruction.

To regenerate the goldens after an intentional kernel change, run
pytest with ``BT_REGEN_GOLDENS=1`` and review the diff.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from custom_components.better_thermostat.core.decide import decide
from custom_components.better_thermostat.core.fsm.control_mode import (
    ControlMode,
    ControlModeState,
)
from custom_components.better_thermostat.core.fsm.lifecycle import LifecycleState
from custom_components.better_thermostat.core.fsm.maintenance import (
    MaintenancePhase,
    MaintenanceState,
)
from custom_components.better_thermostat.core.fsm.mode import ModeState
from custom_components.better_thermostat.core.fsm.reachability import ReachabilityState
from custom_components.better_thermostat.core.fsm.window import WindowPhase, WindowState
from custom_components.better_thermostat.core.recorder import FlightRecorder, replay
from custom_components.better_thermostat.core.snapshot import HvacMode, TrvReported
from tests.factories import make_snapshot, make_state

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "replay_corpus"


def _scenarios() -> dict[str, tuple]:
    """One (snapshot, state) pair per kernel decision tier."""
    return {
        "heating_cold_room": (make_snapshot(room_temp=18.0), make_state()),
        "no_call_for_heat": (make_snapshot(call_for_heat=False), make_state()),
        "off_mode": (
            make_snapshot(hvac_mode=HvacMode.OFF),
            make_state(mode=ModeState(hvac_mode=HvacMode.OFF)),
        ),
        "window_open": (
            make_snapshot(),
            make_state(window=WindowState(phase=WindowPhase.OPEN)),
        ),
        "startup_running": (make_snapshot(), make_state(lifecycle=LifecycleState())),
        "maintenance_running": (
            make_snapshot(),
            make_state(
                maintenance=MaintenanceState(
                    phase=MaintenancePhase.RUNNING, running_since=900.0
                )
            ),
        ),
        "unreachable_trv": (
            make_snapshot(
                trvs={
                    "climate.trv1": TrvReported(
                        entity_id="climate.trv1", available=False
                    ),
                    "climate.trv2": TrvReported(entity_id="climate.trv2"),
                }
            ),
            make_state(
                reachability={
                    "climate.trv1": ReachabilityState(
                        online=False, offline_since=800.0, retry_at=1100.0
                    )
                }
            ),
        ),
        "hold_rung": (
            make_snapshot(room_temp=None),
            make_state(
                control_mode=ControlModeState(
                    mode=ControlMode.HOLD, degraded_since=700.0
                )
            ),
        ),
        "sensor_fallback_rung": (
            make_snapshot(),
            make_state(
                control_mode=ControlModeState(
                    mode=ControlMode.SENSOR_FALLBACK, degraded_since=700.0
                )
            ),
        ),
    }


def _export_entry(name: str) -> dict:
    snapshot, state = _scenarios()[name]
    recorder = FlightRecorder(capacity=1)
    desired, _ = decide(snapshot, state)
    recorder.record(snapshot, state, desired)
    return recorder.export()[0]


@pytest.mark.parametrize("name", sorted(_scenarios()))
def test_decision_matches_the_golden_file(name):
    """The exported decision tuple is byte-stable against the golden."""
    entry = _export_entry(name)
    golden_path = GOLDEN_DIR / f"{name}.json"

    if os.environ.get("BT_REGEN_GOLDENS") == "1":
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(json.dumps(entry, indent=2, sort_keys=True) + "\n")

    golden = json.loads(golden_path.read_text())
    assert entry == golden


@pytest.mark.parametrize("name", sorted(_scenarios()))
def test_golden_replays_through_the_kernel(name):
    """replay() reproduces every recorded decision from the corpus."""
    golden = json.loads((GOLDEN_DIR / f"{name}.json").read_text())
    matches, recomputed = replay(golden)
    assert matches, f"kernel diverged from recorded decision: {recomputed}"
