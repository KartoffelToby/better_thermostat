"""Unit tests for the offline batch re-identification core (HA/daqp-free)."""

from __future__ import annotations

import math

from custom_components.better_thermostat.utils.calibration.mpc_v2.reid import (
    ReidBuffer,
    ReidConfig,
    ReidSample,
    Segment,
    _nelder_mead,
    _simulate_room,
    extract_segments,
    run_reid_fit,
)
from custom_components.better_thermostat.utils.calibration.mpc_v2_internals.plant import (
    PlantParams,
)


def _sample(t_s: float, T_room: float, u: float, **kw: object) -> ReidSample:
    """Build a ReidSample with sane defaults for outdoor/window."""
    return ReidSample(
        t_s=t_s,
        T_room_C=T_room,
        u_frac=u,
        T_outdoor_C=kw.get("outdoor", 5.0),
        T_trv_C=kw.get("trv"),
        window_open=bool(kw.get("window", False)),
    )


# -- Buffer -----------------------------------------------------------------


def test_buffer_dedupes_samples_closer_than_spacing() -> None:
    """Two samples milliseconds apart (multi-TRV pass) collapse into one."""
    buf = ReidBuffer(min_spacing_s=60.0)
    assert buf.append(_sample(1000.0, 20.0, 0.5)) is True
    assert buf.append(_sample(1000.005, 20.0, 0.5)) is False
    assert buf.append(_sample(1061.0, 20.1, 0.5)) is True
    assert len(buf.samples) == 2


def test_buffer_evicts_oldest_beyond_maxlen() -> None:
    """The buffer stays bounded and keeps the most recent samples."""
    buf = ReidBuffer(maxlen=5, min_spacing_s=1.0)
    for i in range(10):
        buf.append(_sample(float(i * 10), 20.0, 0.0))
    assert len(buf.samples) == 5
    assert buf.samples[0].t_s == 50.0


def test_buffer_rejects_non_finite_values() -> None:
    """NaN/Inf in any field must never reach a later fit."""
    buf = ReidBuffer()
    assert buf.append(_sample(0.0, float("nan"), 0.5)) is False
    assert buf.append(_sample(0.0, 20.0, float("inf"))) is False
    assert buf.append(_sample(0.0, 20.0, 0.5, outdoor=float("nan"))) is False
    assert buf.samples == []


# -- Segment extraction -----------------------------------------------------


def _stream(
    *phases: tuple[float, float, float, int],
    spacing_s: float = 300.0,
    window_at: int | None = None,
) -> list[ReidSample]:
    """Build a sample stream from (T_start, T_end, u, n) phases."""
    samples: list[ReidSample] = []
    t = 0.0
    idx = 0
    for T_start, T_end, u, n in phases:
        for i in range(n):
            T = T_start + (T_end - T_start) * (i / max(1, n - 1))
            samples.append(
                _sample(t, T, u, window=(window_at is not None and idx == window_at))
            )
            t += spacing_s
            idx += 1
    return samples


def test_extracts_heatup_and_cooldown() -> None:
    """A heating rise and an idle drop become one segment each."""
    samples = _stream((18.0, 21.0, 1.0, 12), (21.0, 19.5, 0.0, 16))
    segments = extract_segments(samples, ReidConfig())
    kinds = [s.kind for s in segments]
    assert kinds == ["heatup", "cooldown"]


def test_window_open_cuts_a_segment() -> None:
    """A window-open sample in mid-heatup splits the run below min length."""
    samples = _stream((18.0, 21.0, 1.0, 12), window_at=6)
    segments = extract_segments(samples, ReidConfig())
    assert segments == []


def test_sampling_gap_cuts_a_segment() -> None:
    """A gap beyond max_gap_s splits an otherwise valid run."""
    samples = _stream((18.0, 21.0, 1.0, 12))
    for s in samples[6:]:
        s.t_s += 3600.0
    segments = extract_segments(samples, ReidConfig())
    assert segments == []


def test_missing_outdoor_disqualifies_samples() -> None:
    """Samples without an outdoor reading cannot be simulated and are cut."""
    samples = _stream((18.0, 21.0, 1.0, 12))
    for s in samples:
        s.T_outdoor_C = None
    assert extract_segments(samples, ReidConfig()) == []


def test_flat_runs_are_not_segments() -> None:
    """Regulation at constant temperature carries no excitation."""
    samples = _stream((21.0, 21.1, 1.0, 12), (21.1, 21.0, 0.0, 16))
    assert extract_segments(samples, ReidConfig()) == []


def test_warm_cooldown_requires_colder_outdoors() -> None:
    """An idle temperature drop with warm outdoors (solar night?) is skipped."""
    samples = _stream((22.0, 20.0, 0.0, 16))
    for s in samples:
        s.T_outdoor_C = 25.0
    assert extract_segments(samples, ReidConfig()) == []


# -- Optimiser --------------------------------------------------------------


def test_nelder_mead_minimises_a_quadratic() -> None:
    """The simplex finds the minimum of a smooth 2-parameter bowl."""

    def bowl(x: list[float]) -> float:
        return (x[0] - 1.0) ** 2 + (x[1] + 2.0) ** 2

    best = _nelder_mead(bowl, [0.0, 0.0], step=0.5, max_iterations=200)
    assert abs(best[0] - 1.0) < 1e-3
    assert abs(best[1] + 2.0) < 1e-3


