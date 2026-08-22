"""Per-file budget for blind exception handlers: freeze the backlog, refuse growth.

Ruff's BLE001 flags an `except Exception` that neither re-raises nor logs the
exception with its traceback. A handler that calls `_LOGGER.exception(...)`, or
passes `exc_info=True`, is not flagged. So what this budget counts is broad
handlers that swallow the failure silently, not broad handlers as such.

Silencing the rule per file — which `pyproject.toml` does for the files that
carry the backlog — exempts the handlers written tomorrow along with the ones
written yesterday, and the file holding the largest backlog is exactly where
the next one lands. So the budget is per file and it counts: a file may not
exceed the number it has today, and a file that is not in the budget may not
have a single finding.

The count therefore does not come from `ruff check`. It comes from a scan that
runs with ruff's own configuration ignored, with inline `noqa` directives
overridden, and over the file list git reports rather than over a directory
walk. No `per-file-ignores` entry, no `exclude` entry, no `noqa` comment and no
`.gitignore` line moves the number: the budget file is the one place where a
blind handler is recorded.

Two modes:

``check``
    Count today's findings and exit non-zero when a file is over its budget, or
    when a file with no budget has a finding at all.

``update``
    Rewrite the budget from today's counts. Run this after converting handlers,
    so the lower number is the one that has to be held.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tomllib

REPO_ROOT = Path(__file__).resolve().parent.parent
BUDGET_FILE = REPO_ROOT / ".blind-except-budget.json"

RULE = "BLE001"

# `--isolated` drops every lint setting the repository carries, which is the
# point: `per-file-ignores`, `extend-per-file-ignores`, `ignore`, `exclude` and
# `extend-exclude` each silence BLE001, and the scan has to see past every one
# of them. `--ignore-noqa` does the same for the inline directives. The files
# are passed explicitly, so no directory walk and no `.gitignore` line decides
# what is looked at.
RUFF_SCAN = (
    sys.executable,
    "-m",
    "ruff",
    "check",
    "--isolated",
    "--select",
    RULE,
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
    """Return the number of blind exception handlers per file."""
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

    # Only BLE001 was selected, so anything else is a file ruff could not parse.
    # Such a file reports no findings, which would read as a count of zero.
    unparsed = sorted(
        {_normalise(f["filename"]) for f in findings if f["code"] != RULE}
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
    if not BUDGET_FILE.exists():
        return {}
    return json.loads(BUDGET_FILE.read_text(encoding="utf-8"))


def check() -> int:
    """Report every file over its budget. Return an exit code."""
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
        print(f"all {len(budget)} files stay within their budget")
        return 0

    if over:
        print("\nmore blind exception handlers than the budget allows:")
        for name, allowed, now in over:
            print(f"  {name}: {now} > {allowed}")
    if unbudgeted:
        print("\nblind exception handlers in files with no budget:")
        for name in unbudgeted:
            print(f"  {name}: {counts[name]}")
    print(
        "\nName the exceptions the handler expects, re-raise, or log the "
        "traceback with `_LOGGER.exception`. The budget only ever falls."
    )
    return 1


def update() -> int:
    """Rewrite the budget from today's counts. Return an exit code."""
    counts = _measure()
    previous = _load_budget()
    BUDGET_FILE.write_text(
        json.dumps(counts, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    raised = sorted(
        name for name, count in counts.items() if count > previous.get(name, 0)
    )
    for name in raised:
        print(f"raised: {name} {previous.get(name, 0)} -> {counts[name]}")
    print(f"recorded {len(counts)} files in {BUDGET_FILE.name}")
    return 0


def main() -> int:
    """Parse arguments and run the requested mode."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("check", "update"))
    args = parser.parse_args()
    return check() if args.mode == "check" else update()


if __name__ == "__main__":
    raise SystemExit(main())
