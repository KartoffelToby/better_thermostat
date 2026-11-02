"""Orthogonal finite state machines of the decision kernel.

Each region is a frozen dataclass plus a pure ``step()`` function;
timers are expressed through the monotonic timestamps carried in the
state and the ``now`` passed by the caller — no clocks are read here.
"""
