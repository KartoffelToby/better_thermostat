"""Per-file budget for silenced PEP 8 naming findings: freeze it, refuse growth.

Ruff's `N` rules read the case and shape of a name: a class that is not
CapWords (N801), a function or argument or local or class attribute that is not
lowercase (N802, N803, N806, N815), a module whose file name is not a valid
identifier (N999). Two deviations are the domain's own and are accepted by
name and place:

- the control-theory notation (`A`, `kalman_P`, `T_room`, `error_K`), whose
  shapes `extend-ignore-names` in `pyproject.toml` lists, inside
  ``NOTATION_ZONES`` only;
- N999 for the device model strings that name the modules under
  ``MODULE_NAME_ZONE``.

Ruff can apply a name pattern only everywhere and a `per-file-ignores` glob
only to a whole rule, so neither says "these names in these paths". The scan
here does: the zone files are scanned with the patterns, every other file
without them, so a `kalman_P` in `climate.py` is a finding although ruff itself
lets it through.

Every other silenced finding is a deviation the budget records per file: a file
may not exceed the number it carries now, and a file that is not in the budget
may not have a single one. The budget is scaffolding, not an inventory. It only
ever shrinks, and once the last entry reaches zero the file is deleted and the
check becomes "no silenced naming finding outside the accepted notation".

The count therefore does not come from `ruff check`. It comes from a scan that
runs with ruff's own configuration ignored, with inline `noqa` directives
overridden, and over the file list git reports rather than over a directory
walk. No `per-file-ignores` entry, no `exclude` entry, no `noqa` comment and no
`.gitignore` line moves the number: the budget file is the one place where a
silenced naming finding is recorded. A `# noqa: N815` on a single line and a
glob over a whole tree are worth the same here, which is what lets the narrower
form be chosen on its merits.

Two modes:

``check``
    Count today's findings and exit non-zero when a file is over its budget,
    when a file with no budget has a finding at all, or when a file came in
    under its budget and the lower number has not been recorded yet.

``update``
    Rewrite the budget from today's counts. Run this after a rename, so the
    lower number is the one that has to be held. It refuses to record a count
    that grew unless `--allow-raise` says so, which is for the way a count rises
    without anyone writing a new deviation: a file moved and took its notation
    along.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tomllib
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
BUDGET_FILE = REPO_ROOT / ".pep8-naming-budget.json"

RULE_FAMILY = "N"

# The paths where the control-theory notation is accepted: the calibration code
# and the tests that mirror it.
NOTATION_ZONES = (
    "custom_components/better_thermostat/utils/calibration/**",
    "tests/benchmark/**",
    "tests/unit/mpc_v2/**",
    "tests/unit/test_mpc_comprehensive.py",
)

# The modules named after the device model strings `load_model_quirks` imports.
MODULE_NAME_ZONE = "custom_components/better_thermostat/model_fixes/**"
MODULE_NAME_RULE = "N999"

# `--isolated` drops every lint setting the repository carries, which is the
# point: `per-file-ignores`, `extend-per-file-ignores`, `ignore`, `exclude` and
# `extend-exclude` each silence a naming rule, and the scan has to see past
# every one of them. `--ignore-noqa` does the same for the inline directives.
# The files are passed explicitly, so no directory walk and no `.gitignore` line
# decides what is looked at.
RUFF_SCAN = (
    sys.executable,
    "-m",
    "ruff",
    "check",
    "--isolated",
    "--select",
    RULE_FAMILY,
    "--ignore-noqa",
    "--output-format",
    "json",
)


def _normalise(name: str) -> str:
    """Return a ruff filename as a repository-relative POSIX path."""
    path = Path(name)
    if path.is_absolute():
        try:
            path = path.relative_to(REPO_ROOT)
        except ValueError:
            pass
    return path.as_posix()


def _target_version() -> str | None:
    """Return the Python version ruff parses against, or None when unset.

    An isolated scan has no `target-version`, and a grammar older than the
    sources turns a file into a syntax error, which reports no findings at all.
    The setting selects a grammar and cannot silence a rule, so reading this one
    value back out of `pyproject.toml` does not reopen what `--isolated` closes.
    """
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = config.get("tool", {}).get("ruff", {}).get("target-version")
    return str(version) if version else None


def _python_files() -> list[str]:
    """Return the repository's Python files, added to git or not yet.

    A file nobody has run ``git add`` on is part of the next commit all the
    same, so it counts here as it will in CI. The ignore rules decide what
    belongs to the repository.
    """
    listing = subprocess.run(
        (
            "git",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            "*.py",
        ),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if listing.returncode != 0:
        sys.exit(f"git could not list the Python files:\n{listing.stderr.strip()}")
    return [name for name in listing.stdout.split("\0") if name]


def _in_zone(name: str, *zones: str) -> bool:
    """Return whether a repository-relative path lies in one of *zones*."""
    path = PurePosixPath(name)
    return any(path.full_match(zone) for zone in zones)


def _notation_patterns() -> list[str]:
    """Return the name patterns `pyproject.toml` lists as notation.

    They are the one lint setting the scan reads back, and it applies them to
    ``NOTATION_ZONES`` alone: a pattern widens what the notation zones accept
    and nothing else.
    """
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    naming = config.get("tool", {}).get("ruff", {}).get("lint", {})
    patterns = naming.get("pep8-naming", {}).get("extend-ignore-names", [])
    if not isinstance(patterns, list) or not all(
        isinstance(pattern, str) for pattern in patterns
    ):
        sys.exit("pyproject.toml: extend-ignore-names is no list of patterns")
    return patterns


def _scan(files: list[str], *extra: str) -> list[dict[str, Any]]:
    """Return ruff's naming findings for *files*, exiting when it cannot scan."""
    if not files:
        return []
    command = [*RUFF_SCAN, *extra]
    target = _target_version()
    if target is not None:
        command += ["--target-version", target]
    scan = subprocess.run(
        [*command, *files], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    # Ruff exits 1 when it found something and 2 or above when it could not
    # scan, which is the only case that says nothing about the code.
    if scan.returncode > 1:
        sys.exit(f"ruff could not scan the repository:\n{scan.stderr.strip()}")
    try:
        return json.loads(scan.stdout)
    except json.JSONDecodeError as err:
        sys.exit(f"ruff did not report JSON: {err}\n{scan.stderr.strip()}")


def _measure() -> dict[str, int]:
    """Return the number of silenced naming findings per file.

    The notation zones are scanned with the notation patterns, every other
    file without them, and N999 is accepted under ``MODULE_NAME_ZONE`` alone.
    """
    files = _python_files()
    zoned = [name for name in files if _in_zone(name, *NOTATION_ZONES)]
    plain = [name for name in files if not _in_zone(name, *NOTATION_ZONES)]
    patterns = json.dumps(_notation_patterns())
    findings = [
        *_scan(plain),
        *_scan(zoned, "--config", f"lint.pep8-naming.extend-ignore-names={patterns}"),
    ]

    # Only the naming rules were selected, so anything else is a file ruff could
    # not parse. Such a file reports no naming findings, which would read as a
    # count of zero.
    unparsed = sorted(
        {
            _normalise(finding["filename"])
            for finding in findings
            if not (finding["code"] or "").startswith(RULE_FAMILY)
        }
    )
    if unparsed:
        sys.exit(
            "ruff could not parse, so it counted nothing in: " + ", ".join(unparsed)
        )

    counts: dict[str, int] = {}
    for finding in findings:
        name = _normalise(finding["filename"])
        if finding["code"] == MODULE_NAME_RULE and _in_zone(name, MODULE_NAME_ZONE):
            continue
        counts[name] = counts.get(name, 0) + 1
    return counts


def _load_budget() -> dict[str, int]:
    """Return the stored budget, or an empty mapping when there is none yet."""
    try:
        return json.loads(BUDGET_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as err:
        sys.exit(f"{BUDGET_FILE.name} is not valid JSON: {err}")


def check() -> int:
    """Report the files over budget and the files with no budget at all.

    Files that came in under budget fail too, with a prompt to re-record: the
    lower number is the one that has to be held from then on. With no budget
    recorded, every file is unbudgeted. Return 1 when any file is over, under or
    unbudgeted, 0 otherwise.
    """
    counts = _measure()
    budget = _load_budget()

    over = [
        (name, budget[name], counts[name])
        for name in sorted(counts)
        if name in budget and counts[name] > budget[name]
    ]
    unbudgeted = sorted(name for name in counts if name not in budget)
    improved = [
        (name, allowed, counts.get(name, 0))
        for name, allowed in sorted(budget.items())
        if counts.get(name, 0) < allowed
    ]

    for name, allowed, now in improved:
        print(f"below budget: {name} at {now} of {allowed}")
    if improved:
        print(
            f"re-record with '{Path(__file__).name} update' to hold the lower numbers"
        )

    if not over and not unbudgeted and not improved:
        total = sum(counts.values())
        if not total:
            print("no silenced naming finding outside the accepted notation")
            return 0
        print(f"{total} silenced naming findings across {len(budget)} files, all held")
        return 0

    if not over and not unbudgeted:
        return 1

    if over:
        print("\nmore silenced naming findings than the budget allows:")
        for name, allowed, now in over:
            print(f"  {name}: {now} > {allowed}")
    if unbudgeted:
        print("\nsilenced naming findings in files with no budget:")
        for name in unbudgeted:
            print(f"  {name}: {counts[name]}")
    print(
        "\nSpell the name the way PEP 8 does, or, where the notation is the "
        "domain's own, say so in the pull request. The budget only ever falls."
    )
    return 1


def update(*, allow_raise: bool) -> int:
    """Rewrite the budget from today's counts. Return an exit code."""
    counts = _measure()
    previous = _load_budget()
    budget = dict(sorted(counts.items()))

    raised = [
        (name, previous.get(name, 0), count)
        for name, count in budget.items()
        if count > previous.get(name, 0)
    ]
    for name, before, after in raised:
        print(f"raised: {name} {before} -> {after}")
    if raised and not allow_raise:
        print(
            f"\nrefusing to record {len(raised)} raised count(s). Fix the names, or "
            "pass --allow-raise after a file move."
        )
        return 1

    remaining = sum(budget.values())
    if not remaining:
        BUDGET_FILE.unlink(missing_ok=True)
        print(f"nothing left to hold — {BUDGET_FILE.name} removed")
        return 0
    BUDGET_FILE.write_text(
        json.dumps(budget, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        f"recorded {len(budget)} files, {remaining} findings left, in "
        f"{BUDGET_FILE.name}"
    )
    return 0


def main() -> int:
    """Parse arguments and run the requested mode."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("check", "update"))
    parser.add_argument(
        "--allow-raise",
        action="store_true",
        help="update mode: record a count that grew, after a file move",
    )
    args = parser.parse_args()
    return check() if args.mode == "check" else update(allow_raise=args.allow_raise)


if __name__ == "__main__":
    raise SystemExit(main())
