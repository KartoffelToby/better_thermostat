"""Integration tests for re-identification: adopt path, persistence, dispatch."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import json
from types import SimpleNamespace
from typing import Any, override
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.climate.const import HVACMode
from homeassistant.core import State
import pytest

from custom_components.better_thermostat.calibration import _compute_mpc_v2_balance
from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.fsm.control_mode import ControlMode
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.calibration.mpc_v2 import (
    MpcV2Input,
    MpcV2Params,
    PlantParams,
    ReidConfig,
    ReidSample,
    compute_mpc_v2,
    export_mpc_v2_state,
    import_mpc_v2_state,
)
from custom_components.better_thermostat.utils.calibration.mpc_v2.state import (
    _plant_signature_of,
)
from custom_components.better_thermostat.utils.calibration.plant_preset import (
    DEFAULT_FLOW_TEMPERATURE,
)
from custom_components.better_thermostat.utils.const import (
    CalibrationMode,
    CalibrationOutput,
    MpcV2PlantPreset,
)
from custom_components.better_thermostat.utils.entry_schema import StoredChoice
from custom_components.better_thermostat.utils.state_manager import (
    MpcV2ReidData,
    RuntimeState,
    StateManager,
    _deserialize,
    _serialize,
    deserialize_mpc_v2_reid,
)

_REID = MpcV2ReidData(
    tau_room_minutes=240.0,
    gain_heater=3.0,
    fitted_ts=1000.0,
    rmse_prior_kelvin=0.4,
    rmse_fit_kelvin=0.1,
    n_segments=4,
)


def _make_manager() -> StateManager:
    """Build a StateManager with a mocked HA Store."""
    mock_hass = AsyncMock()
    with patch("custom_components.better_thermostat.utils.state_manager.Store"):
        return StateManager(mock_hass, "test_entry")


def _warm(mgr: StateManager, key: str, params: MpcV2Params) -> None:
    """Run one compute cycle so the manager holds a live controller."""
    state = mgr.get_mpc_v2_live(key, params)
    _out, state = compute_mpc_v2(
        MpcV2Input(
            key=key,
            target_temperature=22.0,
            room_temperature=19.0,
            outdoor_temperature=5.0,
            heating_allowed=True,
            window_open=False,
        ),
        params,
        state=state,
        now=0.0,
    )
    mgr.set_mpc_v2_live(key, state)


# -- StateManager: adopt + persistence ---------------------------------------


def test_adopt_is_bumpless() -> None:
    """Adoption drops the live controller but carries its state across.

    The next live access must rebuild the controller with the new prior
    while restoring the previous command from the folded snapshot — the
    definition of a bumpless transfer.
    """
    mgr = _make_manager()
    _warm(mgr, "k", MpcV2Params())
    old = mgr.get_mpc_v2_live("k", MpcV2Params())
    assert old.controller is not None
    last_u_before = old.controller._last_u

    mgr.adopt_mpc_v2_reid("k", _REID)

    new_params = MpcV2Params()
    new_params.plant.tau_room_min = _REID.tau_room_minutes
    new_params.plant.gain_heater = _REID.gain_heater
    rebuilt = mgr.get_mpc_v2_live("k", new_params)
    assert rebuilt is not old
    assert rebuilt.controller is not None
    assert rebuilt.controller is not old.controller
    assert rebuilt.controller._last_u == last_u_before
    assert rebuilt.controller.plant_fine.params.tau_room_min == _REID.tau_room_minutes


def test_adopt_marks_dirty_and_result_readable() -> None:
    """The adopted result is stored, retrievable, and flagged for saving."""
    mgr = _make_manager()
    mgr.adopt_mpc_v2_reid("k", _REID)
    assert mgr.dirty is True
    stored = mgr.get_mpc_v2_reid("k")
    assert stored is not None
    assert stored.tau_room_minutes == 240.0


def test_reid_result_survives_serialization_round_trip() -> None:
    """serialize -> deserialize reproduces the persisted re-ID result."""
    mgr = _make_manager()
    mgr.adopt_mpc_v2_reid("k", _REID)
    raw = _serialize(mgr.state)
    restored = _deserialize(raw)
    assert restored.mpc_v2_reid["k"].tau_room_minutes == 240.0
    assert restored.mpc_v2_reid["k"].gain_heater == 3.0
    assert restored.mpc_v2_reid["k"].n_segments == 4


def test_deserialize_rejects_malformed_reid_payload() -> None:
    """Zero/garbage fitted components cannot seed a plant prior."""
    assert deserialize_mpc_v2_reid({"tau_room_min": 0.0, "gain_heater": 2.0}) is None
    assert deserialize_mpc_v2_reid({"tau_room_min": "junk"}) is None
    ok = deserialize_mpc_v2_reid({"tau_room_min": 300.0, "gain_heater": 2.5})
    assert ok is not None and ok.tau_room_minutes == 300.0


# -- Dispatcher wiring --------------------------------------------------------


def _make_trv(entity_id: str, preset: StoredChoice) -> Trv:
    """Build a Trv configured for MPC v2 calibration with a given preset."""
    return Trv(
        entity_id=entity_id,
        current_temperature=19.0,
        valve_max_opening=100.0,
        advanced={
            "calibration": CalibrationOutput.DIRECT_VALVE_BASED,
            "calibration_mode": CalibrationMode.MPC_V2_CALIBRATION,
            "mpc_v2_plant_preset": preset,
        },
        valve_position_writable=True,
        valve_position_entity="number.trv_valve",
        max_temp=30.0,
        model_quirks=None,
    )


def _make_bt(preset: StoredChoice = MpcV2PlantPreset.AUTO) -> Any:
    """Build a minimal BT-shaped namespace with a real StateManager."""
    return SimpleNamespace(
        real_trvs={"climate.x": _make_trv("climate.x", preset)},
        heat_target_temperature=21.0,
        room_temperature=19.5,
        learning_held=False,
        tolerance=0.0,
        window_open=False,
        contact_open=False,
        contact_sensed_open=False,
        kernel_state=SimpleNamespace(
            control_mode=SimpleNamespace(mode=ControlMode.OPTIMAL)
        ),
        device_name="BT_TEST",
        bt_hvac_mode=HVACMode.HEAT,
        heating_power=0.04,
        heat_loss_rate=0.02,
        outdoor_sensor_entity_id=None,
        weather_entity_id=None,
        hass=None,
        unique_id=None,
        state_mgr=_make_manager(),
        clock=FakeClock(monotonic_value=1_000_000.0),
        schedule_save_state=MagicMock(),
        is_removed=False,
    )


def test_auto_prior_uses_adopted_reid_result() -> None:
    """Under AUTO, an adopted re-ID result overrides the heat-loss heuristic."""
    bt = _make_bt()
    out, _ = _compute_mpc_v2_balance(bt, "climate.x")
    assert out is not None
    key = next(iter(bt.state_mgr._mpc_v2_live))
    bt.state_mgr.adopt_mpc_v2_reid(key, _REID)

    out, _ = _compute_mpc_v2_balance(bt, "climate.x")
    assert out is not None
    live = bt.state_mgr.get_mpc_v2_live(key, MpcV2Params())
    assert live.controller is not None
    assert live.controller.plant_fine.params.tau_room_min == _REID.tau_room_minutes
    assert live.controller.plant_fine.params.gain_heater == _REID.gain_heater
    debug = bt.real_trvs["climate.x"].calibration_balance["debug"]
    assert debug["reid_tau_room"] == _REID.tau_room_minutes


def test_out_of_band_stored_prior_never_reaches_the_controller() -> None:
    """A store entry outside the band is dropped, so AUTO keeps its heuristic.

    Restored through the real ``_deserialize`` on a JSON round trip and read
    back by the dispatcher: the controller must run on the heat-loss-derived
    prior (15.0 K / 0.02 K per min = 750 min) and the valve must not sit on a
    rail, which is what a ``tau_room_min`` of 5e-324 commands.
    """
    mgr = _make_manager()
    raw = _serialize(RuntimeState())
    raw["mpc_v2_reid"] = {
        "bt:reid": {
            "tau_room_min": 5e-324,
            "gain_heater": 3.0,
            "fitted_ts": 1000.0,
            "n_segments": 3,
            "rmse_prior_K": 0.5,
            "rmse_fit_K": 0.3,
            "flow_temperature": 65.0,
        }
    }
    mgr._state = _deserialize(json.loads(json.dumps(raw)))
    assert "bt:reid" not in mgr.state.mpc_v2_reid

    bt = _make_bt()
    bt.state_mgr = mgr
    out, _ = _compute_mpc_v2_balance(bt, "climate.x")
    assert out is not None
    live = next(iter(mgr._mpc_v2_live.values()))
    assert live.controller is not None
    assert live.controller.plant_fine.params.tau_room_min == 750.0
    assert 0.0 < bt.real_trvs["climate.x"].calibration_balance["valve_percent"] < 100.0


def test_explicit_preset_beats_reid_result() -> None:
    """A user-chosen preset is an opt-out: the re-ID result is ignored."""
    bt = _make_bt(preset=MpcV2PlantPreset.SMALL_ROOM)
    out, _ = _compute_mpc_v2_balance(bt, "climate.x")
    assert out is not None
    key = next(iter(bt.state_mgr._mpc_v2_live))
    bt.state_mgr.adopt_mpc_v2_reid(key, _REID)

    out, _ = _compute_mpc_v2_balance(bt, "climate.x")
    assert out is not None
    live = next(iter(bt.state_mgr._mpc_v2_live.values()))
    assert live.controller is not None
    assert live.controller.plant_fine.params.tau_room_min == 180.0  # small_room


def test_dispatch_records_reid_samples_under_auto() -> None:
    """AUTO re-ID records the previously confirmed valve input."""
    bt = _make_bt()
    bt.real_trvs["climate.x"].last_valve_percent = 37
    out, _ = _compute_mpc_v2_balance(bt, "climate.x")
    assert out is not None
    key = next(iter(bt.state_mgr._mpc_v2_reid_live))
    runtime = bt.state_mgr.get_mpc_v2_reid_runtime(key)
    assert len(runtime.buffer.samples) == 1
    sample = runtime.buffer.samples[0]
    assert sample.T_room == 19.5
    assert sample.u_frac == 0.37
    assert sample.window_open is False


def test_dispatch_does_not_identify_from_an_unconfirmed_recommendation() -> None:
    """The first command cannot enter re-ID before a write is confirmed."""
    bt = _make_bt()
    out, _ = _compute_mpc_v2_balance(bt, "climate.x")
    assert out is not None
    assert bt.state_mgr._mpc_v2_reid_live == {}


def test_dispatch_skips_sampling_when_control_mode_degraded() -> None:
    """Samples are recorded only on the OPTIMAL rung of the fail-soft ladder.

    Under SENSOR_FALLBACK ``room_temperature`` freezes at the last valid reading
    while the valve keeps moving; recording such samples would fit the
    plant against a frozen temperature tail. HOLD has no usable reading
    at all. Both rungs must leave the buffer untouched.
    """
    bt = _make_bt()
    # The TRV stays reachable and reports its temperature, so SENSOR_FALLBACK
    # reads its internal temperature.
    bt.hass = SimpleNamespace(
        states=SimpleNamespace(
            get=lambda entity_id: (
                State(entity_id, "heat", {"current_temperature": 19.0})
                if entity_id == "climate.x"
                else None
            )
        ),
        config=SimpleNamespace(units=SimpleNamespace(temperature_unit="°C")),
    )
    bt.real_trvs["climate.x"].last_valve_percent = 37
    out, _ = _compute_mpc_v2_balance(bt, "climate.x")
    assert out is not None
    key = next(iter(bt.state_mgr._mpc_v2_reid_live))
    runtime = bt.state_mgr.get_mpc_v2_reid_runtime(key)
    assert len(runtime.buffer.samples) == 1

    for degraded_mode in (ControlMode.SENSOR_FALLBACK, ControlMode.HOLD):
        bt.kernel_state.control_mode = SimpleNamespace(mode=degraded_mode)
        bt.clock.advance(120.0)
        out, _ = _compute_mpc_v2_balance(bt, "climate.x")
        assert out is not None
        assert len(runtime.buffer.samples) == 1

    bt.kernel_state.control_mode = SimpleNamespace(mode=ControlMode.OPTIMAL)
    bt.clock.advance(120.0)
    out, _ = _compute_mpc_v2_balance(bt, "climate.x")
    assert out is not None
    assert len(runtime.buffer.samples) == 2


def test_fallback_episode_gap_splits_reid_segments() -> None:
    """A sampling pause longer than ``max_gap_s`` cuts the segment there.

    Skipping samples during a degraded episode leaves a time gap in the
    buffer; the segmenter breaks runs on gaps above ``ReidConfig.max_gap_s``,
    so the frozen episode can never bridge two transients into one.
    """
    from custom_components.better_thermostat.utils.calibration.mpc_v2.reid import (
        extract_segments,
    )

    config = ReidConfig()
    spacing = 300.0
    samples: list[ReidSample] = []
    # First heat-up run: 10 samples over 2700 s, rising 1.8 K.
    for i in range(10):
        samples.append(
            ReidSample(
                t_s=i * spacing, T_room=19.0 + 0.2 * i, u_frac=0.8, T_outdoor=5.0
            )
        )
    # Degraded episode: no samples for longer than the gap threshold.
    gap_start = samples[-1].t_s
    resume = gap_start + config.max_gap_s + spacing
    # Second heat-up run after recovery.
    for i in range(10):
        samples.append(
            ReidSample(
                t_s=resume + i * spacing,
                T_room=20.0 + 0.2 * i,
                u_frac=0.8,
                T_outdoor=5.0,
            )
        )

    segments = extract_segments(samples, config)
    assert len(segments) == 2
    assert all(s.kind == "heatup" for s in segments)
    # Without the gap the same samples form one contiguous run.
    contiguous = [
        ReidSample(t_s=i * spacing, T_room=19.0 + 0.1 * i, u_frac=0.8, T_outdoor=5.0)
        for i in range(20)
    ]
    assert len(extract_segments(contiguous, config)) == 1


def test_dispatch_skips_sampling_for_explicit_preset() -> None:
    """Preset mode is a re-ID opt-out: no samples are collected."""
    bt = _make_bt(preset=MpcV2PlantPreset.LARGE_ROOM)
    out, _ = _compute_mpc_v2_balance(bt, "climate.x")
    assert out is not None
    assert bt.state_mgr._mpc_v2_reid_live == {}


# -- Fit scheduling -----------------------------------------------------------


class _FakeFuture:
    """Future double that resolves synchronously in add_done_callback."""

    def __init__(self, result: object) -> None:
        self._result = result

    def result(self) -> object:
        """Return the wrapped result."""
        return self._result

    def add_done_callback(self, cb) -> None:
        """Invoke the callback immediately with this future."""
        cb(self)


# The configuration a hass double carries: a metric system.
_METRIC_CONFIG = SimpleNamespace(units=SimpleNamespace(temperature_unit="°C"))


class _FakeHass:
    """hass double whose executor runs the job inline."""

    config = _METRIC_CONFIG

    def async_add_executor_job(self, func, *args: object) -> _FakeFuture:
        """Run ``func`` synchronously and hand back a resolved future."""
        return _FakeFuture(func(*args))


def test_fit_scheduling_adopts_accepted_outcome(monkeypatch) -> None:
    """A due fit runs via the executor and adopts an accepted result once."""
    from custom_components.better_thermostat import calibration as cal
    from custom_components.better_thermostat.utils.calibration.mpc_v2 import (
        ReidOutcome,
        ReidSample,
    )

    bt = _make_bt()
    bt.hass = _FakeHass()
    bt.real_trvs["climate.x"].last_valve_percent = 50
    out, _ = _compute_mpc_v2_balance(bt, "climate.x")
    assert out is not None
    key = next(iter(bt.state_mgr._mpc_v2_reid_live))
    runtime = bt.state_mgr.get_mpc_v2_reid_runtime(key)
    # Replace the dispatch's wall-clock sample with a dense synthetic history;
    # mixed time bases would trip the buffer's spacing dedupe.
    runtime.buffer.samples.clear()
    for i in range(300):
        runtime.buffer.append(ReidSample(t_s=float(i * 300), T_room=20.0, u_frac=0.5))
    runtime.last_fit_attempt_ts = 0.0

    calls: list[int] = []

    def _fake_fit(samples, prior):
        calls.append(len(samples))
        return ReidOutcome(
            status="accepted",
            tau_room_min=_REID.tau_room_minutes,
            gain_heater=_REID.gain_heater,
            rmse_prior_K=0.4,
            rmse_fit_K=0.1,
            n_segments=4,
            n_samples=300,
        )

    monkeypatch.setattr(cal, "run_reid_fit", _fake_fit)

    cal._maybe_start_mpc_v2_reid_fit(bt, key, MpcV2Params())
    assert calls == [len(runtime.buffer.samples)]
    adopted = bt.state_mgr.get_mpc_v2_reid(key)
    assert adopted is not None
    assert adopted.tau_room_minutes == _REID.tau_room_minutes
    assert runtime.fit_inflight is False
    bt.schedule_save_state.assert_called()

    # A second immediate call is throttled by the attempt interval.
    cal._maybe_start_mpc_v2_reid_fit(bt, key, MpcV2Params())
    assert len(calls) == 1


def test_accepted_fit_is_stamped_on_the_wall_clock(monkeypatch) -> None:
    """An accepted fit records when it happened on the wall clock.

    The stamp is persisted and outlives a host reboot, which starts the
    monotonic clock over; the wall clock continues across it.
    """
    from custom_components.better_thermostat import calibration as cal
    from custom_components.better_thermostat.utils.calibration.mpc_v2 import (
        ReidOutcome,
        ReidSample,
    )

    bt = _make_bt()
    bt.hass = _FakeHass()
    bt.real_trvs["climate.x"].last_valve_percent = 50
    _compute_mpc_v2_balance(bt, "climate.x")
    key = next(iter(bt.state_mgr._mpc_v2_reid_live))
    runtime = bt.state_mgr.get_mpc_v2_reid_runtime(key)
    runtime.buffer.samples.clear()
    for i in range(300):
        runtime.buffer.append(ReidSample(t_s=float(i * 300), T_room=20.0, u_frac=0.5))
    runtime.last_fit_attempt_ts = 0.0
    monkeypatch.setattr(
        cal,
        "run_reid_fit",
        lambda samples, prior: ReidOutcome(
            status="accepted",
            tau_room_min=_REID.tau_room_minutes,
            gain_heater=_REID.gain_heater,
            rmse_prior_K=0.4,
            rmse_fit_K=0.1,
            n_segments=4,
            n_samples=300,
        ),
    )

    cal._maybe_start_mpc_v2_reid_fit(bt, key, MpcV2Params())

    adopted = bt.state_mgr.get_mpc_v2_reid(key)
    assert adopted is not None
    assert adopted.fitted_ts == bt.clock.now().timestamp()
    assert adopted.fitted_ts != bt.clock.monotonic()


# -- Target-independent re-ID keying ------------------------------------------


def test_reid_buffer_is_shared_across_target_buckets() -> None:
    """A setpoint move past a bucket boundary keeps feeding one buffer."""
    bt = _make_bt()
    bt.real_trvs["climate.x"].last_valve_percent = 37
    out, _ = _compute_mpc_v2_balance(bt, "climate.x")
    assert out is not None

    bt.clock.advance(120.0)
    bt.heat_target_temperature = 22.5  # different half-degree bucket
    out, _ = _compute_mpc_v2_balance(bt, "climate.x")
    assert out is not None

    reid_keys = list(bt.state_mgr._mpc_v2_reid_live)
    assert len(reid_keys) == 1
    runtime = bt.state_mgr.get_mpc_v2_reid_runtime(reid_keys[0])
    assert len(runtime.buffer.samples) == 2


def test_adopt_transfers_all_live_controllers_bumplessly() -> None:
    """Adoption folds and drops every cached live controller, not just one.

    The plant prior describes the room, so a controller cached under any
    target bucket is stale after adoption. Each one must take the
    bumpless rebuild path on its next activation: observer state carried
    over from the folded snapshot and the new prior's signature stamped,
    so ``compute_mpc_v2`` does not force a cold rebuild on the first
    cycle back in that bucket.
    """
    from custom_components.better_thermostat.utils.calibration.mpc_v2.state import (
        _plant_signature_of,
    )

    mgr = _make_manager()
    key_a = "uid:climate.x:t21.0"
    key_b = "uid:climate.x:t22.5"
    _warm(mgr, key_a, MpcV2Params())
    _warm(mgr, key_b, MpcV2Params())
    old_a = mgr.get_mpc_v2_live(key_a, MpcV2Params())
    old_b = mgr.get_mpc_v2_live(key_b, MpcV2Params())
    assert old_a.controller is not None and old_b.controller is not None
    last_u_a = old_a.controller._last_u
    last_u_b = old_b.controller._last_u

    mgr.adopt_mpc_v2_reid("uid:reid", _REID)

    assert mgr.get_mpc_v2_reid("uid:reid") is _REID
    assert mgr.get_mpc_v2_reid(key_a) is None

    new_params = MpcV2Params()
    new_params.plant.tau_room_min = _REID.tau_room_minutes
    new_params.plant.gain_heater = _REID.gain_heater
    for key, old, last_u in ((key_a, old_a, last_u_a), (key_b, old_b, last_u_b)):
        rebuilt = mgr.get_mpc_v2_live(key, new_params)
        assert rebuilt is not old
        assert rebuilt.controller is not None
        assert rebuilt.controller is not old.controller
        assert rebuilt.controller._last_u == last_u
        # The new prior's signature is stamped on the rebuilt state, so the
        # signature guard in compute_mpc_v2 keeps this controller (bumpless)
        # instead of discarding it for a cold rebuild.
        assert rebuilt.plant_signature == _plant_signature_of(new_params)


def test_adopt_under_shared_key_removes_legacy_bucket_entries() -> None:
    """Writing the shared key clears this uid's per-bucket result entries.

    The shared key wins every read, so bucket entries are dead weight;
    left in place they could only resurrect stale data through the
    legacy fallback if the shared entry ever disappeared. Entries of
    other uids are untouched.
    """
    mgr = _make_manager()
    legacy = MpcV2ReidData(
        tau_room_minutes=600.0, gain_heater=1.0, fitted_ts=100.0, n_segments=3
    )
    mgr.adopt_mpc_v2_reid("uid:climate.x:t21.0", legacy)
    mgr.adopt_mpc_v2_reid("uid:group:t19.0", legacy)
    mgr.adopt_mpc_v2_reid("other:climate.y:t20.0", legacy)

    mgr.adopt_mpc_v2_reid("uid:reid", _REID)

    assert mgr.get_mpc_v2_reid("uid:reid") is _REID
    assert mgr.get_mpc_v2_reid("uid:climate.x:t21.0") is None
    assert mgr.get_mpc_v2_reid("uid:group:t19.0") is None
    assert mgr.get_mpc_v2_reid("other:climate.y:t20.0") is legacy


def test_shared_reid_key_survives_serialization_round_trip() -> None:
    """The target-independent key persists and reloads like any other."""
    mgr = _make_manager()
    mgr.adopt_mpc_v2_reid("uid:reid", _REID)
    restored = _deserialize(_serialize(mgr.state))
    assert restored.mpc_v2_reid["uid:reid"].tau_room_minutes == 240.0


def test_prior_lookup_falls_back_to_legacy_bucket_keys() -> None:
    """A result persisted under a per-bucket key still seeds the prior."""
    bt = _make_bt()
    # Legacy layout: result stored under the entity's target-bucket key.
    bt.state_mgr.adopt_mpc_v2_reid("bt:climate.x:t21.0", _REID)

    out, _ = _compute_mpc_v2_balance(bt, "climate.x")
    assert out is not None
    live = next(iter(bt.state_mgr._mpc_v2_live.values()))
    assert live.controller is not None
    assert live.controller.plant_fine.params.tau_room_min == _REID.tau_room_minutes


def test_prior_lookup_prefers_shared_key_over_bucket_entries() -> None:
    """When both layouts exist, the shared-key result wins."""
    from custom_components.better_thermostat.calibration import _lookup_mpc_v2_reid

    bt = _make_bt()
    legacy = MpcV2ReidData(
        tau_room_minutes=600.0, gain_heater=1.0, fitted_ts=2_000_000.0, n_segments=3
    )
    bt.state_mgr.adopt_mpc_v2_reid("bt:climate.x:t21.0", legacy)
    bt.state_mgr.adopt_mpc_v2_reid("bt:reid", _REID)

    found = _lookup_mpc_v2_reid(
        bt, "bt:reid", "bt:climate.x:t21.0", DEFAULT_FLOW_TEMPERATURE
    )
    assert found is _REID


def test_prior_lookup_picks_freshest_legacy_bucket_entry() -> None:
    """Among legacy bucket entries, the most recently fitted one wins."""
    from custom_components.better_thermostat.calibration import _lookup_mpc_v2_reid

    bt = _make_bt()
    stale = MpcV2ReidData(
        tau_room_minutes=600.0, gain_heater=1.0, fitted_ts=100.0, n_segments=3
    )
    fresh = MpcV2ReidData(
        tau_room_minutes=300.0, gain_heater=2.5, fitted_ts=200.0, n_segments=3
    )
    bt.state_mgr.adopt_mpc_v2_reid("bt:climate.x:t19.0", stale)
    bt.state_mgr.adopt_mpc_v2_reid("bt:climate.x:t21.0", fresh)

    found = _lookup_mpc_v2_reid(
        bt, "bt:reid", "bt:climate.x:t22.0", DEFAULT_FLOW_TEMPERATURE
    )
    assert found is fresh


# -- Preset and compute failures ----------------------------------------------


def test_dispatch_reads_an_unknown_stored_preset_as_auto() -> None:
    """A preset this version does not know runs as AUTO and keeps sampling."""
    bt = _make_bt(preset="retired_preset")
    bt.real_trvs["climate.x"].last_valve_percent = 37

    out, _ = _compute_mpc_v2_balance(bt, "climate.x")

    assert out is not None
    key = next(iter(bt.state_mgr._mpc_v2_reid_live))
    assert len(bt.state_mgr.get_mpc_v2_reid_runtime(key).buffer.samples) == 1


def test_a_failed_compute_publishes_no_valve() -> None:
    bt = _make_bt()

    with patch(
        "custom_components.better_thermostat.calibration.compute_mpc_v2",
        side_effect=ValueError("boom"),
    ):
        out, supports_valve = _compute_mpc_v2_balance(bt, "climate.x")

    assert out is None
    assert supports_valve is False
    assert bt.real_trvs["climate.x"].calibration_balance is None


# -- Fit scheduling failures --------------------------------------------------


def _due_fit(bt: Any) -> tuple[str, Any]:
    """Dispatch once and fill the buffer so a fit is due; return key and runtime."""
    bt.real_trvs["climate.x"].last_valve_percent = 50
    _compute_mpc_v2_balance(bt, "climate.x")
    key = next(iter(bt.state_mgr._mpc_v2_reid_live))
    runtime = bt.state_mgr.get_mpc_v2_reid_runtime(key)
    runtime.buffer.samples.clear()
    for i in range(300):
        runtime.buffer.append(ReidSample(t_s=float(i * 300), T_room=20.0, u_frac=0.5))
    runtime.last_fit_attempt_ts = 0.0
    return key, runtime


class _RaisingFuture(_FakeFuture):
    """Future double whose job raised."""

    @override
    def result(self) -> object:
        """Raise what the job raised."""
        raise RuntimeError("fit crashed")


def test_a_fit_already_running_starts_no_second_one(monkeypatch) -> None:
    from custom_components.better_thermostat import calibration as cal

    bt = _make_bt()
    bt.hass = _FakeHass()
    key, runtime = _due_fit(bt)
    runtime.fit_inflight = True
    started: list[int] = []
    monkeypatch.setattr(cal, "run_reid_fit", lambda samples, prior: started.append(1))

    cal._maybe_start_mpc_v2_reid_fit(bt, key, MpcV2Params())

    assert started == []
    assert runtime.fit_inflight is True


def test_a_rejected_fit_leaves_the_prior_alone(monkeypatch) -> None:
    from custom_components.better_thermostat import calibration as cal
    from custom_components.better_thermostat.utils.calibration.mpc_v2 import ReidOutcome

    bt = _make_bt()
    bt.hass = _FakeHass()
    key, runtime = _due_fit(bt)
    monkeypatch.setattr(
        cal,
        "run_reid_fit",
        lambda samples, prior: ReidOutcome(
            status="rejected", n_segments=2, n_samples=300
        ),
    )

    cal._maybe_start_mpc_v2_reid_fit(bt, key, MpcV2Params())

    assert bt.state_mgr.get_mpc_v2_reid(key) is None
    assert runtime.fit_inflight is False


class _PendingFuture(_FakeFuture):
    """Future double that resolves only when the test says so."""

    def __init__(self, result: object) -> None:
        super().__init__(result)
        self._callbacks: list[Callable[[_FakeFuture], object]] = []

    @override
    def add_done_callback(self, cb) -> None:
        """Hold the callback until :meth:`resolve`."""
        self._callbacks.append(cb)

    def resolve(self) -> None:
        """Run the held callbacks, as the loop does once the job returns."""
        for cb in self._callbacks:
            cb(self)


def test_a_fit_finishing_after_the_removal_is_not_adopted(monkeypatch) -> None:
    """A thermostat removed while its fit runs adopts and saves nothing.

    The fit runs in the executor, where the removal cannot stop it. Its
    result belongs to a thermostat whose final save is already written.
    """
    from custom_components.better_thermostat import calibration as cal
    from custom_components.better_thermostat.utils.calibration.mpc_v2 import ReidOutcome

    outcome = ReidOutcome(
        status="accepted",
        tau_room_min=_REID.tau_room_minutes,
        gain_heater=_REID.gain_heater,
        rmse_prior_K=0.4,
        rmse_fit_K=0.1,
        n_segments=4,
        n_samples=300,
    )
    pending = _PendingFuture(outcome)
    bt = _make_bt()
    bt.hass = SimpleNamespace(
        async_add_executor_job=lambda func, *args: pending, config=_METRIC_CONFIG
    )
    key, runtime = _due_fit(bt)
    bt.schedule_save_state.reset_mock()

    cal._maybe_start_mpc_v2_reid_fit(bt, key, MpcV2Params())
    bt.is_removed = True
    pending.resolve()

    assert bt.state_mgr.get_mpc_v2_reid(key) is None
    bt.schedule_save_state.assert_not_called()
    assert runtime.fit_inflight is False


def test_a_fit_that_crashes_frees_the_next_attempt(caplog) -> None:
    """A crashed fit is reported and does not block every later one."""
    from custom_components.better_thermostat import calibration as cal

    bt = _make_bt()
    bt.hass = SimpleNamespace(
        async_add_executor_job=lambda func, *args: _RaisingFuture(None),
        config=_METRIC_CONFIG,
    )
    key, runtime = _due_fit(bt)

    cal._maybe_start_mpc_v2_reid_fit(bt, key, MpcV2Params())

    assert runtime.fit_inflight is False
    assert bt.state_mgr.get_mpc_v2_reid(key) is None
    assert "re-identification failed" in caplog.text


def test_a_fit_that_cannot_be_scheduled_frees_the_next_attempt(caplog) -> None:
    from custom_components.better_thermostat import calibration as cal

    def _refuse(func, *args):
        raise RuntimeError("executor shut down")

    bt = _make_bt()
    bt.hass = SimpleNamespace(async_add_executor_job=_refuse, config=_METRIC_CONFIG)
    key, runtime = _due_fit(bt)

    cal._maybe_start_mpc_v2_reid_fit(bt, key, MpcV2Params())

    assert runtime.fit_inflight is False
    assert "could not schedule MPC v2 re-identification" in caplog.text


# -- Missing state store ------------------------------------------------------


def test_reid_helpers_without_a_state_store_do_nothing() -> None:
    """Every re-ID helper is a no-op until the state store exists."""
    from custom_components.better_thermostat import calibration as cal

    bt = _make_bt()
    bt.hass = _FakeHass()
    bt.state_mgr = None

    assert (
        cal._lookup_mpc_v2_reid(
            bt, "bt:reid", "bt:climate.x:t21.0", DEFAULT_FLOW_TEMPERATURE
        )
        is None
    )
    cal._record_mpc_v2_reid_sample(
        bt,
        "bt:reid",
        applied_valve_percent=40.0,
        trv_temperature=21.0,
        outdoor_temperature=5.0,
    )
    cal._maybe_start_mpc_v2_reid_fit(bt, "bt:reid", MpcV2Params())


# -- Configured flow temperature ----------------------------------------------


def _hass_in(unit: str) -> SimpleNamespace:
    """Return a hass double whose system unit is ``unit``."""
    return SimpleNamespace(
        config=SimpleNamespace(units=SimpleNamespace(temperature_unit=unit)),
        async_add_executor_job=lambda func, *args: _FakeFuture(func(*args)),
    )


def _live_controller(bt: Any) -> Any:
    """Return the one live MPC v2 controller the dispatch built."""
    (live,) = bt.state_mgr._mpc_v2_live.values()
    assert live.controller is not None
    return live.controller


@pytest.mark.parametrize(
    ("stored", "unit", "flow_temperature"),
    [
        (40.0, "°C", 40.0),
        (104.0, "°F", 40.0),
        ("45", "°C", 45.0),
        (149.0, "°C", DEFAULT_FLOW_TEMPERATURE),
        (20.0, "°C", DEFAULT_FLOW_TEMPERATURE),
        ("warm", "°C", DEFAULT_FLOW_TEMPERATURE),
        (True, "°C", DEFAULT_FLOW_TEMPERATURE),
        (None, "°C", DEFAULT_FLOW_TEMPERATURE),
    ],
    ids=[
        "celsius",
        "fahrenheit",
        "text",
        "above_the_range",
        "below_the_range",
        "not_a_number",
        "flag",
        "null",
    ],
)
def test_the_controller_models_the_configured_flow_temperature(
    stored: object, unit: str, flow_temperature: float
) -> None:
    """The flow temperature the TRV stores, in °C, is the plant's supply water.

    It is stored in the system unit; a value the flows could not have
    written leaves the room on the default.
    """
    bt = _make_bt()
    bt.hass = _hass_in(unit)
    bt.real_trvs["climate.x"].advanced["mpc_v2_flow_temperature"] = stored

    out, _ = _compute_mpc_v2_balance(bt, "climate.x")

    assert out is not None
    controller = _live_controller(bt)
    assert controller.plant_coarse.params.T_water == pytest.approx(flow_temperature)
    assert controller.plant_fine.params.T_water == pytest.approx(flow_temperature)


def test_a_thermostat_without_a_flow_temperature_models_the_default() -> None:
    """An entry written before the setting existed runs on 65 °C supply water."""
    bt = _make_bt()
    assert "mpc_v2_flow_temperature" not in bt.real_trvs["climate.x"].advanced

    out, _ = _compute_mpc_v2_balance(bt, "climate.x")

    assert out is not None
    assert _live_controller(bt).plant_coarse.params.T_water == 65.0


@pytest.mark.parametrize("preset", list(MpcV2PlantPreset))
def test_every_plant_preset_models_the_configured_flow_temperature(
    preset: MpcV2PlantPreset,
) -> None:
    """The flow temperature applies whichever room model the preset picks."""
    bt = _make_bt(preset)
    bt.real_trvs["climate.x"].advanced["mpc_v2_flow_temperature"] = 38.0

    _compute_mpc_v2_balance(bt, "climate.x")

    assert _live_controller(bt).plant_coarse.params.T_water == 38.0


def test_an_accepted_fit_records_the_flow_temperature_it_was_fitted_under(
    monkeypatch,
) -> None:
    """The stored result names the supply water its heater gain belongs to."""
    from custom_components.better_thermostat import calibration as cal
    from custom_components.better_thermostat.utils.calibration.mpc_v2 import ReidOutcome

    bt = _make_bt()
    bt.hass = _FakeHass()
    key, _runtime = _due_fit(bt)
    priors: list[PlantParams] = []

    def _fake_fit(samples, prior):
        priors.append(prior)
        return ReidOutcome(
            status="accepted",
            tau_room_min=_REID.tau_room_minutes,
            gain_heater=_REID.gain_heater,
            rmse_prior_K=0.4,
            rmse_fit_K=0.1,
            n_segments=4,
            n_samples=300,
        )

    monkeypatch.setattr(cal, "run_reid_fit", _fake_fit)

    cal._maybe_start_mpc_v2_reid_fit(
        bt, key, MpcV2Params(plant=PlantParams(T_water=42.0))
    )

    assert [prior.T_water for prior in priors] == [42.0]
    adopted = bt.state_mgr.get_mpc_v2_reid(key)
    assert adopted is not None
    assert adopted.flow_temperature == 42.0


@pytest.mark.parametrize("shared", [True, False], ids=["shared_key", "bucket_key"])
@pytest.mark.parametrize(
    ("fitted", "configured", "applies"),
    [(65.0, 65.0, True), (40.0, 40.0, True), (65.0, 40.0, False)],
    ids=["default_flow", "same_low_flow", "other_flow"],
)
def test_a_fit_seeds_the_prior_only_under_its_own_flow_temperature(
    shared: bool, fitted: float, configured: float, applies: bool
) -> None:
    """A gain fitted under one flow temperature misstates the room under another.

    A result seeds the prior while the flow temperature it was fitted under
    is configured, and the plant models that flow temperature with it. Under
    another one the room runs on the heat-loss prior until a fit under the
    new value is adopted, wherever the old result is stored.
    """
    from custom_components.better_thermostat import calibration as cal

    bt = _make_bt()
    bt.real_trvs["climate.x"].advanced["mpc_v2_flow_temperature"] = configured
    key = cal._build_mpc_v2_reid_key(bt) if shared else "bt:climate.x:t21.0"
    bt.state_mgr.adopt_mpc_v2_reid(key, replace(_REID, flow_temperature=fitted))

    out, _ = _compute_mpc_v2_balance(bt, "climate.x")

    assert out is not None
    plant = _live_controller(bt).plant_coarse.params
    assert plant.T_water == configured
    assert (plant.tau_room_min == _REID.tau_room_minutes) is applies
    assert (plant.gain_heater == _REID.gain_heater) is applies
    debug = bt.real_trvs["climate.x"].calibration_balance["debug"]
    assert (debug["reid_gain"] == _REID.gain_heater) is applies


def test_a_stored_fit_without_a_flow_temperature_was_fitted_under_the_default() -> None:
    """Results stored before the field existed were all fitted under 65 °C."""
    restored = deserialize_mpc_v2_reid(
        {"tau_room_min": 300.0, "gain_heater": 2.5, "fitted_ts": 1.0}
    )

    assert restored is not None
    assert restored.flow_temperature == DEFAULT_FLOW_TEMPERATURE


def test_the_recorded_flow_temperature_survives_a_restart() -> None:
    """The flow temperature of a fit is stored with it and read back."""
    mgr = _make_manager()
    mgr.adopt_mpc_v2_reid("k", replace(_REID, flow_temperature=38.5))

    restored = _deserialize(_serialize(mgr.state))

    assert restored.mpc_v2_reid["k"].flow_temperature == 38.5


def test_a_flow_temperature_change_never_rebuilds_a_running_controller() -> None:
    """The flow temperature is no part of the plant signature.

    A new flow temperature arrives with the entry reload an options change
    triggers, never on a running controller.
    """
    assert _plant_signature_of(
        MpcV2Params(plant=PlantParams(T_water=40.0))
    ) == _plant_signature_of(MpcV2Params(plant=PlantParams(T_water=65.0)))


def test_the_reload_after_a_flow_temperature_change_keeps_the_observer_state() -> None:
    """The controller rebuilt on reload models the new supply water.

    What the observer learned about the room, the Kalman estimate, the
    disturbance reading, the error integral, the governor and the command
    history, comes back from the snapshot the unload stored.
    """
    mgr = _make_manager()
    _warm(mgr, "k", MpcV2Params())
    old = mgr.get_mpc_v2_live("k", MpcV2Params())
    assert old.controller is not None
    payload = export_mpc_v2_state(old)
    assert payload is not None

    reloaded = import_mpc_v2_state(
        payload, MpcV2Params(plant=PlantParams(T_water=40.0)), key="k"
    )

    assert reloaded.controller is not None
    assert reloaded.controller.plant_coarse.params.T_water == 40.0
    assert reloaded.controller.export_snapshot() == old.controller.export_snapshot()


def test_a_fit_under_a_new_flow_temperature_replaces_the_old_result(
    monkeypatch,
) -> None:
    """One result per thermostat: going back only helps before the next fit.

    Under a new flow temperature the stored result stays put and applies
    again when its own flow temperature comes back. The next accepted fit is
    stored under the same key with the new flow temperature, and from then
    on the old one finds nothing.
    """
    from custom_components.better_thermostat import calibration as cal
    from custom_components.better_thermostat.utils.calibration.mpc_v2 import ReidOutcome

    bt = _make_bt()
    bt.hass = _FakeHass()
    key, _runtime = _due_fit(bt)
    mpc_key = "bt:climate.x:t21.0"
    bt.state_mgr.adopt_mpc_v2_reid(key, replace(_REID, flow_temperature=65.0))

    assert cal._lookup_mpc_v2_reid(bt, key, mpc_key, 40.0) is None
    back = cal._lookup_mpc_v2_reid(bt, key, mpc_key, 65.0)
    assert back is not None
    assert back.flow_temperature == 65.0

    def _fake_fit(samples, prior):
        return ReidOutcome(
            status="accepted",
            tau_room_min=180.0,
            gain_heater=1.5,
            rmse_prior_K=0.4,
            rmse_fit_K=0.1,
            n_segments=4,
            n_samples=300,
        )

    monkeypatch.setattr(cal, "run_reid_fit", _fake_fit)
    cal._maybe_start_mpc_v2_reid_fit(
        bt, key, MpcV2Params(plant=PlantParams(T_water=40.0))
    )

    assert [
        data.flow_temperature for data in bt.state_mgr.state.mpc_v2_reid.values()
    ] == [40.0]
    assert cal._lookup_mpc_v2_reid(bt, key, mpc_key, 65.0) is None
    fitted = cal._lookup_mpc_v2_reid(bt, key, mpc_key, 40.0)
    assert fitted is not None
    assert fitted.gain_heater == 1.5


# -- Paused learning ----------------------------------------------------------


def _accepted_outcome() -> Any:
    from custom_components.better_thermostat.utils.calibration.mpc_v2 import ReidOutcome

    return ReidOutcome(
        status="accepted",
        tau_room_min=_REID.tau_room_minutes,
        gain_heater=_REID.gain_heater,
        rmse_prior_K=0.4,
        rmse_fit_K=0.1,
        n_segments=4,
        n_samples=300,
    )


def test_a_held_dispatch_records_a_sample_that_cuts_the_buffer() -> None:
    """While learning is held each cycle records a paused sample, valve known or not."""
    bt = _make_bt()
    bt.learning_held = True
    out, _ = _compute_mpc_v2_balance(bt, "climate.x")

    assert out is not None
    key = next(iter(bt.state_mgr._mpc_v2_reid_live))
    samples = bt.state_mgr.get_mpc_v2_reid_runtime(key).buffer.samples
    assert [sample.learning_paused for sample in samples] == [True]


def test_a_long_pause_leaves_the_recorded_samples_in_the_buffer() -> None:
    """A pause records one cut, however many cycles it holds."""
    bt = _make_bt()
    bt.real_trvs["climate.x"].last_valve_percent = 37
    _compute_mpc_v2_balance(bt, "climate.x")
    bt.learning_held = True
    for _ in range(5):
        bt.clock.advance(300.0)
        _compute_mpc_v2_balance(bt, "climate.x")

    key = next(iter(bt.state_mgr._mpc_v2_reid_live))
    samples = bt.state_mgr.get_mpc_v2_reid_runtime(key).buffer.samples
    assert [sample.learning_paused for sample in samples] == [False, True]


def test_a_pause_shorter_than_the_gap_still_splits_the_segments() -> None:
    """No transient spans a pause, however short: its samples cut the run."""
    from custom_components.better_thermostat.utils.calibration.mpc_v2.reid import (
        ReidBuffer,
        extract_segments,
    )

    config = ReidConfig()
    buffer = ReidBuffer()
    for i in range(20):
        buffer.append(
            ReidSample(
                t_s=i * 300.0,
                T_room=19.0 + 0.2 * i,
                u_frac=0.8,
                T_outdoor=5.0,
                learning_paused=i == 10,
            )
        )

    segments = extract_segments(buffer.samples, config)
    assert len(segments) == 2
    assert all(sample.t_s != 3000.0 for s in segments for sample in s.samples)
    unpaused = [replace(sample, learning_paused=False) for sample in buffer.samples]
    assert len(extract_segments(unpaused, config)) == 1


def test_a_paused_sample_inside_the_spacing_floor_marks_the_one_before() -> None:
    """A pause starting right after a sample still cuts the buffer there."""
    from custom_components.better_thermostat.utils.calibration.mpc_v2.reid import (
        ReidBuffer,
    )

    buffer = ReidBuffer()
    buffer.append(ReidSample(t_s=0.0, T_room=20.0, u_frac=0.8, T_outdoor=5.0))
    buffer.append(ReidSample(t_s=10.0, T_room=20.0, u_frac=0.0, learning_paused=True))

    assert len(buffer.samples) == 1
    assert buffer.samples[0].learning_paused is True


def test_no_fit_starts_while_learning_is_held(monkeypatch) -> None:
    """A fit that is due waits for the pause to end."""
    from custom_components.better_thermostat import calibration as cal

    bt = _make_bt()
    bt.hass = _FakeHass()
    key, runtime = _due_fit(bt)
    calls: list[int] = []
    monkeypatch.setattr(
        cal,
        "run_reid_fit",
        lambda samples, prior: calls.append(len(samples)) or _accepted_outcome(),
    )

    bt.learning_held = True
    cal._maybe_start_mpc_v2_reid_fit(bt, key, MpcV2Params())
    assert calls == []
    assert runtime.last_fit_attempt_ts == 0.0

    bt.learning_held = False
    cal._maybe_start_mpc_v2_reid_fit(bt, key, MpcV2Params())
    assert len(calls) == 1
    assert bt.state_mgr.get_mpc_v2_reid(key) is not None


def test_a_fit_finishing_while_learning_is_held_is_discarded(monkeypatch) -> None:
    """A fit started before the pause adopts nothing when it completes during it."""
    from custom_components.better_thermostat import calibration as cal

    pending = _PendingFuture(_accepted_outcome())
    bt = _make_bt()
    bt.hass = SimpleNamespace(
        async_add_executor_job=lambda func, *args: pending, config=_METRIC_CONFIG
    )
    key, runtime = _due_fit(bt)
    bt.schedule_save_state.reset_mock()

    cal._maybe_start_mpc_v2_reid_fit(bt, key, MpcV2Params())
    bt.learning_held = True
    pending.resolve()

    assert bt.state_mgr.get_mpc_v2_reid(key) is None
    bt.schedule_save_state.assert_not_called()
    assert runtime.fit_inflight is False
