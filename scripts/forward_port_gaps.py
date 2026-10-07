"""Maintenance-line commits whose content never reached the development line.

`1.9` is the maintenance line, `develop` is what ships as the next major
version. A change wanted on both lines is written twice, one commit per line.
A change written only on `1.9` is a gap, and ``git cherry`` cannot tell a gap
from a pair: the two commits of a pair are written separately, so they share
no ancestry below the merge base, and ``git cherry`` matches by patch id,
which ignores only whitespace and line numbers, so two diffs that differ in
anything else count as unrelated. What they share is the text, so the
comparison is textual.

Every commit on the maintenance line that the development line does not
contain is reduced to a set of *markers*, and each marker is looked up in the
development line's **tree**. Comparing against the tree instead of the history
is what finds the pair: a line that reached `develop` under any
commit is in the tree. The share of a commit's markers found there is its hit
rate.

A marker is an added line that is

* in a production file, not under ``tests/``: each line writes its own tests,
  in its own fixtures and under its own names, so a test line says nothing
  about whether the fix it covers reached the other line,
* still in the maintenance line's tree, which excludes an intermediate state
  a later maintenance-line commit replaced. The development line cannot be
  asked to hold what the maintenance line itself no longer holds, and the
  replacing commit carries the evidence instead,
* at least ``MARKER_MIN_LENGTH`` characters once stripped,
* not a comment, not an import and not a decorator,
* carrying at least one character of ``=(){}[]:``, which excludes the prose
  inside a docstring — prose is reworded on the way across and matches
  nothing it should match,
* absent from the same file's parent revision, which excludes a line the
  commit only moved, reindented or copied from another section of the file.
  Such a line is in both trees no matter what the commit did.

The prose and parent-revision rules carry most of the separation. Without
the prose rule a docstring-heavy commit reads as absent because its sentences
were rewritten; without the parent-revision rule a translation block moved
into a new section reads as present because its strings already existed
elsewhere in the file.

A marker is looked up under the development line's names, too. The
development line renames identifiers onto the terms of its `glossary.toml`
and the maintenance line keeps the old spellings, so a marker spelling a
rejected alias is also searched with the alias replaced by each term that
lists it, one whole identifier at a time; a private name is matched
without its leading underscores and keeps them. The glossary is read from
the development tree, the one that did the renaming.

``MARKER_MIN_LENGTH`` is 16 from measurement. Over the 801 candidate lines of
eleven commits whose content was confirmed by hand to be absent from
`develop`, lines shorter than 16 characters were found in the tree anyway 45%
of the time (9 of 20) and lines of 16 or more 1% of the time (10 of 781).
Below 16 characters a line is short enough to recur by coincidence, above it
is not.

``HIT_RATE_THRESHOLD`` is 0.5, and the measured distribution has nothing near
it: over ``develop..origin/1.9``, commits carried forward score 75% and up,
commits not carried forward 33% and down.

Known misreadings, both directions:

* A commit that fixes the same defect differently on the two lines scores low
  and is not a gap. The maintenance line lacks the state machine `develop`
  has, so a guard placed in the machine on one line sits in the handlers on
  the other, and no marker survives that.
* A commit that carries `develop` code *into* the maintenance line scores
  high while adding nothing to `develop`. That reads correctly — there is
  nothing to forward-port — but the rate says "already there" rather than
  "went the other way".
* A commit with fewer than ``MIN_MARKERS`` markers gets no hit rate; it
  counts as carried forward only when every one of its markers is present.
  A commit with no production marker at all (version bumps, pure-prose and
  test-only commits, and a real change too small to leave one) is listed
  separately rather than dropped, because a truncated list reads like
  completeness.

Two modes:

``list``
    Print every commit with its hit rate, grouped by whether it is carried
    forward, plus the ones too small to score.

``check``
    Exit non-zero when a commit below the threshold is not recorded in
    `.forward-port-gaps.json`. That file maps a commit to the reason it stays
    behind, and it is written by hand: a generated reason would be empty, and
    the reason is the point.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import itertools
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import tomllib

REPO_ROOT = Path(__file__).resolve().parent.parent
ACKNOWLEDGED_FILE = REPO_ROOT / ".forward-port-gaps.json"

DEFAULT_MAINTENANCE = "origin/1.9"
DEFAULT_DEVELOPMENT = "origin/develop"

# Each line writes its own tests, so no marker is taken from under here.
TEST_ROOT = "tests/"
GLOSSARY_PATH = "glossary.toml"

MARKER_MIN_LENGTH = 16
MARKERS_PER_COMMIT = 12
MIN_MARKERS = 3
HIT_RATE_THRESHOLD = 0.5
# Spellings tried for one marker; bounds the work a line full of aliases costs.
SPELLINGS_PER_MARKER = 256

# Suffixes whose content is line-oriented text worth comparing. Translations
# are `.json` and blueprints are `.yaml`, so both carry markers.
TEXT_SUFFIXES = (
    ".py",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".md",
    ".mdx",
    ".js",
    ".ts",
    ".tsx",
)

COMMENT_START = re.compile(r"""^(#|//|/\*|\*|<!--|-->|\"\"\"|''')""")
IMPORT_OR_DECORATOR = re.compile(r"^(import |from \S+ import|@)")
# Each line carries its own release number, so a bump is never forwarded.
VERSION_LINE = re.compile(r"""^["']?version["']?\s*[:=]""")
HUNK_HEADER = re.compile(r"^@@ -\S+ \+(\d+)")
CODE_PUNCTUATION = frozenset("=(){}[]:")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# A subject written under this repository's commit convention. The convention
# is what separates the two groups here: work built as a pair follows it and
# contributions taken from elsewhere do not. It is a proxy for the group and
# not a claim about authorship — a contributor who writes a conventional
# subject is counted with the pairs.
CONVENTIONAL_SUBJECT = re.compile(
    r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)"
    r"(\([a-z0-9_./-]+\))?!?: "
)


