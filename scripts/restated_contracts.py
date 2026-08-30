"""List the test docstrings that restate the code instead of naming a requirement.

A test can only assert its rule as well as somebody once wanted that rule. Where
the docstring retells what the implementation does — "If ANY TRV is HomematicIP,
600s debounce applies" — the test holds the behaviour it found, and it holds a
defect just as firmly as a decision. Nothing in the suite objects: the lines
run, the module keeps its coverage floor, and a mutation probe reports the spot
as *well covered*, because a defect planted there turns it red. Whether a
contract is the wanted one is a question to the text, not to the execution.

So this is a reading aid, not a verdict. A hit means "ask the question here",
and the question is always the same one: does the docstring name the behaviour
somebody asked for, or the behaviour somebody found? Both answers occur, and
the wording that produces a hit is perfectly correct in plenty of tests — a
requirement may legitimately say "never" or "regardless".

One form runs by default and carries the budget. Every test function is parsed
with :mod:`ast` and the opening paragraph of its docstring matched against
wordings that repeat the *shape of a condition* rather than an obligation: a
shouted quantifier (`ANY`, `ALWAYS`), an `if any`/`if all` clause,
`regardless`, `even when`, and a bare interval copied out of the source
(`600s`). Only that paragraph is read, because the ones under it describe the
setup, where such a word qualifies a precondition. The docstring is taken from
the tree, so a comment or a string that merely carries the wording is not
counted.

``list`` marks a hit with ``!`` when the summary also spells an identifier or a
call out of the source. A public name is a fair word for a requirement, so on
its own that is not enough to ask about, but next to a restating wording it is
the sharper half of the list.

**What this does not reach.** The marker list is a sample of the suspicion, not
a survey of it. Of 3275 test docstrings in the tree it reports 112, and two
common shapes stay outside it by measurement: 246 summaries opening "Test that
…", which name the call rather than the obligation, and 177 built around
"should", which state an expectation without saying whose. Matching either
would add 423 sentences to a list of 112 and bury the question this exists to
ask, so both are left to the reader. A green run means the budget held, never
that the tree is free of restatements.

**Symptom wording.** A test whose docstring repeats a phrase from a document
that collects reported symptoms is pinning what a user complained about. The
repository tracks no such document, so this form needs one passed in with
``--symptoms``. It never contributes to the budget — a count that depends on an
untracked file could not be held — and it runs only in ``list``.

Three modes:

``check``
    Count today's findings and exit non-zero when a file is over its budget, or
    when a file with no budget has a finding at all.

``update``
    Rewrite the budget from today's counts. Run this after rewording a
    docstring, so the lower number is the one that has to be held.

``list``
    Print the findings themselves, for one path or for the whole tree.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
BUDGET_FILE = REPO_ROOT / ".restated-contract-budget.json"
SCANNED = "tests"

# A bare count of seconds or minutes is the source's own number. Unlike a
# quantifier it carries no claim at all, so it is only ever a copy.
_INTERVAL = r"\b\d+ ?(?:s|ms|min)\b"

# Each wording repeats the shape of a condition instead of stating what is
# owed. A shouted quantifier is the sharpest of them: nobody writes a
# requirement in capitals, but somebody reading a loop out of the source does.
# `NONE` is left out on purpose: in this tree it is nearly always the spelling
# of a value — `PRESET_NONE`, a mode written out in a transition — and not a
# quantifier somebody shouted.
MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("shouted quantifier", re.compile(r"\b(?:ANY|ALL|EVERY|ALWAYS|NEVER)\b")),
    (
        "quantified condition",
        re.compile(r"\bif (?:any|all|either|neither|none)\b", re.I),
    ),
    ("regardless", re.compile(r"\bregardless\b|\bno matter\b|\birrespective\b", re.I)),
    ("even when", re.compile(r"\beven (?:when|if|though|after|while)\b", re.I)),
    ("unconditional", re.compile(r"\bunconditional(?:ly)?\b", re.I)),
    ("in every case", re.compile(r"\bin (?:every|all|either) cases?\b", re.I)),
    ("always or never", re.compile(r"\b(?:always|never)\b", re.I)),
    ("copied interval", re.compile(_INTERVAL)),
)

# A summary that reaches for the implementation's own vocabulary is describing
# the code rather than the obligation. An identifier and a written-out call are
# too common in this tree to ask about on their own — a public name is a fair
# word for a requirement — so they only sharpen a hit somebody else produced.
# The interval is the exception and stands in MARKERS as well: a bare number of
# seconds has no meaning outside the source it was copied from.
CODE_SHAPED = re.compile(
    r"\b[a-z_]+_[a-z_]+\b"  # snake_case identifier
    r"|\b\w+\(\)"  # a call written out
    r"|" + _INTERVAL  # an interval copied from the source
)

# A phrase shorter than this matches by accident. Symptom documents are prose,
# and a two-word span is a turn of phrase rather than a report.
MIN_SYMPTOM_PHRASE_WORDS = 3


@dataclass(frozen=True)
class Finding:
    """One test docstring that reads like a restatement of the code."""

    path: str
    line: int
    test: str
    marker: str
    quote: str
    code_shaped: bool

    def __str__(self) -> str:
        """Return the finding as a ``file:line: message`` line."""
        loud = "!" if self.code_shaped else " "
        return (
            f"{loud} {self.path}:{self.line}: {self.test} — {self.marker}: "
            f'"{self.quote}"'
        )


def _test_files(paths: list[str] | None) -> list[Path]:
    """Return the tracked test modules, restricted to ``paths`` when given."""
    listing = subprocess.run(
        ("git", "ls-files", "-z", "--", *(paths or (SCANNED,))),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if listing.returncode != 0:
        sys.exit(f"git could not list the test files:\n{listing.stderr.strip()}")
    names = [n for n in listing.stdout.split("\0") if n.endswith(".py")]
    if not names:
        sys.exit(f"no tracked Python files under {', '.join(paths or (SCANNED,))}")
    return [REPO_ROOT / name for name in names]


def _summary_paragraph(docstring: str) -> str:
    """Return everything above the first blank line, collapsed onto one line.

    A summary that wraps across several lines is one sentence to read, so the
    whole opening paragraph is returned rather than its first line.
    """
    summary = docstring.strip().split("\n\n", 1)[0]
    return " ".join(summary.split())


def _parse_module(path: Path) -> ast.Module:
    """Return a module's syntax tree, or stop the run when it will not parse.

    A module that could not be parsed has no findings, which is the same answer
    a clean module gives. Nothing downstream can tell the two apart, so an
    unparseable module ends the run instead of being counted as empty.
    """
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as err:
        sys.exit(f"{path} could not be parsed, so nothing was counted: {err}")


def _scan_module(path: Path) -> list[Finding]:
    """Return the restating docstrings among a module's test functions."""
    tree = _parse_module(path)
    relative = path.relative_to(REPO_ROOT).as_posix()
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not node.name.startswith("test_"):
            continue
        docstring = ast.get_docstring(node, clean=True)
        if not docstring:
            continue
        # Only the opening paragraph is read. It is the sentence that states
        # the contract, however far it wraps; the paragraphs under it explain
        # the setup, and a "never" down there qualifies a precondition rather
        # than the obligation.
        summary = _summary_paragraph(docstring)
        for marker, pattern in MARKERS:
            if pattern.search(summary) is None:
                continue
            findings.append(
                Finding(
                    relative,
                    node.lineno,
                    node.name,
                    marker,
                    summary,
                    CODE_SHAPED.search(summary) is not None,
                )
            )
            # One question per test is enough to ask it; a second wording in
            # the same sentence would only inflate the count.
            break
    return findings


