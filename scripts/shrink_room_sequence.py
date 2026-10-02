r"""Shrink a failing room event sequence to the events that still break its rule.

``tests/integration/test_room_event_sequences.py`` ends every failure message
with a ``BT_ROOM_REPLAY='...'`` line. Pass that line, or the JSON inside it:

    uv run python scripts/shrink_room_sequence.py "BT_ROOM_REPLAY='{...}'"

The script replays the sequence once to learn which rule it breaks, then
drops events and keeps every drop after which the replay still
breaks that same rule. A drop that makes a later event impossible skips the
replay, and a sequence that breaks a different rule is a different finding;
neither counts as still failing. Runs of events are tried before single
ones, so a pair that can only go together goes. It stops when no single
event can go and prints the shorter sequence with its own replay line.

Every replay starts Home Assistant afresh, a few seconds each, and a
sequence of eight events takes a few dozen of them at most.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
REPLAY_TEST = (
    "tests/integration/test_room_event_sequences.py"
    "::test_a_replayed_sequence_keeps_every_rule"
)
RULE = re.compile(
    r"^E\s+(?:AssertionError:\s+)?\[(intent|convergence|grid|bulkhead|settle|surfaces)\]",
    re.MULTILINE,
)
PREFIX = "BT_ROOM_REPLAY="


def parse_sequence(text: str) -> dict[str, Any]:
    """Return the sequence a replay line or its bare JSON carries."""
    text = text.strip()
    if text.startswith(PREFIX):
        text = text[len(PREFIX) :]
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1]
    sequence = json.loads(text)
    if not isinstance(sequence, dict) or {"room", "events"} - sequence.keys():
        raise ValueError("a sequence names its room and lists its events")
    return sequence


def replay_line(sequence: dict[str, Any]) -> str:
    """Return the environment setting that plays ``sequence``."""
    return f"{PREFIX}'{json.dumps(sequence)}'"


def broken_rule_in(output: str) -> str | None:
    """Return the first rule a test run's output reports as broken.

    Only a failure message counts: a line pytest marks with ``E`` whose text
    opens with the rule's tag. Source lines in the traceback can spell out
    the tags of other checks and are not read.
    """
    match = RULE.search(output)
    return match.group(1) if match else None


def shrink[T](events: list[T], still_breaks: Callable[[list[T]], bool]) -> list[T]:
    """Drop runs of events for as long as ``still_breaks`` holds for the rest.

    Runs are tried from half the sequence, rounded up, down to single events,
    each at every position, and again from half after every drop, because a
    drop can bring together two events that were apart. A run lets two events
    go together that cannot go one at a time, such as a head dropping off the
    air and coming back, where the return is impossible without the drop. The
    result is a sequence from which no single event can be dropped. Two
    events that only matter together are kept.
    """
    current = list(events)
    size = _half(current)
    while True:
        for start in range(len(current) - size + 1):
            candidate = current[:start] + current[start + size :]
            if candidate and still_breaks(candidate):
                current = candidate
                size = _half(current)
                break
        else:
            if size == 1:
                return current
            size //= 2


def _half(events: list) -> int:
    """Return half the length of ``events``, rounded up, and at least one."""
    return max(1, (len(events) + 1) // 2)


def _count(events: list) -> str:
    """Return how many events there are, in words."""
    return f"{len(events)} event" + ("" if len(events) == 1 else "s")


def run_replay(sequence: dict[str, Any]) -> str | None:
    """Replay ``sequence`` once and return the rule it breaks, if any."""
    environment = {**os.environ, "BT_ROOM_REPLAY": json.dumps(sequence)}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            REPLAY_TEST,
            "-q",
            "-p",
            "no:cacheprovider",
            "--show-capture=no",
            "--tb=short",
        ],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 1:
        return None
    return broken_rule_in(result.stdout)


def main() -> None:
    """Shrink the sequence given on the command line and print the result."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("sequence", help="a BT_ROOM_REPLAY line or its JSON")
    args = parser.parse_args()

    sequence = parse_sequence(args.sequence)
    rule = run_replay(sequence)
    if rule is None:
        sys.exit("the sequence breaks no rule when replayed; nothing to shrink")
    print(f"breaks [{rule}] with {_count(sequence['events'])}, shrinking ...")

    def still_breaks(events: list[dict[str, Any]]) -> bool:
        return run_replay({**sequence, "events": events}) == rule

    events = shrink(sequence["events"], still_breaks)
    shrunk = {**sequence, "events": events}
    print(f"breaks [{rule}] with {_count(events)}:")
    for number, event in enumerate(events, 1):
        print(f"  {number}. {json.dumps(event)}")
    print(replay_line(shrunk))


if __name__ == "__main__":
    main()
