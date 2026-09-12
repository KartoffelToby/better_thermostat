"""Per-file budget for silenced PEP 8 naming findings: freeze it, refuse growth.

Ruff's `N` rules read the case and shape of a name: a class that is not
CapWords (N801), a function or argument or local or class attribute that is not
lowercase (N802, N803, N806, N815), a module whose file name is not a valid
identifier (N999). `pyproject.toml` silences some of them for the control-theory
notation under `utils/calibration/` and its test mirrors, and N999 for the
device model strings that name the modules under `model_fixes/`.

A `per-file-ignores` glob is blunt twice over. It covers every file the glob
matches, including the ones that never tripped the rule and the ones written
tomorrow, and it covers the whole of each file, including the parts that carry
no notation at all. So the glob says where the convention may be dropped, and
this budget says how far it is actually dropped today: a file may not exceed the
number of silenced findings it carries now, and a file that is not in the budget
may not have a single one.

The budget is scaffolding, not an inventory. It only ever shrinks, and once the
last entry reaches zero the file is deleted and the check becomes "no silenced
naming finding anywhere".

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
    Count today's findings and exit non-zero when a file is over its budget, or
    when a file with no budget has a finding at all.

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
from pathlib import Path
import subprocess
import sys
import tomllib

REPO_ROOT = Path(__file__).resolve().parent.parent
BUDGET_FILE = REPO_ROOT / ".pep8-naming-budget.json"

RULE_FAMILY = "N"

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
    """Return the repository's Python files, as git records them."""
    listing = subprocess.run(
        ("git", "ls-files", "-z", "--", "*.py"),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if listing.returncode != 0:
        sys.exit(f"git could not list the Python files:\n{listing.stderr.strip()}")
    return [name for name in listing.stdout.split("\0") if name]


def _measure() -> dict[str, int]:
    """Return the number of silenced naming findings per file."""
    target = _target_version()
    command = [*RUFF_SCAN]
    if target is not None:
        command += ["--target-version", target]
    scan = subprocess.run(
        [*command, *_python_files()],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    # Ruff exits 1 when it found something and 2 or above when it could not
    # scan, which is the only case that says nothing about the code.
    if scan.returncode > 1:
        sys.exit(f"ruff could not scan the repository:\n{scan.stderr.strip()}")
    try:
        findings = json.loads(scan.stdout)
    except json.JSONDecodeError as err:
        sys.exit(f"ruff did not report JSON: {err}\n{scan.stderr.strip()}")

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

    Files that came in under budget are named too, with a prompt to re-record.
    Return 1 when any file is over or unbudgeted, 0 otherwise; exit outright
    when no budget has been recorded yet.
    """
    counts = _measure()
    budget = _load_budget()
    if not budget:
        sys.exit(f"no budget recorded — run '{Path(__file__).name} update' first")

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

    if not over and not unbudgeted:
        total = sum(counts.values())
        print(f"{total} silenced naming findings across {len(budget)} files, all held")
        return 0

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
