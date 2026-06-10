"""Tests for the control-cycle scheduling facade."""

import asyncio
from unittest.mock import MagicMock

import pytest

from custom_components.better_thermostat.utils.scheduler import (
    empty_queue,
    request_control_cycle,
)


def _make_bt() -> MagicMock:
    bt = MagicMock()
    bt.device_name = "Test BT"
    bt.control_queue_task = asyncio.Queue(maxsize=1)
    return bt


class TestRequestControlCycle:
    """Requests enqueue once and coalesce afterwards."""

    def test_enqueues_the_entity_on_an_empty_queue(self):
        """A request on an idle queue enqueues the entity."""
        bt = _make_bt()
        request_control_cycle(bt)
        assert bt.control_queue_task.get_nowait() is bt

    def test_coalesces_when_a_cycle_is_already_pending(self):
        """A second request neither blocks nor raises; one item remains."""
        bt = _make_bt()
        request_control_cycle(bt)
        request_control_cycle(bt)
        assert bt.control_queue_task.qsize() == 1

    def test_replace_pending_drops_the_stale_item(self):
        """With replace_pending the fresh request supersedes a queued one."""
        bt = _make_bt()
        bt.control_queue_task.put_nowait("stale")
        request_control_cycle(bt, replace_pending=True)
        assert bt.control_queue_task.qsize() == 1
        assert bt.control_queue_task.get_nowait() is bt

    def test_replace_pending_on_an_empty_queue_just_enqueues(self):
        """replace_pending on an empty queue behaves like a plain request."""
        bt = _make_bt()
        request_control_cycle(bt, replace_pending=True)
        assert bt.control_queue_task.get_nowait() is bt


@pytest.mark.asyncio
async def test_empty_queue_drains_and_marks_done():
    """empty_queue consumes every pending item and marks it done."""
    queue = asyncio.Queue()
    queue.put_nowait(1)
    queue.put_nowait(2)
    empty_queue(queue)
    assert queue.empty()
    # join() must not block after the drain: all items were marked done.
    await asyncio.wait_for(queue.join(), timeout=1)
