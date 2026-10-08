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

Besides whole names, `glossary.toml` rejects word parts. A `[[word]]` entry
rejects a word wherever it stands in a name (`temp` in `trv_temp`), everywhere.
A `[[modifier]]` entry with `rejected` spellings rejects a unit suffix at the
end of a name (`_s` in `delay_s`); a suffix after `per` is the denominator of a
rate and stays (`_kelvin_per_min`). Unit suffixes are the SI symbols the
control-theory notation writes, so they are not judged under the
`notation-paths` that `pyproject.toml` declares, and a name with one that the
notation spells is not charged to the code that reads it elsewhere, such as a
caller naming a keyword parameter there.

Only identifiers are counted. Each file is parsed with :mod:`ast` and the names
are taken from the tree, never from strings, comments or docstrings. A leading
underscore does not hide a spelling: `_offset` counts as `offset`. A
persisted configuration key that happens to spell a rejected alias lives in
zone B and moves only with a migration, not with a rename. Where a rejected
alias is the correct name after all, ``[[exception]]`` in `glossary.toml`
records it together with the reason.

Under `tests` a rejected spelling counts only once production has stopped using
it. A test names the attribute it asserts on, so the spelling it carries is the
production one and not a naming decision of its own; charging both files for
one rename would make the backlog look larger than it is and would leave a new
test file no room to name the field it covers. The coupling is what keeps the
tests honest: the moment the last production site is renamed, every test still
spelling the old name is over budget and has to follow.

Three modes:

``check``
    Count today's findings and exit non-zero when a file is over its budget,
    when a file with no budget has a finding at all, or when a file came in
    under its budget and the lower number has not been recorded yet.

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
from dataclasses import dataclass, field
import json
from pathlib import Path, PurePosixPath
import sys
import tomllib

REPO_ROOT = Path(__file__).resolve().parent.parent
GLOSSARY_FILE = REPO_ROOT / "glossary.toml"
PYPROJECT_FILE = REPO_ROOT / "pyproject.toml"
BUDGET_FILE = REPO_ROOT / ".naming-budget.json"
PRODUCTION_ROOT = "custom_components"
TEST_ROOT = "tests"
SCANNED = (PRODUCTION_ROOT, TEST_ROOT, "scripts")


@dataclass(frozen=True)
class Finding:
    """One identifier that spells a rejected alias."""

    path: str
    line: int
    alias: str
    replacements: tuple[str, ...]
    part: str | None = None

    def __str__(self) -> str:
        """Return the finding as a ``file:line: message`` line."""
        use = " or ".join(f"`{name}`" for name in self.replacements)
        if self.part is not None:
            return (
                f"{self.path}:{self.line}: `{self.alias}` spells `{self.part}`, "
                f"use {use}"
            )
        return f"{self.path}:{self.line}: `{self.alias}` is rejected, use {use}"


@dataclass(frozen=True)
class Glossary:
    """The parsed contents of `glossary.toml`."""

    aliases: dict[str, tuple[str, ...]]
    exceptions: dict[str, tuple[str, ...]]
    words: dict[str, str] = field(default_factory=dict)
    suffixes: dict[str, str] = field(default_factory=dict)
    notation_paths: tuple[str, ...] = ()


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

    words: dict[str, str] = {}
    for entry in data.get("word", []):
        for spelling in entry.get("rejected", []):
            words[spelling] = entry["name"]
    suffixes: dict[str, str] = {}
    for entry in data.get("modifier", []):
        for spelling in entry.get("rejected", []):
            if not spelling.startswith("_"):
                sys.exit(
                    f"{GLOSSARY_FILE.name}: modifier {entry['name']} rejects "
                    f"`{spelling}`, which is no suffix"
                )
            suffixes[spelling.removeprefix("_")] = entry["name"]

    exceptions: dict[str, list[str]] = {}
    for entry in data.get("exception", []):
        if not entry.get("reason"):
            sys.exit(
                f"{GLOSSARY_FILE.name}: exception for `{entry['alias']}` has no reason"
            )
        exceptions.setdefault(entry["alias"], []).extend(entry["paths"])

    inert = sorted(
        alias
        for alias in exceptions
        if alias not in aliases and not _spelled_parts(alias, words, suffixes)
    )
    if inert:
        sys.exit(
            f"{GLOSSARY_FILE.name}: no term rejects {', '.join(inert)}, "
            "so its exception excuses nothing"
        )

    return Glossary(
        aliases={alias: tuple(names) for alias, names in aliases.items()},
        exceptions={alias: tuple(paths) for alias, paths in exceptions.items()},
        words=words,
        suffixes=suffixes,
        notation_paths=_notation_paths(),
    )