# -- Fit + validation -------------------------------------------------------

_TRUTH = PlantParams(tau_room_min=240.0, gain_heater=3.0)


def _generate_day(
    truth: PlantParams,
    noise: float = 0.01,
    spacing_s: float = 300.0,
    phases: list[tuple[float, float]] | None = None,
) -> list[ReidSample]:
    """Simulate a setback day (cool-down, heat-up, cool-down) from ``truth``.

    Plain Euler at a finer step than the fit's substep so the fit cannot
    trivially invert its own discretisation. TRV temperature is included so
    segment simulations seed the radiator state correctly. ``phases`` is a
    list of ``(valve_fraction, duration_s)`` tuples; the default models a
    single setback day ending in a cool-down.
    """
    T_room, T_rad = 21.0, 21.0
    T_outdoor = 5.0
    samples: list[ReidSample] = []
    t = 0.0
    if phases is None:
        phases = [(0.0, 4 * 3600.0), (1.0, 3 * 3600.0), (0.0, 4 * 3600.0)]
    step_s = 60.0
    i = 0
    for u, duration_s in phases:
        elapsed = 0.0
        while elapsed < duration_s:
            if elapsed % spacing_s == 0.0:
                wobble = noise * math.sin(i * 1.7)
                samples.append(
                    _sample(t, T_room + wobble, u, trv=T_rad, outdoor=T_outdoor)
                )
                i += 1
            h_min = step_s / 60.0
            dT_rad = (
                truth.gain_heater * u * (truth.T_water_C - T_rad) - (T_rad - T_room)
            ) / truth.tau_rad_min
            dT_room = (
                truth.coupling_rad_room * (T_rad - T_room) - (T_room - T_outdoor)
            ) / truth.tau_room_min
            T_room += dT_room * h_min
            T_rad += dT_rad * h_min
            t += step_s
            elapsed += step_s
    return samples


def test_fit_recovers_truth_params_and_is_accepted() -> None:
    """On data from a plant far off the prior, the fit recovers and wins.

    The day ends with a heat-up so the holdout carries gain information
    and the recovered ``gain_heater`` is actually validated.
    """
    samples = _generate_day(
        _TRUTH,
        phases=[
            (0.0, 4 * 3600.0),
            (1.0, 3 * 3600.0),
            (0.0, 4 * 3600.0),
            (1.0, 3 * 3600.0),
        ],
    )
    prior = PlantParams()  # tau 480, gain 2.0 — both well off the truth
    outcome = run_reid_fit(samples, prior)
    assert outcome.status == "accepted"
    assert outcome.tau_room_min is not None and outcome.gain_heater is not None
    assert abs(outcome.tau_room_min - _TRUTH.tau_room_min) / _TRUTH.tau_room_min < 0.25
    assert abs(outcome.gain_heater - _TRUTH.gain_heater) / _TRUTH.gain_heater < 0.25
    assert outcome.rmse_fit_K is not None and outcome.rmse_prior_K is not None
    assert outcome.rmse_fit_K < outcome.rmse_prior_K


def test_cooldown_holdout_does_not_adopt_unvalidated_gain() -> None:
    """A cool-down holdout cannot validate gain, so the prior gain is kept.

    With u ≈ 0 throughout the holdout, any ``gain_heater`` predicts the
    same trajectory — the candidate must not carry the (unvalidated)
    fitted gain, only the tau value.
    """
    samples = _generate_day(_TRUTH)  # default day ends with a cool-down
    prior = PlantParams()  # gain 2.0, well below the truth's 3.0
    outcome = run_reid_fit(samples, prior)
    assert outcome.gain_heater == prior.gain_heater
    assert outcome.tau_room_min is not None
    assert outcome.tau_room_min != prior.tau_room_min


def test_fit_rejected_when_prior_already_matches() -> None:
    """Data generated by the prior itself leaves nothing to improve."""
    prior = PlantParams()
    samples = _generate_day(prior)
    outcome = run_reid_fit(samples, prior)
    assert outcome.status == "rejected"


def test_insufficient_without_a_heatup_in_training() -> None:
    """Cool-downs alone cannot identify the heater gain."""
    samples = _generate_day(_TRUTH)
    idle_only = [s for s in samples if s.u_frac <= 0.05]
    outcome = run_reid_fit(idle_only, PlantParams())
    assert outcome.status == "insufficient_data"


def test_insufficient_with_too_few_segments() -> None:
    """Fewer segments than min_segments cannot be split into train/holdout."""
    samples = _stream((18.0, 21.0, 1.0, 12))
    outcome = run_reid_fit(samples, PlantParams())
    assert outcome.status == "insufficient_data"


def test_simulate_room_seeds_radiator_from_trv() -> None:
    """The initial radiator state comes from the TRV reading when present."""
    seg = Segment(
        kind="cooldown",
        samples=[
            _sample(0.0, 21.0, 0.0, trv=40.0),
            _sample(300.0, 21.0, 0.0, trv=38.0),
        ],
    )
    with_trv = _simulate_room(PlantParams(), seg, substep_s=150.0)
    for s in seg.samples:
        s.T_trv_C = None
    without_trv = _simulate_room(PlantParams(), seg, substep_s=150.0)
    # A hot radiator keeps feeding the room; the seeded run must end warmer.
    assert with_trv[-1] > without_trv[-1]
