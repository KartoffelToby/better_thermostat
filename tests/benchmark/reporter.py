"""Console reporting for benchmark runs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
import statistics

from .metrics import MetricValues
from .scoring import DimensionScores, UserProfile, compute_scores


@dataclass(frozen=True)
class ScenarioResult:
    """Outcome of one (controller, scenario) benchmark run."""

    controller: str
    scenario: str
    metrics: MetricValues


def format_metric(val: float, decimals: int = 2) -> str:
    """Format a metric value for human-readable output."""
    if math.isinf(val):
        return "  inf"
    if math.isnan(val):
        return "  NaN"
    return f"{val:.{decimals}f}"


# -----------------------------------------------------------------------------
# Score-matrix reporting (continuous, oracle-normalised)
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoredResult:
    """A :class:`ScenarioResult` enriched with oracle-relative scores.

    ``block_label`` carries the plant-override block the result came
    from; the same scenario name can exist under several blocks, so the
    pair (``block_label``, ``scenario``) is the full scenario identity.
    """

    controller: str
    scenario: str
    metrics: MetricValues
    scores: DimensionScores
    block_label: str = ""


def score_results(
    results: Mapping[str, list[ScenarioResult]],
    oracle_per_scenario: Mapping[tuple[str, str], MetricValues],
    profile: UserProfile,
) -> list[ScoredResult]:
    """Compute per-(controller, scenario) scores against the Oracle baseline.

    ``oracle_per_scenario`` is keyed by ``(block_label, scenario_name)`` so
    the same physical scenario evaluated under multiple plant overrides
    gets its own Oracle reference.
    """
    out: list[ScoredResult] = []
    for label, block in results.items():
        for r in block:
            oracle_m = oracle_per_scenario.get((label, r.scenario))
            if oracle_m is None:
                continue
            s = compute_scores(r.metrics, oracle_m, profile)
            out.append(
                ScoredResult(
                    controller=r.controller,
                    scenario=r.scenario,
                    metrics=r.metrics,
                    scores=s,
                    block_label=label,
                )
            )
    return out


def _mean(values: list[float]) -> float:
    finite = [v for v in values if not math.isnan(v)]
    return statistics.mean(finite) if finite else 0.0


def _stdev(values: list[float]) -> float:
    """Compute population stdev; return 0 for fewer than two finite samples."""
    finite = [v for v in values if not math.isnan(v)]
    return statistics.pstdev(finite) if len(finite) >= 2 else 0.0


def render_score_matrix(scored: list[ScoredResult], profile: UserProfile) -> str:
    """Per-controller mean (comfort, actuator, energy, overall) ranking.

    Each metric column is followed by a ``σ`` column — the population
    standard deviation of that metric across the controller's scenarios.
    High σ means the mean hides big per-scenario swings (one bad
    scenario dragging the row down, or vice versa).
    """
    by_ctrl: dict[str, list[ScoredResult]] = {}
    for sr in scored:
        by_ctrl.setdefault(sr.controller, []).append(sr)

    rows: list[
        tuple[str, float, float, float, float, float, float, float, float, int]
    ] = []
    for ctrl, items in by_ctrl.items():
        overalls = [i.scores.overall for i in items]
        comforts = [i.scores.comfort for i in items]
        actuators = [i.scores.actuator for i in items]
        energies = [i.scores.energy for i in items]
        rows.append(
            (
                ctrl,
                _mean(overalls),
                _stdev(overalls),
                _mean(comforts),
                _stdev(comforts),
                _mean(actuators),
                _stdev(actuators),
                _mean(energies),
                _stdev(energies),
                len(items),
            )
        )
    rows.sort(key=lambda r: -r[1])

    lines: list[str] = []
    width = 92
    lines.append("=" * width)
    lines.append(
        f"Score matrix — user profile: {profile.name} "
        f"(w_c={profile.w_comfort}, w_a={profile.w_actuator}, w_e={profile.w_energy})"
    )
    lines.append("Scores are 0..1, oracle-normalised; 1.0 = oracle-equivalent.")
    lines.append(
        "σ = population stdev across scenarios (lower = steadier per dimension)."
    )
    lines.append("=" * width)
    lines.append(
        f"  {'controller':<18}"
        f"{'overall':>9}{'σ':>7}"
        f"{'comfort':>9}{'σ':>7}"
        f"{'actuator':>10}{'σ':>7}"
        f"{'energy':>9}{'σ':>7}"
        f"{'n':>5}"
    )
    lines.append("  " + "-" * (width - 4))
    for ctrl, ov, ov_s, c, c_s, a, a_s, e, e_s, n in rows:
        marker = " *" if (rows and ctrl == rows[0][0]) else "  "
        lines.append(
            f"{marker}{ctrl:<18}"
            f"{ov:>9.3f}{ov_s:>7.3f}"
            f"{c:>9.3f}{c_s:>7.3f}"
            f"{a:>10.3f}{a_s:>7.3f}"
            f"{e:>9.3f}{e_s:>7.3f}"
            f"{n:>5d}"
        )
    lines.append("=" * width)
    return "\n".join(lines)


def render_plant_sweep(
    scored_per_plant: Mapping[str, list[ScoredResult]], profile: UserProfile
) -> str:
    """Cross-plant overall-score matrix — one column per plant.

    Each cell is the controller's mean overall score on that plant.
    Rows are sorted by the mean across all plants.
    """
    plant_names = list(scored_per_plant.keys())
    controllers: list[str] = []
    seen: set[str] = set()
    for label in plant_names:
        for sr in scored_per_plant[label]:
            if sr.controller not in seen:
                controllers.append(sr.controller)
                seen.add(sr.controller)

    # mean(overall) per (controller, plant)
    cell: dict[tuple[str, str], float] = {}
    for label, items in scored_per_plant.items():
        per_ctrl: dict[str, list[float]] = {}
        for sr in items:
            per_ctrl.setdefault(sr.controller, []).append(sr.scores.overall)
        for ctrl, vs in per_ctrl.items():
            cell[(ctrl, label)] = _mean(vs)

    # mean + cross-plant stdev + plant coverage. Controllers with results
    # on only a subset of plants must not outrank full-sweep controllers
    # on an inflated mean, so coverage sorts first.
    cross_mean: dict[str, float] = {}
    cross_std: dict[str, float] = {}
    cross_cov: dict[str, int] = {}
    for ctrl in controllers:
        vals = [cell.get((ctrl, p), float("nan")) for p in plant_names]
        cross_cov[ctrl] = sum(1 for v in vals if not math.isnan(v))
        cross_mean[ctrl] = _mean(vals)
        cross_std[ctrl] = _stdev(vals)
    controllers.sort(key=lambda c: (-cross_cov[c], -cross_mean[c]))

    lines: list[str] = []
    lines.append("")
    header_cells = "".join(f"{p[:13]:>14}" for p in plant_names)
    lines.append(
        f"Cross-plant overall — profile: {profile.name}  "
        f"({len(plant_names)} plants × {len({sr.scenario for items in scored_per_plant.values() for sr in items})} scenarios)"
    )
    lines.append(
        "±σ = population stdev of mean overall across plants. "
        "cov = plants with results; incomplete rows rank below complete ones."
    )
    header = f"  {'controller':<18}{header_cells}{'mean':>10}{'±σ':>8}{'cov':>8}"
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))
    for ctrl in controllers:
        row = f"  {ctrl:<18}"
        for p in plant_names:
            v = cell.get((ctrl, p), float("nan"))
            row += f"{v:>14.3f}"
        cov = f"{cross_cov[ctrl]}/{len(plant_names)}"
        row += f"{cross_mean[ctrl]:>10.3f}{cross_std[ctrl]:>8.3f}{cov:>8}"
        lines.append(row)
    return "\n".join(lines)


def render_per_scenario(scored: list[ScoredResult], profile: UserProfile) -> str:
    """Render the controller × scenario matrix (one row per scenario, overall score per cell).

    When the results span more than one block label, rows are keyed by
    ``scenario [block]`` so same-named scenarios from different plant
    overrides do not overwrite each other.
    """
    labels = {sr.block_label for sr in scored}
    qualify = len(labels) > 1
    by_scen: dict[str, dict[str, float]] = {}
    controllers: list[str] = []
    seen: set[str] = set()
    for sr in scored:
        row_key = f"{sr.scenario} [{sr.block_label}]" if qualify else sr.scenario
        by_scen.setdefault(row_key, {})[sr.controller] = sr.scores.overall
        if sr.controller not in seen:
            controllers.append(sr.controller)
            seen.add(sr.controller)

    lines: list[str] = []
    lines.append("")
    lines.append(f"Per-scenario overall scores — profile: {profile.name}")
    header = f"  {'scenario':<34}" + "".join(f"{c[:9]:>10}" for c in controllers)
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))
    for scen in sorted(by_scen):
        cells = "".join(
            f"{by_scen[scen].get(c, float('nan')):>10.3f}" for c in controllers
        )
        lines.append(f"  {scen:<34}{cells}")
    return "\n".join(lines)
