"""Integration tests for re-identification: adopt path, persistence, dispatch."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.climate.const import HVACMode
from homeassistant.core import State

from custom_components.better_thermostat.calibration import _compute_mpc_v2_balance
from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.fsm.control_mode import ControlMode
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.calibration.mpc_v2 import (
    MpcV2Input,
    MpcV2Params,
    ReidConfig,
    ReidSample,
    compute_mpc_v2,
)
from custom_components.better_thermostat.utils.const import (
    CalibrationMode,
    CalibrationOutput,
    MpcV2PlantPreset,
)
from custom_components.better_thermostat.utils.state_manager import (
    MpcV2ReidData,
    RuntimeState,
    StateManager,
    _deserialize,
    _serialize,
    deserialize_mpc_v2_reid,
)

_REID = MpcV2ReidData(
    tau_room_min=240.0,
    gain_heater=3.0,
    fitted_ts=1000.0,
    rmse_prior_K=0.4,
    rmse_fit_K=0.1,
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
            target_temp_C=22.0,
            current_temp_C=19.0,
            outdoor_temp_C=5.0,
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
    new_params.plant.tau_room_min = _REID.tau_room_min
    new_params.plant.gain_heater = _REID.gain_heater
    rebuilt = mgr.get_mpc_v2_live("k", new_params)
    assert rebuilt is not old
    assert rebuilt.controller is not None
    assert rebuilt.controller is not old.controller
    assert rebuilt.controller._last_u == last_u_before
    assert rebuilt.controller.plant_fine.params.tau_room_min == _REID.tau_room_min


def test_adopt_marks_dirty_and_result_readable() -> None:
    """The adopted result is stored, retrievable, and flagged for saving."""
    mgr = _make_manager()
    mgr.adopt_mpc_v2_reid("k", _REID)
    assert mgr.dirty is True
    stored = mgr.get_mpc_v2_reid("k")
    assert stored is not None
    assert stored.tau_room_min == 240.0


def test_reid_result_survives_serialization_round_trip() -> None:
    """serialize -> deserialize reproduces the persisted re-ID result."""
    mgr = _make_manager()
    mgr.adopt_mpc_v2_reid("k", _REID)
    raw = _serialize(mgr.state)
    restored = _deserialize(raw)
    assert restored.mpc_v2_reid["k"].tau_room_min == 240.0
    assert restored.mpc_v2_reid["k"].gain_heater == 3.0
    assert restored.mpc_v2_reid["k"].n_segments == 4


def test_deserialize_rejects_malformed_reid_payload() -> None:
    """Zero/garbage fitted components cannot seed a plant prior."""
    assert deserialize_mpc_v2_reid({"tau_room_min": 0.0, "gain_heater": 2.0}) is None
    assert deserialize_mpc_v2_reid({"tau_room_min": "junk"}) is None
    ok = deserialize_mpc_v2_reid({"tau_room_min": 300.0, "gain_heater": 2.5})
    assert ok is not None and ok.tau_room_min == 300.0


# -- Dispatcher wiring --------------------------------------------------------


def _make_trv(entity_id: str, preset: MpcV2PlantPreset) -> Trv:
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


def _make_bt(preset: MpcV2PlantPreset = MpcV2PlantPreset.AUTO) -> Any:
    """Build a minimal BT-shaped namespace with a real StateManager."""
    return SimpleNamespace(
        real_trvs={"climate.x": _make_trv("climate.x", preset)},
        heat_target_temperature=21.0,
        room_temperature=19.5,
        tolerance=0.0,
        window_open=False,
        contact_open=False,
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
        state_mgr=_make_manager(),
        clock=FakeClock(monotonic_value=1_000_000.0),
        schedule_save_state=MagicMock(),
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
    assert live.controller.plant_fine.params.tau_room_min == _REID.tau_room_min
    assert live.controller.plant_fine.params.gain_heater == _REID.gain_heater
    debug = bt.real_trvs["climate.x"].calibration_balance["debug"]
    assert debug["reid_tau_room"] == _REID.tau_room_min


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
        "bt:reid": {"tau_room_min": 5e-324, "gain_heater": 3.0, "fitted_ts": 1000.0}
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
    assert sample.T_room_C == 19.5
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
                t_s=i * spacing, T_room_C=19.0 + 0.2 * i, u_frac=0.8, T_outdoor_C=5.0
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
                T_room_C=20.0 + 0.2 * i,
                u_frac=0.8,
                T_outdoor_C=5.0,
            )
        )

    segments = extract_segments(samples, config)
    assert len(segments) == 2
    assert all(s.kind == "heatup" for s in segments)
    # Without the gap the same samples form one contiguous run.
    contiguous = [
        ReidSample(
            t_s=i * spacing, T_room_C=19.0 + 0.1 * i, u_frac=0.8, T_outdoor_C=5.0
        )
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


class _FakeHass:
    """hass double whose executor runs the job inline."""

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
        runtime.buffer.append(ReidSample(t_s=float(i * 300), T_room_C=20.0, u_frac=0.5))
    runtime.last_fit_attempt_ts = 0.0

    calls: list[int] = []

    def _fake_fit(samples, prior):
        calls.append(len(samples))
        return ReidOutcome(
            status="accepted",
            tau_room_min=_REID.tau_room_min,
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
    assert adopted.tau_room_min == _REID.tau_room_min
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
        runtime.buffer.append(ReidSample(t_s=float(i * 300), T_room_C=20.0, u_frac=0.5))
    runtime.last_fit_attempt_ts = 0.0
    monkeypatch.setattr(
        cal,
        "run_reid_fit",
        lambda samples, prior: ReidOutcome(
            status="accepted",
            tau_room_min=_REID.tau_room_min,
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
    new_params.plant.tau_room_min = _REID.tau_room_min
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
        tau_room_min=600.0, gain_heater=1.0, fitted_ts=100.0, n_segments=3
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
    assert restored.mpc_v2_reid["uid:reid"].tau_room_min == 240.0


def test_prior_lookup_falls_back_to_legacy_bucket_keys() -> None:
    """A result persisted under a per-bucket key still seeds the prior."""
    bt = _make_bt()
    # Legacy layout: result stored under the entity's target-bucket key.
    bt.state_mgr.adopt_mpc_v2_reid("bt:climate.x:t21.0", _REID)

    out, _ = _compute_mpc_v2_balance(bt, "climate.x")
    assert out is not None
    live = next(iter(bt.state_mgr._mpc_v2_live.values()))
    assert live.controller is not None
    assert live.controller.plant_fine.params.tau_room_min == _REID.tau_room_min


def test_prior_lookup_prefers_shared_key_over_bucket_entries() -> None:
    """When both layouts exist, the shared-key result wins."""
    from custom_components.better_thermostat.calibration import _lookup_mpc_v2_reid

    bt = _make_bt()
    legacy = MpcV2ReidData(
        tau_room_min=600.0, gain_heater=1.0, fitted_ts=2_000_000.0, n_segments=3
    )
    bt.state_mgr.adopt_mpc_v2_reid("bt:climate.x:t21.0", legacy)
    bt.state_mgr.adopt_mpc_v2_reid("bt:reid", _REID)

    found = _lookup_mpc_v2_reid(bt, "bt:reid", "bt:climate.x:t21.0")
    assert found is _REID


def test_prior_lookup_picks_freshest_legacy_bucket_entry() -> None:
    """Among legacy bucket entries, the most recently fitted one wins."""
    from custom_components.better_thermostat.calibration import _lookup_mpc_v2_reid

    bt = _make_bt()
    stale = MpcV2ReidData(
        tau_room_min=600.0, gain_heater=1.0, fitted_ts=100.0, n_segments=3
    )
    fresh = MpcV2ReidData(
        tau_room_min=300.0, gain_heater=2.5, fitted_ts=200.0, n_segments=3
    )
    bt.state_mgr.adopt_mpc_v2_reid("bt:climate.x:t19.0", stale)
    bt.state_mgr.adopt_mpc_v2_reid("bt:climate.x:t21.0", fresh)

    found = _lookup_mpc_v2_reid(bt, "bt:reid", "bt:climate.x:t22.0")
    assert found is fresh


# -- Preset and compute failures ----------------------------------------------


def test_dispatch_reads_an_unknown_stored_preset_as_auto() -> None:
    """A preset this version does not know runs as AUTO and keeps sampling."""
    bt = _make_bt(preset="retired_preset")  # type: ignore[arg-type]
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
        runtime.buffer.append(ReidSample(t_s=float(i * 300), T_room_C=20.0, u_frac=0.5))
    runtime.last_fit_attempt_ts = 0.0
    return key, runtime


class _RaisingFuture(_FakeFuture):
    """Future double whose job raised."""

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


def test_a_fit_that_crashes_frees_the_next_attempt(caplog) -> None:
    """A crashed fit is reported and does not block every later one."""
    from custom_components.better_thermostat import calibration as cal

    bt = _make_bt()
    bt.hass = SimpleNamespace(
        async_add_executor_job=lambda func, *args: _RaisingFuture(None)
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
    bt.hass = SimpleNamespace(async_add_executor_job=_refuse)
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

    assert cal._lookup_mpc_v2_reid(bt, "bt:reid", "bt:climate.x:t21.0") is None
    cal._record_mpc_v2_reid_sample(
        bt, "bt:reid", applied_valve_pct=40.0, trv_temp=21.0, outdoor_temperature=5.0
    )
    cal._maybe_start_mpc_v2_reid_fit(bt, "bt:reid", MpcV2Params())