@dataclass(frozen=True)
class Commit:
    """One maintenance-line commit measured against the development tree."""

    sha: str
    subject: str
    author: str
    markers: int
    hits: int

    @property
    def short(self) -> str:
        """Return the abbreviated commit id."""
        return self.sha[:8]

    @property
    def scored(self) -> bool:
        """Return whether the commit carries enough markers to judge."""
        return self.markers >= MIN_MARKERS

    @property
    def hit_rate(self) -> float:
        """Return the share of markers found in the development tree."""
        return self.hits / self.markers if self.markers else 0.0

    @property
    def conventional(self) -> bool:
        """Return whether the subject follows this repository's convention."""
        return CONVENTIONAL_SUBJECT.match(self.subject) is not None

    @property
    def carried_forward(self) -> bool:
        """Return whether enough of the commit is present to call it carried.

        A scored commit needs its share of markers. One too small to score
        needs every marker it has, because a missing one-line fix would
        otherwise pass unread; a commit with no marker has nothing to carry.
        """
        if self.scored:
            return self.hit_rate >= HIT_RATE_THRESHOLD
        return self.hits == self.markers


def _git(*arguments: str) -> str:
    """Return the standard output of a git command that has to succeed.

    ``core.quotePath`` is off because both commands that print paths here are
    read by their prefix and their suffix. Git quotes a path carrying bytes
    above ASCII, and a quoted one opens with a quotation mark instead of
    ``b/`` and ends in one instead of a known suffix, so the file it names
    drops out of the comparison on both sides. That setting does not reach a
    tab, a quotation mark or a backslash in a name, which git escapes
    regardless; :func:`_unquote_path` reads those back.
    """
    finished = subprocess.run(
        ("git", "-C", str(REPO_ROOT), "-c", "core.quotePath=false", *arguments),
        capture_output=True,
        text=True,
        check=False,
    )
    if finished.returncode != 0:
        sys.exit(f"git {' '.join(arguments)} failed: {finished.stderr.strip()}")
    return finished.stdout


def _resolve(ref: str) -> str:
    """Return the commit a ref names, or exit saying how to fetch it."""
    finished = subprocess.run(
        ("git", "-C", str(REPO_ROOT), "rev-parse", "--verify", f"{ref}^{{commit}}"),
        capture_output=True,
        text=True,
        check=False,
    )
    if finished.returncode != 0:
        branch = ref.removeprefix("origin/")
        sys.exit(
            f"no such ref: {ref} — fetch it first, for example\n"
            "  git fetch https://github.com/KartoffelToby/better_thermostat.git "
            f"{branch}:refs/remotes/origin/{branch}"
        )
    return finished.stdout.strip()


