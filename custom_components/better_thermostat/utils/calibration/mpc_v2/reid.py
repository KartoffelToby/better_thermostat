"""Offline batch re-identification of the RC2 plant prior.

The controller's plant prior is static for the lifetime of a room. The
information needed to correct it lives in the few moments with real
excitation — the morning heat-up after a night setback and free cool-downs
with the valve closed; regulation at a constant setpoint carries almost
none. This module collects one sample per control cycle into a bounded
in-memory buffer, extracts those transients, fits ``tau_room_min`` and
``gain_heater`` by minimising the prediction error of a full RC2
simulation over the training segments, and validates the candidate on a
held-out segment. Only a fit that predicts the held-out transient
measurably better than the current prior is offered for adoption;
everything else leaves the prior untouched.

``tau_rad_min`` and ``coupling_rad_room`` stay at their defaults: with a
room-temperature output they are weakly identifiable, and across realistic
building profiles a fixed value is more robust than per-room derivation.

Everything here is pure CPU work with no HA dependencies. The caller runs
:func:`run_reid_fit` in an executor and applies an accepted result through
the state manager's bumpless adopt path.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
import math

from ..mpc_v2_internals.plant import PlantParams

TAU_ROOM_BOUNDS_MIN = (60.0, 2000.0)
GAIN_HEATER_BOUNDS = (0.5, 5.0)


@dataclass
class ReidSample:
    """One per-cycle observation of the room/TRV/valve state."""

    t_s: float
    T_room_C: float
    u_frac: float
    T_outdoor_C: float | None = None
    T_trv_C: float | None = None
    window_open: bool = False


@dataclass
class ReidBuffer:
    """Bounded, spacing-deduplicated sample buffer for one MPC key.

    Held in memory only — after a restart the buffer refills within a day,
    while the *result* of a fit is persisted separately. The spacing floor
    also collapses the per-TRV dispatches of a multi-TRV group (milliseconds
    apart) into one sample per control pass.
    """

    maxlen: int = 2016
    min_spacing_s: float = 60.0
    samples: list[ReidSample] = field(default_factory=list)

    def append(self, sample: ReidSample) -> bool:
        """Add a sample; returns ``False`` when deduped or non-finite."""
        for value in (sample.t_s, sample.T_room_C, sample.u_frac):
            if not math.isfinite(value):
                return False
        for optional in (sample.T_outdoor_C, sample.T_trv_C):
            if optional is not None and not math.isfinite(optional):
                return False
        if self.samples and sample.t_s - self.samples[-1].t_s < self.min_spacing_s:
            return False
        self.samples.append(sample)
        if len(self.samples) > self.maxlen:
            del self.samples[: len(self.samples) - self.maxlen]
        return True


@dataclass
class Segment:
    """A contiguous, informative transient cut from the sample buffer."""

    kind: str  # "heatup" | "cooldown"
    samples: list[ReidSample]

    @property
    def end_t_s(self) -> float:
        """Timestamp of the segment's last sample."""
        return self.samples[-1].t_s


@dataclass
class ReidConfig:
    """Thresholds for segment extraction, fitting, and validation."""

    # Segment extraction. A sample is "heating" above u_heating_frac and
    # "idle" below u_idle_frac; runs are cut at window-open samples, missing
    # outdoor readings, class changes, and sampling gaps.
    u_heating_frac: float = 0.5
    u_idle_frac: float = 0.05
    max_gap_s: float = 1800.0
    min_duration_s: float = 2400.0
    min_samples: int = 8
    min_heatup_rise_K: float = 0.8
    min_cooldown_drop_K: float = 0.3

    # Fit and validation. Acceptance needs both a relative win (so the fit
    # must clearly beat the prior) and an absolute one (so a near-perfect
    # prior is not replaced over discretisation noise).
    min_segments: int = 3
    min_improvement: float = 0.1
    min_improvement_K: float = 0.05
    substep_s: float = 150.0
    max_iterations: int = 200


@dataclass
class ReidOutcome:
    """Result of one batch fit attempt."""

    status: str  # "accepted" | "rejected" | "insufficient_data"
    tau_room_min: float | None = None
    gain_heater: float | None = None
    rmse_prior_K: float | None = None
    rmse_fit_K: float | None = None
    n_segments: int = 0
    n_samples: int = 0


def _classify(sample: ReidSample, cfg: ReidConfig) -> str:
    if sample.u_frac >= cfg.u_heating_frac:
        return "heating"
    if sample.u_frac <= cfg.u_idle_frac:
        return "idle"
    return "other"