def _symptom_phrases(path: Path) -> list[str]:
    """Return the quoted and backticked spans a symptom document carries."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        sys.exit(f"the symptom document could not be read: {err}")
    spans = re.findall(r"`([^`\n]+)`|\"([^\"\n]+)\"|„([^“\n]+)“", text)
    phrases = {(a or b or c).strip() for a, b, c in spans}
    return sorted(p for p in phrases if len(p.split()) >= MIN_SYMPTOM_PHRASE_WORDS)


def _scan_symptoms(files: list[Path], phrases: list[str]) -> list[Finding]:
    """Return the tests whose docstring repeats a reported symptom."""
    lowered = [(phrase, phrase.lower()) for phrase in phrases]
    findings: list[Finding] = []
    for path in files:
        tree = _parse_module(path)
        relative = path.relative_to(REPO_ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not node.name.startswith("test_"):
                continue
            docstring = ast.get_docstring(node, clean=True)
            if not docstring:
                continue
            haystack = _summary_paragraph(docstring).lower()
            for phrase, needle in lowered:
                if needle in haystack:
                    findings.append(
                        Finding(
                            relative,
                            node.lineno,
                            node.name,
                            "reported symptom",
                            phrase,
                            CODE_SHAPED.search(phrase) is not None,
                        )
                    )
                    break
    return findings


def _measure() -> dict[str, int]:
    """Return the number of restating docstrings per test module."""
    counts: dict[str, int] = {}
    for path in _test_files(None):
        for finding in _scan_module(path):
            counts[finding.path] = counts.get(finding.path, 0) + 1
    return counts


def _load_budget() -> dict[str, int]:
    """Return the stored budget, or an empty mapping when there is none yet."""
    if not BUDGET_FILE.exists():
        return {}
    return json.loads(BUDGET_FILE.read_text(encoding="utf-8"))


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
        print(
            f"all {len(budget)} files stay within their budget "
            f"({sum(counts.values())} docstrings left to ask about)"
        )
        return 0

    if over:
        print("\nmore restating docstrings than the budget allows:")
        for name, allowed, now in over:
            print(f"  {name}: {now} > {allowed}")
    if unbudgeted:
        print("\nrestating docstrings in files with no budget:")
        for name in unbudgeted:
            print(f"  {name}: {counts[name]}")
    print(
        "\nSay what the test requires, not what the code does. Where the "
        "requirement cannot be phrased without sounding like a bug, it is one. "
        "The budget only ever falls."
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
    print(
        f"recorded {sum(counts.values())} candidates over {len(counts)} files "
        f"in {BUDGET_FILE.name}"
    )
    return 0


def show(paths: list[str] | None, symptoms: Path | None) -> int:
    """Print the candidates themselves. Return an exit code."""
    files = _test_files(paths)
    findings = [f for path in files for f in _scan_module(path)]
    if symptoms is not None:
        phrases = _symptom_phrases(symptoms)
        print(
            f"# {len(phrases)} symptom phrases read from {symptoms}\n", file=sys.stderr
        )
        findings += _scan_symptoms(files, phrases)

    by_marker: dict[str, int] = {}
    for finding in sorted(findings, key=lambda f: (f.path, f.line)):
        print(finding)
        by_marker[finding.marker] = by_marker.get(finding.marker, 0) + 1

    loud = sum(1 for f in findings if f.code_shaped)
    print(f"\n{len(findings)} docstrings to ask the question about:")
    for marker, count in sorted(by_marker.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {count:>4}  {marker}")
    print(
        f"\n{loud} of them are marked '!': the summary also spells an "
        "identifier, a call or an interval out of the source, which is where "
        "to start reading."
    )
    print(
        "\nA hit is a question, not a verdict: a requirement may well say "
        "'never'. Read each one and decide whether the sentence names the "
        "wanted behaviour or the found one."
    )
    return 0


def main() -> int:
    """Parse arguments and run the requested mode."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("check", "update", "list"))
    parser.add_argument(
        "paths",
        nargs="*",
        help="limit 'list' to these paths (default: the whole test tree)",
    )
    parser.add_argument(
        "--symptoms",
        type=Path,
        help="a document collecting reported symptoms; 'list' also reports the "
        "tests whose docstring repeats a phrase from it",
    )
    args = parser.parse_args()

    if args.mode != "list" and (args.paths or args.symptoms):
        parser.error("'paths' and '--symptoms' belong to the 'list' mode")
    if args.mode == "check":
        return check()
    if args.mode == "update":
        return update()
    return show(args.paths or None, args.symptoms)


if __name__ == "__main__":
    raise SystemExit(main())
