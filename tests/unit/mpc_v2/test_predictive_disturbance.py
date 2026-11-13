"""Tests for the predictive-disturbance plumbing into MPC v2."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.better_thermostat.calibration import _learn_disturbance_profile
from custom_components.better_thermostat.core.fsm.control_mode import ControlMode
from custom_components.better_thermostat.utils.calibration.disturbance_forecast import (
    HourlyForecastPoint,
)
from custom_components.better_thermostat.utils.calibration.disturbance_profile import (
    DisturbanceProfile,
    half_hour,
)
from custom_components.better_thermostat.utils.calibration.mpc_v2 import (
    MpcV2Input,
    MpcV2Params,
    compute_mpc_v2,
)
from custom_components.better_thermostat.utils.calibration.mpc_v2.controller import (
    PREDICTIVE_SETBACK_MARGIN_K,
    MpcV2Controller,
)
from custom_components.better_thermostat.utils.calibration.mpc_v2.io import (
    MpcV2Diagnostics,
)

MONDAY_NOON = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)


def _diagnostics(**overrides: object) -> MpcV2Diagnostics:
    values: dict[str, Any] = {
        "T_room_hat": 20.0,
        "T_rad_hat": 40.0,
        "D_hat_K_per_min": 0.02,
        "tau_room_min": 480.0,
        "coupling_rad_room": 1.0,
        "disturbance_at_bound": False,
    }
    values.update(overrides)
    return MpcV2Diagnostics(**values)


def _plan(predicted: tuple[float, ...]) -> float:
    controller = MpcV2Controller(MpcV2Params())
    # The room sits on the setpoint on a mild day, so the first command is not
    # pinned on the rate limit and the predicted gain can move it.
    u, _ = controller.step(
        t_s=0.0,
        T_room=21.0,
        T_target=21.0,
        T_outdoor=10.0,
        predicted_disturbance_K_per_min=predicted,
    )
    return u


def test_the_safety_hull_caps_the_predicted_gain_by_tau() -> None:
    controller = MpcV2Controller(MpcV2Params())
    cap = PREDICTIVE_SETBACK_MARGIN_K / controller.plant_fine.params.tau_room_min

    bounded = controller._bounded_disturbance((0.1, -1.0, 0.001, float("nan")))

    assert bounded == (cap, 0.0, 0.001, 0.0)


def test_an_empty_profile_leaves_the_constant_estimate_in_charge() -> None:
    controller = MpcV2Controller(MpcV2Params())
    assert controller._bounded_disturbance(()) is None
    assert controller._bounded_disturbance((0.0, 0.0)) is None


def test_a_predicted_gain_holds_the_valve_back() -> None:
    baseline = _plan(())
    predicted = _plan((0.01,) * 24)

    assert predicted < baseline


def test_compute_builds_the_profile_on_the_controller_grid() -> None:
    profile = DisturbanceProfile().learn(
        now_ts=MONDAY_NOON.timestamp(),
        weekday=0,
        month=1,
        hour=12,
        minute=0,
        disturbance_kelvin_per_min=0.02,
        clear_sky_factor=0.5,
        is_day=True,
    )
    hourly = (
        HourlyForecastPoint(timestamp=MONDAY_NOON.timestamp(), clear_sky_factor=0.5),
    )
    inp = MpcV2Input(
        key="room",
        target_temperature=21.0,
        room_temperature=20.0,
        outdoor_temperature=0.0,
        disturbance_profile=profile,
        hourly_forecast=hourly,
        now_wall=MONDAY_NOON,
    )
    diagnostics = _diagnostics()

    with patch.object(
        MpcV2Controller, "step", autospec=True, return_value=(0.0, diagnostics)
    ) as step:
        output, _state = compute_mpc_v2(inp, MpcV2Params())

    assert output is not None
    predicted = step.call_args.kwargs["predicted_disturbance_K_per_min"]
    assert len(predicted) == MpcV2Params().qp.horizon_steps
    assert predicted[0] == pytest.approx(0.02)
    assert output.diagnostics.predicted_disturbance_kelvin_per_min == pytest.approx(
        0.02
    )
    # Later steps leave the learned half-hour bucket; nothing is invented.
    assert predicted[-1] == 0.0


def test_without_a_profile_the_prediction_stays_unset() -> None:
    inp = MpcV2Input(
        key="room",
        target_temperature=21.0,
        room_temperature=20.0,
        outdoor_temperature=0.0,
    )

    with patch.object(
        MpcV2Controller, "step", autospec=True, return_value=(0.0, _diagnostics())
    ):
        output, _state = compute_mpc_v2(inp, MpcV2Params())

    assert output is not None
    assert output.diagnostics.predicted_disturbance_kelvin_per_min is None


class _FakeStateManager:
    def __init__(self) -> None:
        self.profiles: dict[str, DisturbanceProfile] = {}
        self.writes = 0

    def get_disturbance_profile(self, key: str) -> DisturbanceProfile:
        return self.profiles.get(key, DisturbanceProfile())

    def set_disturbance_profile(self, key: str, profile: DisturbanceProfile) -> None:
        self.writes += 1
        self.profiles[key] = profile


def _fake_shell(*, mode: ControlMode = ControlMode.OPTIMAL) -> Any:
    return SimpleNamespace(
        learning_held=False,
        heating_blocked_by=None,
        kernel_state=SimpleNamespace(control_mode=SimpleNamespace(mode=mode)),
        state_mgr=_FakeStateManager(),
        hass=None,
        weather_entity_id=None,
        hourly_forecast=None,
    )


def test_a_clean_plan_teaches_the_routine_bucket() -> None:
    shell = _fake_shell()

    _learn_disturbance_profile(shell, "room", _diagnostics(), MONDAY_NOON)

    profile = shell.state_mgr.profiles["room"]
    bucket = half_hour(12, 0)
    assert profile.routine_gain_kelvin_per_min[bucket] == pytest.approx(0.02)
    assert profile.routine_samples[bucket] == 1.0
    assert profile.last_learned_ts == MONDAY_NOON.timestamp()
    assert shell.state_mgr.writes == 1


@pytest.mark.parametrize(
    "blocker", ["learning_held", "heating_blocked_by", "at_bound", "standby"]
)
def test_unclean_plans_teach_nothing(blocker: str) -> None:
    shell = _fake_shell(
        mode=ControlMode.HOLD if blocker == "standby" else ControlMode.OPTIMAL
    )
    diagnostics = _diagnostics(disturbance_at_bound=blocker == "at_bound")
    if blocker == "learning_held":
        shell.learning_held = True
    if blocker == "heating_blocked_by":
        shell.heating_blocked_by = object()

    _learn_disturbance_profile(shell, "room", diagnostics, MONDAY_NOON)

    assert shell.state_mgr.writes == 0


def test_a_second_sample_within_a_minute_is_not_counted_twice() -> None:
    shell = _fake_shell()

    _learn_disturbance_profile(shell, "room", _diagnostics(), MONDAY_NOON)
    _learn_disturbance_profile(shell, "room", _diagnostics(), MONDAY_NOON)

    assert shell.state_mgr.writes == 1