def _is_text(path: str) -> bool:
    """Return whether a path is one of the line-oriented text kinds."""
    return path.endswith(TEXT_SUFFIXES)


def _unquote_path(field: str) -> str:
    r"""Return the pathname a git field names, undoing its C quoting.

    Git wraps a pathname in quotation marks and escapes it as a C string as
    soon as it holds a control character, a quotation mark or a backslash.
    ``core.quotePath=false`` does not reach those, so a name carrying a tab
    reads as ``"we\tird.py"`` — it neither starts with ``b/`` nor ends in a
    known suffix, and the file drops silently out of the comparison, which is
    the file a gap is least likely to be noticed in.
    """
    if not field.startswith('"') or not field.endswith('"'):
        return field
    # The escapes stand for bytes, so they are decoded to bytes first and
    # read back as the UTF-8 git wrote them from.
    escaped = field[1:-1]
    return (
        escaped.encode("utf-8")
        .decode("unicode_escape")
        .encode("latin-1")
        .decode("utf-8", "replace")
    )


def _tree_paths(ref: str) -> list[str]:
    """Return every path in the ref's tree, read without quoting.

    ``-z`` makes git write the names raw and separate them with NUL, which is
    the one separator a pathname cannot hold.
    """
    finished = subprocess.run(
        ("git", "-C", str(REPO_ROOT), "ls-tree", "-r", "-z", "--name-only", ref),
        capture_output=True,
        check=True,
    )
    return [
        name.decode("utf-8", "replace") for name in finished.stdout.split(b"\0") if name
    ]


def _tree_lines(ref: str) -> set[str]:
    """Return every stripped line the ref's production text files hold.

    Markers come from production files only, so they are looked up there
    only: a test that quotes the line it pins holds the text without the
    change being in place.
    """
    paths = [
        path
        for path in _tree_paths(ref)
        if _is_text(path) and not path.startswith(TEST_ROOT)
    ]
    # ``-z`` on the input too: without it the request is one path per line,
    # and a name holding a newline would be read as two requests.
    specification = "".join(f"{ref}:{path}\0" for path in paths).encode()
    finished = subprocess.run(
        ("git", "-C", str(REPO_ROOT), "cat-file", "--batch", "-z"),
        input=specification,
        capture_output=True,
        check=True,
    )
    return _parse_batch(finished.stdout)


def _parse_batch(output: bytes) -> set[str]:
    """Return the stripped lines of every blob in a ``cat-file --batch`` stream.

    Parameters
    ----------
    output
        The raw stream: one ``<oid> <type> <size>`` header line per request,
        each followed by ``size`` bytes of content and a newline. The size is
        in bytes, so the stream is walked as bytes and decoded per blob.
    """
    lines: set[str] = set()
    position = 0
    while position < len(output):
        end_of_header = output.find(b"\n", position)
        if end_of_header < 0:
            break
        header = output[position:end_of_header].split()
        # A path that is missing from the tree gets "<name> missing" instead
        # of a header, and carries no content to skip over.
        if len(header) < 3:
            position = end_of_header + 1
            continue
        size = int(header[2])
        blob = output[end_of_header + 1 : end_of_header + 1 + size]
        position = end_of_header + 1 + size + 1
        for line in blob.decode("utf-8", "replace").splitlines():
            stripped = line.strip()
            if stripped:
                lines.add(stripped)
    return lines


def _looks_distinctive(line: str) -> bool:
    """Return whether a stripped added line can carry evidence of a change."""
    if len(line) < MARKER_MIN_LENGTH:
        return False
    if COMMENT_START.match(line):
        return False
    if IMPORT_OR_DECORATOR.match(line):
        return False
    if VERSION_LINE.match(line):
        return False
    return bool(CODE_PUNCTUATION & set(line))