def extract_segments(samples: list[ReidSample], cfg: ReidConfig) -> list[Segment]:
    """Cut the buffer into informative heat-up / cool-down transients.

    Runs are contiguous stretches of one activity class with the window
    closed, an outdoor reading present, and no sampling gap larger than
    ``max_gap_s``. A run qualifies as a segment when it is long enough and
    the room temperature actually moved: rose by ``min_heatup_rise_K``
    during heating, or fell by ``min_cooldown_drop_K`` while idle with the
    outdoors colder than the room (free cool-down, not solar gain).
    """
    segments: list[Segment] = []
    run: list[ReidSample] = []
    run_class = ""

    def _flush() -> None:
        nonlocal run
        if len(run) >= cfg.min_samples and (
            run[-1].t_s - run[0].t_s >= cfg.min_duration_s
        ):
            delta = run[-1].T_room_C - run[0].T_room_C
            if run_class == "heating" and delta >= cfg.min_heatup_rise_K:
                segments.append(Segment(kind="heatup", samples=list(run)))
            elif run_class == "idle" and -delta >= cfg.min_cooldown_drop_K:
                outdoor = [s.T_outdoor_C for s in run if s.T_outdoor_C is not None]
                if outdoor and (sum(outdoor) / len(outdoor)) < run[-1].T_room_C:
                    segments.append(Segment(kind="cooldown", samples=list(run)))
        run = []

    for sample in samples:
        if sample.window_open or sample.T_outdoor_C is None:
            _flush()
            run_class = ""
            continue
        sample_class = _classify(sample, cfg)
        gap_broken = bool(run) and sample.t_s - run[-1].t_s > cfg.max_gap_s
        if sample_class != run_class or gap_broken or sample_class == "other":
            _flush()
            run_class = sample_class
        if sample_class != "other":
            run.append(sample)
    _flush()
    return segments


def _simulate_room(
    params: PlantParams, segment: Segment, substep_s: float
) -> list[float]:
    """Simulate T_room over a segment; returns one value per sample.

    Plain-float forward Euler with sub-stepping so the radiator time
    constant stays resolved even when samples are many minutes apart.
    Inputs (valve fraction, outdoor) are held zero-order from the left
    sample of each interval. The initial radiator state is seeded from the
    TRV reading when available, otherwise from the room temperature.
    """
    first = segment.samples[0]
    T_room = first.T_room_C
    T_rad = first.T_trv_C if first.T_trv_C is not None else first.T_room_C
    out: list[float] = [T_room]
    for prev, cur in zip(segment.samples, segment.samples[1:]):
        dt_s = cur.t_s - prev.t_s
        n_sub = max(1, math.ceil(dt_s / substep_s))
        h_min = (dt_s / n_sub) / 60.0
        u = max(0.0, min(1.0, prev.u_frac))
        T_outdoor = prev.T_outdoor_C if prev.T_outdoor_C is not None else T_room
        for _ in range(n_sub):
            dT_rad = (
                params.gain_heater * u * (params.T_water_C - T_rad) - (T_rad - T_room)
            ) / params.tau_rad_min
            dT_room = (
                params.coupling_rad_room * (T_rad - T_room) - (T_room - T_outdoor)
            ) / params.tau_room_min
            T_room += dT_room * h_min
            T_rad += dT_rad * h_min
        out.append(T_room)
    return out


def _sse(params: PlantParams, segments: list[Segment], cfg: ReidConfig) -> float:
    total = 0.0
    for segment in segments:
        simulated = _simulate_room(params, segment, cfg.substep_s)
        for sim, sample in zip(simulated[1:], segment.samples[1:]):
            err = sim - sample.T_room_C
            total += err * err
    return total


def _rmse(params: PlantParams, segments: list[Segment], cfg: ReidConfig) -> float:
    n = sum(len(s.samples) - 1 for s in segments)
    if n <= 0:
        return math.inf
    return math.sqrt(_sse(params, segments, cfg) / n)


