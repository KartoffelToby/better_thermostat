"""Tests for scripts/shrink_room_sequence.py.

The replay itself starts Home Assistant and is driven by the integration
module; what is pinned here is the part that decides what a shorter sequence
is and when it still counts as the same finding.
"""

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "shrink_room_sequence.py"

_spec = importlib.util.spec_from_file_location("shrink_room_sequence", SCRIPT)
assert _spec is not None and _spec.loader is not None
shrinker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shrinker)

SEQUENCE = {
    "room": "single_head",
    "events": [
        {"kind": "command", "value": 20.5},
        {"kind": "turn", "head": 0, "value": 22.0},
    ],
}


def test_a_replay_line_is_read_as_the_sequence_it_carries():
    """The line a failure message prints parses back to the same sequence."""
    assert shrinker.parse_sequence(shrinker.replay_line(SEQUENCE)) == SEQUENCE


def test_the_bare_json_is_read_as_well():
    """The JSON alone is enough, without the variable name around it."""
    assert shrinker.parse_sequence(json.dumps(SEQUENCE)) == SEQUENCE


def test_a_sequence_without_its_room_is_refused():
    """A sequence that names no room cannot be replayed."""
    with pytest.raises(ValueError):
        shrinker.parse_sequence(json.dumps({"events": []}))


def test_the_first_rule_in_the_output_is_the_one_broken():
    """A message naming several rules counts as breaking the first."""
    output = "E  [convergence] heads carry ...\nE  [intent] the room's target"
    assert shrinker.broken_rule_in(output) == "convergence"


def test_the_rule_comes_from_the_message_not_the_source_above_it():
    """Source lines above the message can name other rules; they do not count.

    The output is what pytest prints for a failed ``[convergence]`` check
    when the traceback shows the rule function's source, which spells out
    the ``[intent]`` check before it.
    """
    output = (
        "    async def assert_rules(room, before):\n"
        "        assert await wait_for(...), (\n"
        '            f"[intent] the room\'s target is {bt.bt_target_temp}"\n'
        "        )\n"
        ">       assert await wait_for(...), (\n"
        '            "[convergence] reachable heads carry "\n'
        "        )\n"
        "E       AssertionError: [convergence] reachable heads carry head 0: 21.0\n"
        "E         room single_head:\n"
        "E       assert False\n"
    )
    assert shrinker.broken_rule_in(output) == "convergence"


def test_output_naming_no_rule_breaks_none():
    """A failure that is not a rule, a crash in setup say, is no finding."""
    assert shrinker.broken_rule_in("E  AssertionError: something else") is None


def test_events_that_do_not_matter_are_dropped():
    """Only the two events the failure needs, in their order, are left."""

    def still_breaks(events):
        return "a" in events and "c" in events and events.index("a") < events.index("c")

    assert shrinker.shrink(list("xaybzc"), still_breaks) == ["a", "c"]


def test_a_pair_that_can_only_go_together_goes():
    """A return without its drop is impossible, so the two go as one run.

    Dropping either alone leaves a sequence that cannot be replayed, which
    does not count as still failing.
    """

    def still_breaks(events):
        if ("drop" in events) != ("back" in events):
            return False
        return "fail" in events

    assert shrinker.shrink(["drop", "back", "fail"], still_breaks) == ["fail"]


def test_a_pair_brought_together_by_a_drop_still_goes():
    """Dropping the event between a drop and a return lets the pair go next.

    The first pass over runs of two finds nothing, a single drop then brings
    the pair together, and runs are tried again from half after it.
    """

    def still_breaks(events):
        return ("drop" in events) == ("back" in events) and "fail" in events

    assert shrinker.shrink(["drop", "noise", "back", "fail"], still_breaks) == ["fail"]


def test_a_sequence_that_needs_every_event_is_kept_whole():
    """Nothing is dropped when every drop loses the failure."""
    assert shrinker.shrink(["a", "b"], lambda events: events == ["a", "b"]) == [
        "a",
        "b",
    ]


def test_the_empty_sequence_is_not_a_candidate():
    """The empty sequence is not offered: it replays nothing."""
    offered = []

    def still_breaks(events):
        offered.append(list(events))
        return True

    assert shrinker.shrink(["a"], still_breaks) == ["a"]
    assert [] not in offered