def _notation_paths() -> tuple[str, ...]:
    """Return the paths `pyproject.toml` declares for the control-theory notation.

    No `pyproject.toml`, or none that declares them, means no such paths.
    """
    try:
        config = tomllib.loads(PYPROJECT_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ()
    table = config.get("tool", {}).get("better_thermostat", {}).get("pep8-naming", {})
    paths = table.get("notation-paths", [])
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        sys.exit(f"{PYPROJECT_FILE.name}: notation-paths is no list of strings")
    return tuple(paths)


def _spelled_words(name: str, words: dict[str, str]) -> list[tuple[str, str]]:
    """Return each rejected word *name* spells, with its replacement.

    A name is read as the words between its underscores, and a rejected word
    counts wherever it stands. The match is case-sensitive, which leaves an
    upper-case constant alone: a constant follows the persisted string it names.
    """
    return [
        (token, words[token]) for token in name.lstrip("_").split("_") if token in words
    ]


def _spelled_suffix(name: str, suffixes: dict[str, str]) -> tuple[str, str] | None:
    """Return the rejected unit suffix *name* ends in, with its replacement.

    The suffix is the last word of a name that has more than one, and not one
    after `per`, where it is the denominator of a rate. The match is
    case-sensitive, as for words.
    """
    tokens = name.lstrip("_").split("_")
    last = tokens[-1]
    if len(tokens) < 2 or last not in suffixes or tokens[-2] == "per":
        return None
    return f"_{last}", suffixes[last]


def _spelled_parts(
    name: str, words: dict[str, str], suffixes: dict[str, str]
) -> list[tuple[str, str]]:
    """Return every rejected word and unit suffix *name* spells."""
    suffix = _spelled_suffix(name, suffixes)
    return [*_spelled_words(name, words), *([suffix] if suffix else [])]


def _identifiers(tree: ast.AST) -> list[tuple[str, int]]:
    """Return every identifier the code defines or reads, with its line.

    Bindings count wherever they are made, not only where a name is read: an
    import alias, an `except ... as` clause, a match capture, a `global` or
    `nonlocal` declaration and a type parameter all introduce a name that a
    rename has to reach.
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
            case ast.Global() | ast.Nonlocal():
                found.extend((name, node.lineno) for name in node.names)
            case ast.TypeVar() | ast.ParamSpec() | ast.TypeVarTuple():
                found.append((node.name, node.lineno))
    return found


def _parse(path: Path) -> ast.Module:
    """Return the parsed file, stopping on one the parser cannot read."""
    relative = path.relative_to(REPO_ROOT).as_posix()
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    except SyntaxError as err:
        sys.exit(f"{relative}: {err}")


def _alias(name: str, glossary: Glossary) -> str | None:
    """Return the rejected alias a name spells, with or without leading underscores.

    A private `_offset` carries the same word as `offset`, so the underscore
    does not hide it. An alias the glossary lists with its underscore, such as
    `_real_trv`, matches as written.
    """
    if name in glossary.aliases:
        return name
    stripped = name.lstrip("_")
    return stripped if stripped in glossary.aliases else None


def _in_notation(relative: str, glossary: Glossary) -> bool:
    """Return whether a repository-relative path writes the control notation."""
    path = PurePosixPath(relative)
    return any(path.full_match(glob) for glob in glossary.notation_paths)


def _production_spellings(glossary: Glossary) -> frozenset[str]:
    """Return the rejected aliases the production tree still spells.

    These are the names a test cannot avoid: to assert on a field, it has to
    spell the name production gave that field, rejected or not. Such a use is
    a reader of a name someone else chose, so it is not counted while that
    name exists, and it becomes a finding as soon as the production spelling
    is gone. A name with a rejected word part is listed whole, without its
    leading underscores.
    """
    spellings: set[str] = set()
    for path in sorted((REPO_ROOT / PRODUCTION_ROOT).rglob("*.py")):
        for name, _ in _identifiers(_parse(path)):
            if (alias := _alias(name, glossary)) is not None:
                spellings.add(alias)
            elif _spelled_parts(name, glossary.words, glossary.suffixes):
                spellings.add(name.lstrip("_"))
    return frozenset(spellings)


def _notation_spellings(glossary: Glossary) -> frozenset[str]:
    """Return the names with a rejected unit suffix the notation paths spell.

    The notation chose them, so code elsewhere that reads one, a caller naming
    a keyword parameter for instance, carries no naming decision of its own.
    """
    spellings: set[str] = set()
    for root in SCANNED:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            if not _in_notation(path.relative_to(REPO_ROOT).as_posix(), glossary):
                continue
            for name, _ in _identifiers(_parse(path)):
                if _spelled_suffix(name, glossary.suffixes):
                    spellings.add(name.lstrip("_"))
    return frozenset(spellings)


def _scan(
    path: Path,
    glossary: Glossary,
    production: frozenset[str],
    notation: frozenset[str] = frozenset(),
) -> list[Finding]:
    """Return every rejected alias or word part used as an identifier in one file."""
    relative = path.relative_to(REPO_ROOT).as_posix()
    mirrors_production = relative.startswith(f"{TEST_ROOT}/")

    seen: set[tuple[str, int]] = set()
    findings = []
    for name, line in _identifiers(_parse(path)):
        if (name, line) in seen:
            continue
        alias = _alias(name, glossary)
        if alias is not None:
            if mirrors_production and alias in production:
                continue
            if _excepted(relative, alias, glossary):
                continue
            seen.add((name, line))
            findings.append(Finding(relative, line, name, glossary.aliases[alias]))
            continue
        whole = name.lstrip("_")
        parts = _spelled_words(name, glossary.words)
        suffix = _spelled_suffix(name, glossary.suffixes)
        # Every name with a suffix a notation path spells is in `notation`, so
        # this also leaves the notation paths their SI unit symbols.
        if suffix is not None and whole not in notation:
            parts.append(suffix)
        if not parts:
            continue
        if mirrors_production and whole in production:
            continue
        if _excepted(relative, whole, glossary):
            continue
        seen.add((name, line))
        for part, replacement in parts:
            findings.append(Finding(relative, line, name, (replacement,), part))
    return findings


def _excepted(relative: str, spelling: str, glossary: Glossary) -> bool:
    """Return whether an ``[[exception]]`` clears *spelling* in this file."""
    return any(relative.startswith(p) for p in glossary.exceptions.get(spelling, ()))


def _sources(paths: list[Path] | None) -> list[Path]:
    """Return the Python files to scan, defaulting to the whole project.

    Roots may overlap, so a file named by two of them is returned once: a
    second copy would double its count and report it over a budget it meets.
    """
    roots = paths or [REPO_ROOT / name for name in SCANNED]
    files: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        for path in sorted(root.rglob("*.py")) if root.is_dir() else [root]:
            if path not in seen:
                seen.add(path)
                files.append(path)
    return files


def _findings(paths: list[Path] | None, glossary: Glossary) -> list[Finding]:
    """Return every finding across the requested paths.

    Production is read in full whatever the requested paths are, because a file
    under `tests` is judged against the spellings production still carries and
    a partial reading of them would invent findings.
    """
    production = _production_spellings(glossary)
    notation = _notation_spellings(glossary)
    return [
        f
        for path in _sources(paths)
        for f in _scan(path, glossary, production, notation)
    ]


def _load_budget() -> dict[str, int]:
    """Return the recorded budget, or an empty one before it is first written."""
    try:
        return json.loads(BUDGET_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as err:
        sys.exit(f"{BUDGET_FILE.name} is not valid JSON: {err}")


def check(paths: list[Path] | None) -> int:
    """Compare today's findings against the budget. Return an exit code.

    A file under its budget fails like one over it, so a rename records the
    lower number it reached. A full check judges every recorded file, so a
    deleted file counts as zero and its budget has to be dropped. A partial
    check judges only the files it scanned and says nothing about the rest.
    """
    glossary = _load_glossary()
    findings: dict[str, list[Finding]] = {}
    for finding in _findings(paths, glossary):
        findings.setdefault(finding.path, []).append(finding)
    counts = Counter({path: len(found) for path, found in findings.items()})
    budget = _load_budget()

    over = sorted(
        (path, count, budget.get(path, 0))
        for path, count in counts.items()
        if count > budget.get(path, 0)
    )
    for path, count, allowed in over:
        for finding in findings[path]:
            print(finding)
        if allowed:
            print(f"over budget: {path} {count} rejected names, budget {allowed}\n")
        else:
            print(f"{path}: {count} rejected names, none allowed\n")

    if over:
        print(f"{len(over)} file(s) carry more rejected names than allowed")
        return 1

    if not counts and not budget:
        print("no rejected names")
        return 0
    print(
        f"{sum(counts.values())} rejected names across {len(counts)} files, "
        "all within budget"
    )
    judged = (
        budget.keys()
        if paths is None
        else {path.relative_to(REPO_ROOT).as_posix() for path in _sources(paths)}
    )
    slack = sum(
        1
        for path, count in budget.items()
        if path in judged and count > counts.get(path, 0)
    )
    if slack:
        print(
            f"{slack} file(s) below budget. Run `check_naming.py update` to record it."
        )
        return 1
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
