"""Integration-level tests for ``_compute_mpc_v2_balance`` dispatch.

Covers the multi-TRV branch (group key + ``distribute_valve_percent``)
that the controller-level tests can't exercise — the dispatcher reads
``self.real_trvs``, ``self.heat_target_temperature`` and friends off the BT entity.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from homeassistant.components.climate.const import HVACMode

from custom_components.better_thermostat.calibration import _compute_mpc_v2_balance
from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.fsm.control_mode import ControlMode
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.calibration.mpc_v2 import (
    MpcV2Params,
    MpcV2State,
)
from custom_components.better_thermostat.utils.const import (
    CalibrationMode,
    CalibrationOutput,
    MpcV2PlantPreset,
)
from custom_components.better_thermostat.utils.state_manager import MpcV2ReidRuntime


class _FakeStateManager:
    """In-memory stand-in for the dispatcher's StateManager dependency.

    Holds the live MPC v2 controller state by key across calls so the
    multi-TRV group test sees the controller the first dispatch advanced when
    the second dispatch reads it.
    """

    def __init__(self) -> None:
        """Start with an empty per-key live MPC v2 state store."""
        self._mpc_v2_live: dict[str, MpcV2State] = {}
        self._mpc_v2_reid_live: dict[str, MpcV2ReidRuntime] = {}
        self.mpc_v2_reid: dict[str, object] = {}

    @property
    def state(self):
        """Return self so ``state.mpc_v2_reid`` resolves like on the real store."""
        return self

    def get_mpc_v2_live(self, key: str, params: MpcV2Params) -> MpcV2State:
        """Return the live state for key, creating a fresh one on first use."""
        live = self._mpc_v2_live.get(key)
        if live is None:
            live = MpcV2State()
            self._mpc_v2_live[key] = live
        return live

    def set_mpc_v2_live(self, key: str, state: MpcV2State) -> None:
        """Store the live MPC v2 state for key."""
        self._mpc_v2_live[key] = state

    def get_mpc_v2_reid(self, key: str) -> None:
        """No persisted re-identification result in the fake store."""
        return None

    def get_mpc_v2_reid_runtime(self, key: str) -> MpcV2ReidRuntime:
        """Return the in-memory re-ID collection state, building it on first use."""
        runtime = self._mpc_v2_reid_live.get(key)
        if runtime is None:
            runtime = MpcV2ReidRuntime()
            self._mpc_v2_reid_live[key] = runtime
        return runtime


def _make_bt(*, real_trvs: dict[str, Trv], unique_id: str = "bt_test") -> Any:
    """Build a minimal BT-shaped namespace good enough for the dispatcher."""
    return SimpleNamespace(
        real_trvs=real_trvs,
        heat_target_temperature=21.0,
        room_temperature=19.5,
        room_temperature_filtered=None,
        tolerance=0.0,
        temperature_slope=None,
        window_open=False,
        door_open=False,
        # The real entity derives this from both contacts; the dispatcher
        # reads the combined flag, so the stand-in has to carry it too.
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
        unique_id=unique_id,
        device_id="bt_test_device",
        entry_id="bt_test_entry",
        state_mgr=_FakeStateManager(),
        clock=FakeClock(monotonic_value=1_000_000.0),
        schedule_save_state=MagicMock(),
    )


def _make_trv(
    entity_id: str,
    *,
    current_temperature: float | None,
    supports_valve: bool,
    max_temp: float = 30.0,
    valve_max_opening: float = 100.0,
) -> Trv:
    """Build a Trv configured for MPC v2 calibration."""
    return Trv(
        entity_id=entity_id,
        current_temperature=current_temperature,
        valve_max_opening=valve_max_opening,
        advanced={
            "calibration": (
                CalibrationOutput.DIRECT_VALVE_BASED
                if supports_valve
                else CalibrationOutput.TARGET_TEMP_BASED
            ),
            "calibration_mode": CalibrationMode.MPC_V2_CALIBRATION,
            "mpc_v2_plant_preset": MpcV2PlantPreset.AUTO,
        },
        valve_position_writable=supports_valve,
        valve_position_entity="number.trv_valve" if supports_valve else None,
        max_temp=max_temp,
        model_quirks=None,
    )


def test_multi_trv_distributes_group_valve() -> None:
    """Cold TRV gets more opening than warm TRV out of the same group %."""
    real_trvs = {
        "climate.living_cold": _make_trv(
            "climate.living_cold", current_temperature=19.0, supports_valve=True
        ),
        "climate.living_warm": _make_trv(
            "climate.living_warm", current_temperature=21.0, supports_valve=True
        ),
    }
    bt = _make_bt(real_trvs=real_trvs)

    # Dispatch each TRV. The group MPC controller is stateful and advances one
    # step per dispatch, so the two calls observe slightly different group
    # commands; the cold-favouring split is therefore asserted directly on
    # distribute_valve_percent below rather than across the two outputs.
    out_cold, _ = _compute_mpc_v2_balance(bt, "climate.living_cold")
    out_warm, _ = _compute_mpc_v2_balance(bt, "climate.living_warm")

    assert out_cold is not None and out_warm is not None
    cal_cold = real_trvs["climate.living_cold"].calibration_balance
    cal_warm = real_trvs["climate.living_warm"].calibration_balance

    # Both TRVs report a v2 group command so telemetry surfaces v2 attrs.
    assert cal_cold["debug"]["controller_version"] == "v2"
    assert cal_warm["debug"]["controller_version"] == "v2"

    # The cold-favouring split is verified through the dispatcher's own
    # per-TRV distributed output (the real wiring), not a standalone call.
    assert (
        cal_cold["debug"]["distributed_valve_pct"]
        >= cal_warm["debug"]["distributed_valve_pct"]
    )


def test_multi_trv_clamps_to_per_trv_max_opening() -> None:
    """A boosted cold TRV is clamped to its own configured max opening.

    The controller only sees the warmest TRV's cap, so the distribution can
    hand the cold TRV a share above its own limit; the dispatcher must clamp
    the per-TRV command to that TRV's ``valve_max_opening``.
    """
    real_trvs = {
        "climate.living_cold": _make_trv(
            "climate.living_cold",
            current_temperature=15.0,
            supports_valve=True,
            valve_max_opening=40.0,
        ),
        "climate.living_warm": _make_trv(
            "climate.living_warm", current_temperature=22.0, supports_valve=True
        ),
    }
    bt = _make_bt(real_trvs=real_trvs)
    bt.heat_target_temperature = 25.0
    bt.room_temperature = 15.0

    out_cold, _ = _compute_mpc_v2_balance(bt, "climate.living_cold")

    assert out_cold is not None
    cal_cold = real_trvs["climate.living_cold"].calibration_balance
    # The group command exceeds the cold TRV's cap, so the clamp is exercised
    # (the distribution only ever adds to the group value for a colder TRV).
    assert cal_cold["debug"]["group_valve_pct"] > 40.0
    assert cal_cold["valve_percent"] == 40
    assert out_cold.valve_percent == 40


def test_single_trv_passes_through_without_distribution() -> None:
    """One TRV ⇒ no distribute_valve_percent splitting, group == per-TRV."""
    real_trvs = {
        "climate.solo": _make_trv(
            "climate.solo", current_temperature=19.0, supports_valve=True
        )
    }
    bt = _make_bt(real_trvs=real_trvs)

    out, supports = _compute_mpc_v2_balance(bt, "climate.solo")
    assert out is not None
    assert supports is True

    cal = real_trvs["climate.solo"].calibration_balance
    assert cal["debug"]["group_valve_pct"] == cal["debug"]["distributed_valve_pct"]
    assert cal["debug"]["controller_version"] == "v2"


def test_hvac_off_returns_none() -> None:
    """HVAC OFF must short-circuit before constructing a controller."""
    real_trvs = {
        "climate.x": _make_trv(
            "climate.x", current_temperature=19.0, supports_valve=True
        )
    }
    bt = _make_bt(real_trvs=real_trvs)
    bt.bt_hvac_mode = HVACMode.OFF

    out, supports = _compute_mpc_v2_balance(bt, "climate.x")
    assert out is None
    assert supports is False
    assert real_trvs["climate.x"].calibration_balance is None


def test_dispatch_without_a_state_store_publishes_no_valve() -> None:
    """Before the store is loaded there is no controller state to run on.

    A valve intent from an earlier cycle must not outlive the missing store.
    """
    real_trvs = {
        "climate.x": _make_trv(
            "climate.x", current_temperature=19.0, supports_valve=True
        )
    }
    real_trvs["climate.x"].calibration_balance = {"valve_percent": 80}
    bt = _make_bt(real_trvs=real_trvs)
    bt.state_mgr = None

    out, supports = _compute_mpc_v2_balance(bt, "climate.x")

    assert out is None
    assert supports is False
    assert real_trvs["climate.x"].calibration_balance is None


def test_missing_room_temperature_returns_none() -> None:
    """A missing BT room temperature short-circuits the dispatch."""
    real_trvs = {
        "climate.x": _make_trv(
            "climate.x", current_temperature=19.0, supports_valve=True
        )
    }
    bt = _make_bt(real_trvs=real_trvs)
    bt.room_temperature = None

    out, supports = _compute_mpc_v2_balance(bt, "climate.x")
    assert out is None
    assert supports is False


def test_daqp_absence_uses_portable_solver(monkeypatch) -> None:
    """MPC v2 keeps dispatching through the portable solver without DAQP."""
    from custom_components.better_thermostat.utils.calibration.mpc_v2_internals import (
        qp_optimiser,
    )

    monkeypatch.setattr(qp_optimiser, "DAQP_AVAILABLE", False)
    monkeypatch.setattr(qp_optimiser, "_daqp", None)

    real_trvs = {
        "climate.x": _make_trv(
            "climate.x", current_temperature=19.0, supports_valve=True
        )
    }
    bt = _make_bt(real_trvs=real_trvs)

    out, supports = _compute_mpc_v2_balance(bt, "climate.x")

    assert out is not None and supports is True
    assert real_trvs["climate.x"].calibration_balance is not None


def _capture_controller_input(monkeypatch) -> list[Any]:
    """Record every MpcV2Input the dispatcher hands to the controller."""
    from custom_components.better_thermostat import calibration

    seen: list[Any] = []
    real_compute = calibration.compute_mpc_v2

    def _recording_compute(inp, params, state=None, **kwargs):
        seen.append(inp)
        return real_compute(inp, params, state=state, **kwargs)

    monkeypatch.setattr(calibration, "compute_mpc_v2", _recording_compute)
    return seen


def test_setpoint_steered_trv_keeps_its_reported_opening_out_of_the_controller(
    monkeypatch,
) -> None:
    """A TRV BT steers through its setpoint must not feed its opening back as BT's command.

    Between re-plans the controller returns its applied input unchanged, so a
    reported opening the TRV chose itself would become BT's next command.
    """
    seen = _capture_controller_input(monkeypatch)
    trv = _make_trv("climate.x", current_temperature=19.0, supports_valve=False)
    trv.valve_position = 80.0
    bt = _make_bt(real_trvs={"climate.x": trv})

    _compute_mpc_v2_balance(bt, "climate.x")

    assert len(seen) == 1
    assert seen[0].applied_valve_pct is None


def test_setpoint_steered_trv_reported_opening_still_feeds_re_identification(
    monkeypatch,
) -> None:
    """The reported opening must stay a re-identification sample of the room's input."""
    _capture_controller_input(monkeypatch)
    trv = _make_trv("climate.x", current_temperature=19.0, supports_valve=False)
    trv.valve_position = 80.0
    bt = _make_bt(real_trvs={"climate.x": trv})

    _compute_mpc_v2_balance(bt, "climate.x")

    runtimes = list(bt.state_mgr._mpc_v2_reid_live.values())
    assert len(runtimes) == 1
    samples = runtimes[0].buffer.samples
    assert [s.u_frac for s in samples] == [0.8]


def test_direct_valve_trv_feeds_its_reported_opening_to_the_controller(
    monkeypatch,
) -> None:
    """A valve BT writes itself must reach the controller as the applied input."""
    seen = _capture_controller_input(monkeypatch)
    trv = _make_trv("climate.x", current_temperature=19.0, supports_valve=True)
    trv.valve_position = 80.0
    bt = _make_bt(real_trvs={"climate.x": trv})

    _compute_mpc_v2_balance(bt, "climate.x")

    assert len(seen) == 1
    assert seen[0].applied_valve_pct == 80.0