def _added_lines(sha: str) -> dict[str, list[str]]:
    """Return the distinctive added lines of a commit, per file.

    A file's ``--- a/…`` and ``+++ b/…`` headers stand between ``diff --git``
    and the file's first hunk, and only there. Inside a hunk the same prefixes
    belong to content: at zero context a removed line reading ``--x`` renders
    as ``---x`` and an added line reading ``++x`` as ``+++x``, so a header is
    only recognised while no hunk is open. A YAML document separator or a
    Markdown rule is enough to meet that shape.
    """
    diff = _git("show", "--format=", "--unified=0", "--no-color", sha)
    per_file: dict[str, list[str]] = {}
    path: str | None = None
    in_hunk = False
    number = 0
    prose: set[int] = set()
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            # Every file starts out unnamed. A deleted one reads
            # "+++ /dev/null" and never gets a name, so nothing is collected
            # under it.
            path = None
            in_hunk = False
            continue
        if line.startswith("@@"):
            in_hunk = True
            header = HUNK_HEADER.match(line)
            number = int(header[1]) if header else 0
            continue
        if not in_hunk:
            if line.startswith("+++ "):
                field = _unquote_path(line[4:])
                # A deleted file reads "+++ /dev/null" and keeps no name.
                path = field[2:] if field.startswith("b/") else None
                prose = _docstring_lines(sha, path) if path else set()
            continue
        if not line.startswith("+"):
            continue
        number += 1
        if path is None or not _is_text(path) or number - 1 in prose:
            continue
        stripped = line[1:].strip()
        if _looks_distinctive(stripped):
            per_file.setdefault(path, []).append(stripped)
    return per_file


def _docstring_lines(sha: str, path: str) -> set[int]:
    """Return the line numbers a Python file's string statements span at ``sha``.

    A docstring is prose, and prose that quotes code carries the punctuation
    the marker rule looks for. Where the line sits tells it apart. A file
    that is not Python, or does not parse, marks nothing.
    """
    if not path.endswith(".py"):
        return set()
    finished = subprocess.run(
        ("git", "-C", str(REPO_ROOT), "show", f"{sha}:{path}"),
        capture_output=True,
        text=True,
        check=False,
    )
    if finished.returncode != 0:
        return set()
    try:
        tree = ast.parse(finished.stdout)
    except SyntaxError:
        return set()
    return {
        number
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        and node.end_lineno is not None
        for number in range(node.lineno, node.end_lineno + 1)
    }


def _prior_lines(sha: str, path: str) -> set[str]:
    """Return the stripped lines the file held before the commit."""
    finished = subprocess.run(
        ("git", "-C", str(REPO_ROOT), "show", f"{sha}^:{path}"),
        capture_output=True,
        text=True,
        check=False,
    )
    if finished.returncode != 0:
        return set()
    return {line.strip() for line in finished.stdout.splitlines()}


def markers_of(
    sha: str, limit: int | None = None, present: set[str] | None = None
) -> list[str]:
    """Return up to ``limit`` markers for a commit.

    Parameters
    ----------
    sha
        The commit to read.
    limit
        How many markers to take, ``MARKERS_PER_COMMIT`` when left open. The
        budget is read on each call rather than bound into the signature, so
        a caller that varies the constant varies what this returns. Markers
        are drawn one file at a time, longest first within a file, so a commit
        spread over many files is judged on all of them rather than on
        whichever one is largest.
    present
        The stripped lines of the maintenance line's tree. A line missing
        there was replaced by a later commit and is no marker. Left open,
        every added line counts.
    """
    if limit is None:
        limit = MARKERS_PER_COMMIT
    per_file: dict[str, list[str]] = {}
    for path, lines in _added_lines(sha).items():
        if path.startswith(TEST_ROOT):
            continue
        prior = _prior_lines(sha, path)
        fresh = sorted(
            {
                line
                for line in lines
                if line not in prior and (present is None or line in present)
            },
            key=lambda line: (-len(line), line),
        )
        if fresh:
            per_file[path] = fresh

    # One marker string counts once for the whole commit. The development
    # tree is searched as one set of lines, so a string taken from three
    # files is answered by a single occurrence over there — the commit would
    # score full marks for one file's change having been carried forward.
    chosen: list[str] = []
    taken: set[str] = set()
    depth = 0
    while len(chosen) < limit:
        available = [path for path in sorted(per_file) if depth < len(per_file[path])]
        if not available:
            break
        for path in available:
            marker = per_file[path][depth]
            if marker in taken:
                continue
            taken.add(marker)
            chosen.append(marker)
            if len(chosen) >= limit:
                break
        depth += 1
    return chosen