def _nelder_mead(
    objective: Callable[[list[float]], float],
    x0: list[float],
    step: float,
    max_iterations: int,
) -> list[float]:
    """Minimise ``objective`` from ``x0`` with a standard Nelder-Mead simplex.

    Deterministic and dependency-free; adequate for the smooth 2-parameter
    log-space landscape of the RC2 fit. Stops on iteration budget or when
    the simplex collapses below a fixed spread.
    """
    n = len(x0)
    simplex = [list(x0)]
    for i in range(n):
        vertex = list(x0)
        vertex[i] += step
        simplex.append(vertex)
    values = [objective(v) for v in simplex]

    for _ in range(max_iterations):
        order = sorted(range(len(simplex)), key=lambda i: values[i])
        simplex = [simplex[i] for i in order]
        values = [values[i] for i in order]
        if abs(values[-1] - values[0]) < 1e-12:
            break
        centroid = [sum(vertex[i] for vertex in simplex[:-1]) / n for i in range(n)]
        worst = simplex[-1]
        reflected = [c + (c - w) for c, w in zip(centroid, worst)]
        f_reflected = objective(reflected)
        if f_reflected < values[0]:
            expanded = [c + 2.0 * (c - w) for c, w in zip(centroid, worst)]
            f_expanded = objective(expanded)
            if f_expanded < f_reflected:
                simplex[-1], values[-1] = expanded, f_expanded
            else:
                simplex[-1], values[-1] = reflected, f_reflected
        elif f_reflected < values[-2]:
            simplex[-1], values[-1] = reflected, f_reflected
        else:
            contracted = [c + 0.5 * (w - c) for c, w in zip(centroid, worst)]
            f_contracted = objective(contracted)
            if f_contracted < values[-1]:
                simplex[-1], values[-1] = contracted, f_contracted
            else:
                best = simplex[0]
                simplex = [best] + [
                    [b + 0.5 * (v - b) for b, v in zip(best, vertex)]
                    for vertex in simplex[1:]
                ]
                values = [values[0]] + [objective(v) for v in simplex[1:]]
    order = sorted(range(len(simplex)), key=lambda i: values[i])
    return simplex[order[0]]


def _clamp(value: float, bounds: tuple[float, float]) -> float:
    return max(bounds[0], min(bounds[1], value))


def _params_from_x(x: list[float], prior: PlantParams) -> PlantParams:
    """Map log-space optimiser coordinates onto a bounded parameter set."""
    return replace(
        prior,
        tau_room_min=_clamp(math.exp(x[0]), TAU_ROOM_BOUNDS_MIN),
        gain_heater=_clamp(math.exp(x[1]), GAIN_HEATER_BOUNDS),
    )


def run_reid_fit(
    samples: list[ReidSample], prior: PlantParams, cfg: ReidConfig | None = None
) -> ReidOutcome:
    """Extract segments, fit tau_room/gain, and validate on a holdout.

    The holdout is the most recent segment; training needs at least one
    heat-up (the only place ``gain_heater`` is identifiable — cool-downs
    constrain only ``tau_room_min``). The candidate is accepted when its
    holdout RMSE beats the prior's by at least ``cfg.min_improvement``.
    """
    cfg = cfg or ReidConfig()
    segments = extract_segments(samples, cfg)
    n_samples = sum(len(s.samples) for s in segments)
    if len(segments) < cfg.min_segments:
        return ReidOutcome(
            status="insufficient_data", n_segments=len(segments), n_samples=n_samples
        )

    segments = sorted(segments, key=lambda s: s.end_t_s)
    train, holdout = segments[:-1], [segments[-1]]
    if not any(s.kind == "heatup" for s in train):
        return ReidOutcome(
            status="insufficient_data", n_segments=len(segments), n_samples=n_samples
        )

    def objective(x: list[float]) -> float:
        return _sse(_params_from_x(x, prior), train, cfg)

    x0 = [
        math.log(_clamp(prior.tau_room_min, TAU_ROOM_BOUNDS_MIN)),
        math.log(_clamp(prior.gain_heater, GAIN_HEATER_BOUNDS)),
    ]
    x_best = _nelder_mead(
        objective, x0, step=math.log(1.3), max_iterations=cfg.max_iterations
    )
    fitted = _params_from_x(x_best, prior)

    rmse_prior = _rmse(prior, holdout, cfg)
    rmse_fit = _rmse(fitted, holdout, cfg)
    accepted = (
        math.isfinite(rmse_fit)
        and math.isfinite(rmse_prior)
        and rmse_fit <= (1.0 - cfg.min_improvement) * rmse_prior
        and rmse_prior - rmse_fit >= cfg.min_improvement_K
    )
    return ReidOutcome(
        status="accepted" if accepted else "rejected",
        tau_room_min=fitted.tau_room_min,
        gain_heater=fitted.gain_heater,
        rmse_prior_K=rmse_prior,
        rmse_fit_K=rmse_fit,
        n_segments=len(segments),
        n_samples=n_samples,
    )
