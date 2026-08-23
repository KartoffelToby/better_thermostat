"""Per-file budget for rejected names: freeze the backlog, refuse growth.

`glossary.toml` holds one term per domain concept together with the spellings
that term replaces. A name already used in five hundred places cannot be fixed
by a rule, and silencing the whole file would exempt the names written tomorrow
along with the ones written yesterday. So the backlog is a budget: a file may
not exceed the number of rejected names it carries today, and a file that is
not in the budget may not carry a single one.

The budget is scaffolding, not an inventory. It only ever shrinks, and once the
last entry reaches zero the file is deleted and the check becomes "no rejected
spelling anywhere".

Only identifiers are counted. Each file is parsed with :mod:`ast` and the names
are taken from the tree, never from strings, comments or docstrings. A
persisted configuration key that happens to spell a rejected alias lives in
zone B and moves only with a migration, not with a rename. Where a rejected
alias is the correct name after all, ``[[exception]]`` in `glossary.toml`
records it together with the reason.

Three modes:

``check``
    Count today's findings and exit non-zero when a file is over its budget, or
    when a file with no budget has a finding at all.

``update``
    Rewrite the budget from today's counts. Run this after a rename, so the
    lower number is the one that has to be held. It refuses to record a count
    that grew unless `--allow-raise` says so, which is for the two ways a count
    rises without anyone writing a rejected name: a file moved and took its
    backlog along, or the glossary gained a term and the tree already spelled
    it the old way.

``list``
    Print the findings themselves, for one path or for the whole tree.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
import sys
import tomllib

REPO_ROOT = Path(__file__).resolve().parent.parent
GLOSSARY_FILE = REPO_ROOT / "glossary.toml"
BUDGET_FILE = REPO_ROOT / ".naming-budget.json"
SCANNED = ("custom_components", "tests", "scripts")


@dataclass(frozen=True)
class Finding:
    """One identifier that spells a rejected alias."""

    path: str
    line: int
    alias: str
    replacements: tuple[str, ...]

    def __str__(self) -> str:
        """Return the finding as a ``file:line: message`` line."""
        use = " or ".join(f"`{name}`" for name in self.replacements)
        return f"{self.path}:{self.line}: `{self.alias}` is rejected, use {use}"


@dataclass(frozen=True)
class Glossary:
    """The parsed contents of `glossary.toml`."""

    aliases: dict[str, tuple[str, ...]]
    exceptions: dict[str, tuple[str, ...]]


def _load_glossary() -> Glossary:
    """Return the glossary, refusing a source that contradicts itself."""
    try:
        data = tomllib.loads(GLOSSARY_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.exit(f"no glossary at {GLOSSARY_FILE}")
    except tomllib.TOMLDecodeError as err:
        sys.exit(f"{GLOSSARY_FILE.name} is not valid TOML: {err}")

    terms = data.get("term", [])
    approved = {term["name"].rpartition(".")[2] for term in terms}

    aliases: dict[str, list[str]] = {}
    for term in terms:
        for alias in term.get("rejected", []):
            aliases.setdefault(alias, []).append(term["name"])

    both = sorted(set(aliases) & approved)
    if both:
        sys.exit(f"{GLOSSARY_FILE.name}: {', '.join(both)} is both a term and an alias")

    exceptions: dict[str, list[str]] = {}
    for entry in data.get("exception", []):
        if not entry.get("reason"):
            sys.exit(
                f"{GLOSSARY_FILE.name}: exception for `{entry['alias']}` has no reason"
            )
        exceptions.setdefault(entry["alias"], []).extend(entry["paths"])

    return Glossary(
        aliases={alias: tuple(names) for alias, names in aliases.items()},
        exceptions={alias: tuple(paths) for alias, paths in exceptions.items()},
    )


def _identifiers(tree: ast.AST) -> list[tuple[str, int]]:
    """Return every identifier the code defines or reads, with its line.

    Bindings count wherever they are made, not only where a name is read: an
    import alias, an `except ... as` clause and a match capture all introduce a
    name that a rename has to reach.
    """
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        match node:
            case ast.Name():
                found.append((node.id, node.lineno))
            case ast.Attribute():
                found.append((node.attr, node.lineno))
            case ast.arg():
                found.append((node.arg, node.lineno))
            case ast.keyword() if node.arg is not None:
                found.append((node.arg, node.lineno))
            case ast.FunctionDef() | ast.AsyncFunctionDef() | ast.ClassDef():
                found.append((node.name, node.lineno))
            case ast.alias():
                found.append((node.asname or node.name.split(".")[0], node.lineno))
            case ast.ExceptHandler() if node.name is not None:
                found.append((node.name, node.lineno))
            case ast.MatchAs() | ast.MatchStar() if node.name is not None:
                found.append((node.name, node.lineno))
            case ast.MatchMapping() if node.rest is not None:
                found.append((node.rest, node.lineno))
    return found


def _scan(path: Path, glossary: Glossary) -> list[Finding]:
    """Return every rejected alias used as an identifier in one file."""
    relative = path.relative_to(REPO_ROOT).as_posix()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    except SyntaxError as err:
        sys.exit(f"{relative}: {err}")

    seen: set[tuple[str, int]] = set()
    findings = []
    for name, line in _identifiers(tree):
        targets = glossary.aliases.get(name)
        if targets is None or (name, line) in seen:
            continue
        if any(relative.startswith(p) for p in glossary.exceptions.get(name, ())):
            continue
        seen.add((name, line))
        findings.append(Finding(relative, line, name, targets))
    return findings


def _sources(paths: list[Path] | None) -> list[Path]:
    """Return the Python files to scan, defaulting to the whole project."""
    roots = paths or [REPO_ROOT / name for name in SCANNED]
    files: list[Path] = []
    for root in roots:
        files.extend(sorted(root.rglob("*.py")) if root.is_dir() else [root])
    return files


def _findings(paths: list[Path] | None, glossary: Glossary) -> list[Finding]:
    """Return every finding across the requested paths."""
    return [f for path in _sources(paths) for f in _scan(path, glossary)]


def _load_budget() -> dict[str, int]:
    """Return the recorded budget, or an empty one before it is first written."""
    try:
        return json.loads(BUDGET_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as err:
        sys.exit(f"{BUDGET_FILE.name} is not valid JSON: {err}")


def check(paths: list[Path] | None) -> int:
    """Compare today's findings against the budget. Return an exit code."""
    glossary = _load_glossary()
    counts = Counter(f.path for f in _findings(paths, glossary))
    budget = _load_budget()

    over = sorted(
        (path, count, budget.get(path, 0))
        for path, count in counts.items()
        if count > budget.get(path, 0)
    )
    for path, count, allowed in over:
        for finding in _scan(REPO_ROOT / path, glossary):
            print(finding)
        print(f"over budget: {path} {count} rejected names, budget {allowed}\n")

    if over:
        print(f"{len(over)} file(s) over budget")
        return 1

    print(
        f"{sum(counts.values())} rejected names across {len(counts)} files, "
        "all within budget"
    )
    slack = sum(1 for path, count in budget.items() if count > counts.get(path, 0))
    if slack:
        print(
            f"{slack} file(s) below budget. Run `check_naming.py update` to record it."
        )
    return 0


