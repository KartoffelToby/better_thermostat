"""Per-module coverage floors: freeze what is covered, refuse what drops.

A single project-wide threshold can be bought back by adding tests to a module
that is already well covered, which is where tests are easiest to write and
worth the least. Every user-visible bug this project has had came from a
sparsely covered edge instead. So the floor is per module: each one keeps at
least the coverage it has today, and no amount of work elsewhere pays for a
drop here.

Two modes:

``check``
    Compare a coverage JSON report against the stored floors and exit non-zero
    when any module fell below its own. Modules with no floor yet are listed
    and do not fail — a new module has nothing to regress against.

``update``
    Rewrite the floors from a report. Run this after landing work that raises
    coverage, so the new level is the one that has to be held.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
FLOORS_FILE = REPO_ROOT / ".coverage-floors.json"

# Floors are stored to a tenth of a percent. The suite is deterministic, so a
# real drop is always larger than that; the rounding only absorbs the last
# binary digit.
PRECISION = 0.1


def _read_report(path: Path) -> dict[str, float]:
    """Return the covered percentage per module from a coverage JSON report."""
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.exit(f"no coverage report at {path} — run pytest with --cov-report=json")
    except json.JSONDecodeError as err:
        sys.exit(f"{path} is not a coverage JSON report: {err}")
    if "files" not in report:
        sys.exit(f"{path} is not a coverage JSON report: no 'files' key")
    return {
        _normalise(name): entry["summary"]["percent_covered"]
        for name, entry in report["files"].items()
    }


def _normalise(name: str) -> str:
    """Return a report path as a repository-relative POSIX path."""
    path = Path(name)
    if path.is_absolute():
        try:
            path = path.relative_to(REPO_ROOT)
        except ValueError:
            pass
    return path.as_posix()


def _floor(percent: float) -> float:
    """Round a measured percentage down to the stored precision."""
    return round(math.floor(round(percent / PRECISION, 6)) * PRECISION, 1)


def _load_floors() -> dict[str, float]:
    """Return the stored floors, or an empty mapping when there are none yet."""
    if not FLOORS_FILE.exists():
        return {}
    return json.loads(FLOORS_FILE.read_text(encoding="utf-8"))


def check(report_path: Path) -> int:
    """Report every module that fell below its floor. Return an exit code."""
    measured = _read_report(report_path)
    floors = _load_floors()
    if not floors:
        sys.exit(f"no floors recorded — run '{Path(__file__).name} update' first")

    regressions = [
        (module, floor, measured[module])
        for module, floor in sorted(floors.items())
        if module in measured and measured[module] + 1e-9 < floor
    ]
    gone = sorted(module for module in floors if module not in measured)
    new = sorted(module for module in measured if module not in floors)

    for module in new:
        print(f"no floor yet: {module} at {measured[module]:.1f}%")
    for module in gone:
        print(f"not in the report: {module} (floor {floors[module]:.1f}%)")

    if not regressions:
        print(f"all {len(floors)} modules hold their floor")
        return 0

    print("\ncoverage dropped below the recorded floor:")
    for module, floor, now in regressions:
        print(f"  {module}: {now:.1f}% < {floor:.1f}%")
    print(
        "\nAdd tests for what the change left uncovered, or — if the drop is "
        f"intended — re-record with '{Path(__file__).name} update'."
    )
    return 1


def update(report_path: Path) -> int:
    """Rewrite the floors from a report. Return an exit code."""
    measured = _read_report(report_path)
    floors = {module: _floor(percent) for module, percent in sorted(measured.items())}
    previous = _load_floors()
    FLOORS_FILE.write_text(
        json.dumps(floors, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    lowered = sorted(
        module
        for module, floor in floors.items()
        if module in previous and floor < previous[module]
    )
    for module in lowered:
        print(f"lowered: {module} {previous[module]:.1f}% -> {floors[module]:.1f}%")
    print(f"recorded {len(floors)} floors in {FLOORS_FILE.name}")
    return 0


def main() -> int:
    """Parse arguments and run the requested mode."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("check", "update"))
    parser.add_argument(
        "--report",
        type=Path,
        default=REPO_ROOT / "coverage.json",
        help="coverage JSON report (default: coverage.json)",
    )
    args = parser.parse_args()
    return check(args.report) if args.mode == "check" else update(args.report)


if __name__ == "__main__":
    raise SystemExit(main())
