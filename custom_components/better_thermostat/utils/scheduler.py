"""Scheduling facade for the control loop.

All places that want a control cycle — event handlers, the reconciler,
the watchdog, service calls — go through :func:`request_control_cycle`
instead of touching the control queue directly. The queue holds at most
one pending cycle and a cycle builds its snapshot when it starts, so
requests coalesce: a pending request already covers any state change
that arrives before the cycle runs.
"""

import asyncio
import logging

_LOGGER = logging.getLogger(__name__)


def request_control_cycle(self, *, replace_pending: bool = False) -> None:
    """Request one control cycle, coalescing with any pending request.

    Never blocks the caller. With ``replace_pending`` a stale pending
    item is dropped first (window transitions jump the line).
    """
    queue = self.control_queue_task
    if replace_pending and not queue.empty():
        empty_queue(queue)
    try:
        queue.put_nowait(self)
    except asyncio.QueueFull:
        # A cycle is already pending; it will see the new state.
        _LOGGER.debug(
            "better_thermostat %s: control cycle already pending, coalescing",
            self.device_name,
        )


def empty_queue(q: asyncio.Queue) -> None:
    """Empty out a queue of pending items.

    Consumes all pending items from the queue and marks them as done.
    """
    for _ in range(q.qsize()):
        q.get_nowait()
        q.task_done()