def update(*, allow_raise: bool) -> int:
    """Rewrite the budget from today's counts. Return an exit code.

    Always scans the whole project: a budget written from a partial scan would
    drop the files it did not look at, which reads as progress and is not.
    """
    glossary = _load_glossary()
    counts = Counter(f.path for f in _findings(None, glossary))
    previous = _load_budget()
    budget = dict(sorted(counts.items()))

    raised = [
        (path, previous.get(path, 0), count)
        for path, count in budget.items()
        if count > previous.get(path, 0)
    ]
    for path, before, after in raised:
        print(f"raised: {path} {before} -> {after}")
    if raised and not allow_raise:
        print(
            f"\nrefusing to record {len(raised)} raised count(s). Fix the names, or "
            "pass --allow-raise after a file move or a new glossary term."
        )
        return 1

    BUDGET_FILE.write_text(
        json.dumps(budget, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    remaining = sum(budget.values())
    if not remaining:
        BUDGET_FILE.unlink()
        print(f"nothing left to hold — {BUDGET_FILE.name} removed")
        return 0
    print(
        f"recorded {len(budget)} files, {remaining} names left, in {BUDGET_FILE.name}"
    )
    return 0


def show(paths: list[Path] | None, *, statistics: bool) -> int:
    """Print the findings themselves. Return an exit code."""
    glossary = _load_glossary()
    findings = _findings(paths, glossary)
    if statistics:
        for alias, count in Counter(f.alias for f in findings).most_common():
            print(f"{count:5d}  {alias}")
    else:
        for finding in findings:
            print(finding)
    print(f"{len(findings)} rejected names")
    return 0


def main() -> int:
    """Parse arguments and run the requested mode."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("check", "update", "list"))
    parser.add_argument("paths", nargs="*", type=Path, help="files or directories")
    parser.add_argument(
        "--statistics", action="store_true", help="list mode: count per alias"
    )
    parser.add_argument(
        "--allow-raise",
        action="store_true",
        help="update mode: record a count that grew, after a move or a new term",
    )
    args = parser.parse_args()

    paths = [p.resolve() for p in args.paths] or None
    match args.mode:
        case "check":
            return check(paths)
        case "update":
            if paths:
                sys.exit("update always scans the whole project — drop the paths")
            return update(allow_raise=args.allow_raise)
        case _:
            return show(paths, statistics=args.statistics)


if __name__ == "__main__":
    raise SystemExit(main())
