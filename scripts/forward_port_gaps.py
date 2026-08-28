"""Maintenance-line commits whose content never reached the development line.

`1.9` is the maintenance line, `develop` is what ships as the next major
version. A change wanted on both lines is written twice, one commit per line.
A change written only on `1.9` is a gap, and the gap is invisible to
``git cherry``: pull requests land squashed, so a pair shares no patch id and
no ancestry below the merge base. What survives a squash is the text, so the
comparison is textual.

Every commit on the maintenance line that the development line does not
contain is reduced to a set of *markers*, and each marker is looked up in the
development line's **tree**. Comparing against the tree instead of the history
is what makes the squash irrelevant: a line that reached `develop` under any
commit is in the tree. The share of a commit's markers found there is its hit
rate.

A marker is an added line that is

* at least ``MARKER_MIN_LENGTH`` characters once stripped,
* not a comment, not an import and not a decorator,
* carrying at least one character of ``=(){}[]:``, which excludes the prose
  inside a docstring — prose is reworded on the way across and matches
  nothing it should match,
* absent from the same file's parent revision, which excludes a line the
  commit only moved, reindented or copied from another section of the file.
  Such a line is in both trees no matter what the commit did.

The last two rules carry most of the separation. Without the prose rule a
docstring-heavy commit reads as absent because its sentences were rewritten;
without the parent-revision rule a translation block moved into a new section
reads as present because its strings already existed elsewhere in the file.

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
* A commit with fewer than ``MIN_MARKERS`` markers is not scored at all.
  Version bumps and pure-prose commits land there, and so does a real change
  small enough to leave no marker. Those commits are listed separately rather
  than dropped, because a truncated list reads like completeness.

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
from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
ACKNOWLEDGED_FILE = REPO_ROOT / ".forward-port-gaps.json"

DEFAULT_MAINTENANCE = "origin/1.9"
DEFAULT_DEVELOPMENT = "develop"

MARKER_MIN_LENGTH = 16
MARKERS_PER_COMMIT = 12
MIN_MARKERS = 3
HIT_RATE_THRESHOLD = 0.5

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
CODE_PUNCTUATION = frozenset("=(){}[]:")

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
        """Return whether enough of the commit is present to call it carried."""
        return self.hit_rate >= HIT_RATE_THRESHOLD


def _git(*arguments: str) -> str:
    """Return the standard output of a git command that has to succeed.

    ``core.quotePath`` is off because both commands that print paths here are
    read by their prefix and their suffix. Git quotes a path carrying bytes
    above ASCII, and a quoted one opens with a quotation mark instead of
    ``b/`` and ends in one instead of a known suffix, so the file it names
    drops out of the comparison on both sides.
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
        sys.exit(
            f"no such ref: {ref} — fetch it first, for example\n"
            "  git fetch https://github.com/KartoffelToby/better_thermostat.git "
            "1.9:refs/remotes/origin/1.9"
        )
    return finished.stdout.strip()


def _is_text(path: str) -> bool:
    """Return whether a path is one of the line-oriented text kinds."""
    return path.endswith(TEXT_SUFFIXES)


def _tree_lines(ref: str) -> set[str]:
    """Return every stripped text line the ref's tree holds."""
    paths = _git("ls-tree", "-r", "--name-only", ref).splitlines()
    specification = "".join(f"{ref}:{p}\n" for p in paths if _is_text(p)).encode()
    finished = subprocess.run(
        ("git", "-C", str(REPO_ROOT), "cat-file", "--batch"),
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
            continue
        if not in_hunk:
            if line.startswith("+++ b/"):
                path = line[6:]
            continue
        if not line.startswith("+") or path is None or not _is_text(path):
            continue
        stripped = line[1:].strip()
        if _looks_distinctive(stripped):
            per_file.setdefault(path, []).append(stripped)
    return per_file


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


def markers_of(sha: str, limit: int | None = None) -> list[str]:
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
    """
    if limit is None:
        limit = MARKERS_PER_COMMIT
    per_file: dict[str, list[str]] = {}
    for path, lines in _added_lines(sha).items():
        prior = _prior_lines(sha, path)
        fresh = sorted(
            {line for line in lines if line not in prior},
            key=lambda line: (-len(line), line),
        )
        if fresh:
            per_file[path] = fresh

    chosen: list[str] = []
    depth = 0
    while len(chosen) < limit:
        available = [path for path in sorted(per_file) if depth < len(per_file[path])]
        if not available:
            break
        for path in available:
            chosen.append(per_file[path][depth])
            if len(chosen) >= limit:
                break
        depth += 1
    return chosen


def measure(maintenance: str, development: str) -> list[Commit]:
    """Return every maintenance-line commit scored against the development tree."""
    tree = _tree_lines(development)
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
        markers = markers_of(sha)
        commits.append(
            Commit(
                sha=sha,
                subject=subject,
                author=author,
                markers=len(markers),
                hits=sum(1 for marker in markers if marker in tree),
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

    behind = [c for c in commits if c.scored and not c.carried_forward]
    carried = [c for c in commits if c.scored and c.carried_forward]
    unscored = [c for c in commits if not c.scored]

    print(f"not carried forward — under {HIT_RATE_THRESHOLD:.0%}:")
    for commit in sorted(behind, key=lambda c: c.hit_rate):
        print(_describe(commit))
    print(f"\ncarried forward — {HIT_RATE_THRESHOLD:.0%} or more:")
    for commit in sorted(carried, key=lambda c: c.hit_rate):
        print(_describe(commit))
    print(f"\ntoo few markers to score — under {MIN_MARKERS}:")
    for commit in unscored:
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
    behind = {c.sha: c for c in commits if c.scored and not c.carried_forward}

    unrecorded = [behind[sha] for sha in sorted(behind) if sha not in acknowledged]
    stale = sorted(sha for sha in acknowledged if sha not in behind)

    print(_headline(maintenance, development))
    for sha in stale:
        print(f"no longer behind: {sha[:8]} — drop it from {ACKNOWLEDGED_FILE.name}")

    if not unrecorded:
        scored = sum(1 for c in commits if c.scored)
        print(
            f"all {scored} scored commits are carried forward or recorded "
            f"({len(commits) - scored} too small to score)"
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