def _renames(ref: str) -> dict[str, tuple[str, ...]]:
    """Return each rejected alias of the ref's glossary with the names replacing it.

    A term's name may be qualified (``trv.setpoint``); the identifier written
    in code is its last part.
    """
    finished = subprocess.run(
        ("git", "-C", str(REPO_ROOT), "show", f"{ref}:{GLOSSARY_PATH}"),
        capture_output=True,
        text=True,
        check=False,
    )
    if finished.returncode != 0:
        return {}
    try:
        terms = tomllib.loads(finished.stdout).get("term", [])
    except tomllib.TOMLDecodeError as err:
        sys.exit(f"{ref}:{GLOSSARY_PATH} is not valid TOML: {err}")
    renames: dict[str, list[str]] = {}
    for term in terms:
        rejected = term.get("rejected", [])
        if not isinstance(rejected, list):
            sys.exit(f"{ref}:{GLOSSARY_PATH}: `rejected` of {term['name']} is no list")
        for alias in rejected:
            renames.setdefault(alias, []).append(term["name"].rpartition(".")[2])
    return {alias: tuple(names) for alias, names in renames.items()}


def _renamed(name: str, renames: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    """Return the names the glossary renames *name* to, or nothing.

    A private name is looked up without its leading underscores and keeps
    them in every replacement, the way the naming gate reads it.
    """
    if name in renames:
        return renames[name]
    stripped = name.lstrip("_")
    prefix = name[: len(name) - len(stripped)]
    return tuple(prefix + term for term in renames.get(stripped, ()))


def _spellings(marker: str, renames: dict[str, tuple[str, ...]]) -> list[str]:
    """Return the marker as written plus every spelling the glossary renames it to.

    Each occurrence of an alias is spelled on its own, as written or as any
    name replacing it, so one line can carry an alias renamed in one place
    and kept or renamed differently in another. Past ``SPELLINGS_PER_MARKER``
    combinations every occurrence of an alias takes the same spelling, and
    no more than that many spellings are returned.
    """
    occurrences = [
        match for match in IDENTIFIER.finditer(marker) if _renamed(match[0], renames)
    ]
    options = [(match[0], *_renamed(match[0], renames)) for match in occurrences]
    if math.prod(len(choices) for choices in options) > SPELLINGS_PER_MARKER:
        aliases = sorted({match[0] for match in occurrences})
        options = [(alias, *_renamed(alias, renames)) for alias in aliases]
        option_of = [aliases.index(match[0]) for match in occurrences]
    else:
        option_of = list(range(len(occurrences)))
    spellings = [marker]
    for choice in itertools.product(*options):
        pieces: list[str] = []
        end = 0
        for match, index in zip(occurrences, option_of, strict=True):
            pieces.extend((marker[end : match.start()], choice[index]))
            end = match.end()
        renamed = "".join(pieces) + marker[end:]
        if renamed != marker:
            spellings.append(renamed)
        if len(spellings) >= SPELLINGS_PER_MARKER:
            break
    return spellings


def measure(maintenance: str, development: str) -> list[Commit]:
    """Return every maintenance-line commit scored against the development tree."""
    tree = _tree_lines(development)
    present = _tree_lines(maintenance)
    renames = _renames(development)
    log = _git(
        "log",
        "--no-merges",
        "--reverse",
        "--format=%H%x1f%an%x1f%s",
        f"{development}..{maintenance}",
    )
    commits: list[Commit] = []
    for entry in log.splitlines():
        sha, author, subject = entry.split("\x1f", 2)
        markers = markers_of(sha, present=present)
        commits.append(
            Commit(
                sha=sha,
                subject=subject,
                author=author,
                markers=len(markers),
                hits=sum(
                    1
                    for marker in markers
                    if any(line in tree for line in _spellings(marker, renames))
                ),
            )
        )
    return commits


def _load_acknowledged() -> dict[str, str]:
    """Return the recorded reasons, or an empty mapping when there are none."""
    if not ACKNOWLEDGED_FILE.exists():
        return {}
    return json.loads(ACKNOWLEDGED_FILE.read_text(encoding="utf-8"))


def _describe(commit: Commit) -> str:
    """Return one report line for a commit."""
    rate = f"{commit.hit_rate * 100:3.0f}%" if commit.scored else " n/a"
    return (
        f"  {commit.short}  {rate}  {commit.hits:>2}/{commit.markers:<2}  "
        f"{commit.author[:16]:16}  {commit.subject[:64]}"
    )


def _headline(maintenance: str, development: str) -> str:
    """Return the line naming what was compared against what."""
    return (
        f"{maintenance} ({_resolve(maintenance)[:8]}) "
        f"against the tree of {development} ({_resolve(development)[:8]})"
    )


def show(maintenance: str, development: str) -> int:
    """Print every commit with its hit rate. Return an exit code."""
    commits = measure(maintenance, development)
    print(_headline(maintenance, development))
    print(f"{len(commits)} commits the development line does not contain\n")

    behind = [c for c in commits if not c.carried_forward]
    carried = [c for c in commits if c.markers and c.carried_forward]
    unmarked = [c for c in commits if not c.markers]

    print(
        f"not carried forward — under {HIT_RATE_THRESHOLD:.0%}, or a marker "
        f"missing from a commit with fewer than {MIN_MARKERS}:"
    )
    for commit in sorted(behind, key=lambda c: c.hit_rate):
        print(_describe(commit))
    print(
        f"\ncarried forward — {HIT_RATE_THRESHOLD:.0%} or more, or every marker "
        f"of a commit with fewer than {MIN_MARKERS}:"
    )
    for commit in sorted(carried, key=lambda c: c.hit_rate):
        print(_describe(commit))
    print("\nno production markers:")
    for commit in unmarked:
        print(_describe(commit))

    print("\nby commit convention:")
    for label, group in (
        ("conventional subject", [c for c in commits if c.conventional]),
        ("other", [c for c in commits if not c.conventional]),
    ):
        scored = [c for c in group if c.scored]
        low = sum(1 for c in scored if not c.carried_forward)
        span = (
            f"{min(c.hit_rate for c in scored):.0%}-"
            f"{max(c.hit_rate for c in scored):.0%}"
            if scored
            else "n/a"
        )
        print(
            f"  {label:22}  {len(group):3} commits  {len(scored):3} scored  "
            f"{span:>9}  {low:3} not carried forward"
        )
    return 0


def check(maintenance: str, development: str) -> int:
    """Report unrecorded gaps. Return an exit code."""
    commits = measure(maintenance, development)
    acknowledged = _load_acknowledged()
    behind = {c.sha: c for c in commits if not c.carried_forward}

    unrecorded = [behind[sha] for sha in sorted(behind) if sha not in acknowledged]
    stale = sorted(sha for sha in acknowledged if sha not in behind)

    print(_headline(maintenance, development))
    for sha in stale:
        print(f"no longer behind: {sha[:8]} — drop it from {ACKNOWLEDGED_FILE.name}")

    if not unrecorded:
        marked = sum(1 for c in commits if c.markers)
        print(
            f"all {marked} commits with production markers are carried forward "
            f"or recorded ({len(commits) - marked} without any)"
        )
        return 0

    print(
        f"\n{len(behind)} commits are not carried forward, "
        f"{len(behind) - len(unrecorded)} of them recorded in "
        f"{ACKNOWLEDGED_FILE.name}. The remaining {len(unrecorded)}:"
    )
    for commit in sorted(unrecorded, key=lambda c: c.hit_rate):
        print(_describe(commit))
    print(
        f"\nForward-port the change, or record the commit in "
        f"{ACKNOWLEDGED_FILE.name} with the reason it stays behind."
    )
    return 1


def main() -> int:
    """Parse arguments and run the requested mode."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("check", "list"))
    parser.add_argument(
        "--maintenance",
        default=DEFAULT_MAINTENANCE,
        help=f"the line to read commits from (default: {DEFAULT_MAINTENANCE})",
    )
    parser.add_argument(
        "--development",
        default=DEFAULT_DEVELOPMENT,
        help=f"the line whose tree is searched (default: {DEFAULT_DEVELOPMENT})",
    )
    args = parser.parse_args()
    _resolve(args.maintenance)
    _resolve(args.development)
    if args.mode == "check":
        return check(args.maintenance, args.development)
    return show(args.maintenance, args.development)


if __name__ == "__main__":
    raise SystemExit(main())
