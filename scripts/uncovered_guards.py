r"""List the guards the suite only ever takes one way.

A guard tested in one direction is a guard that can be inverted for free:
both of its lines run, line coverage is satisfied, and the case the
condition exists to exclude is never entered. Branch coverage names
them: coverage.py records, per conditional, which of its two exits no
test took. This script turns that record into a work list.

Read a coverage JSON report produced with ``branch = true``:

    uv run pytest tests --cov=custom_components/better_thermostat \
        --cov-report=json:coverage.json
    uv run python scripts/uncovered_guards.py

Output is one line per untaken direction, grouped by module and ordered
by how many a module has, with the source of the deciding line and where
the missing exit would have gone. A destination of ``exit`` means the
direction that leaves the enclosing function.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPORT = REPO_ROOT / "coverage.json"


def _read_report(path: Path) -> dict[str, list[list[int]]]:
    """Return the missing branch arcs per module from a coverage report."""
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.exit(f"no coverage report at {path}; run pytest with --cov-report=json")
    except json.JSONDecodeError as err:
        sys.exit(f"{path} is not a coverage JSON report: {err}")
    if "files" not in report:
        sys.exit(f"{path} is not a coverage JSON report: no 'files' key")
    missing = {
        name: entry["missing_branches"]
        for name, entry in report["files"].items()
        if entry.get("missing_branches")
    }
    if not missing and report["files"]:
        sys.exit(
            f"{path} records no branches; the report was produced without "
            "branch coverage; check 'branch = true' under [tool.coverage.run]"
        )
    return missing


def _source_lines(name: str) -> list[str]:
    """Return the module's source, or an empty list when it cannot be read."""
    path = Path(name)
    if not path.is_absolute():
        path = REPO_ROOT / path
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def _destination(target: int) -> str:
    """Name where an untaken exit would have gone."""
    return "exit" if target < 0 else f"line {target}"


def main() -> None:
    """Print the untaken branch directions, module by module."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT,
        help=f"coverage JSON report to read (default: {DEFAULT_REPORT})",
    )
    parser.add_argument(
        "--module", help="only report modules whose path contains this substring"
    )
    args = parser.parse_args()

    missing = _read_report(args.report)
    if args.module:
        missing = {n: m for n, m in missing.items() if args.module in n}

    total = 0
    for name, arcs in sorted(missing.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        source = _source_lines(name)
        print(f"\n{name}  ({len(arcs)} untaken)")
        for line_no, target in sorted(arcs):
            code = source[line_no - 1].strip() if line_no <= len(source) else ""
            print(f"  {line_no:>5}  -> {_destination(target):<10}  {code}")
            total += 1
    print(f"\n{total} untaken directions in {len(missing)} modules")


if __name__ == "__main__":
    main()
