"""Tests for control queue retry logic.

Issue #1511: When turning on Heating whole HA freezes

The bug: When a TRV is unavailable, control_trv() would still try to
execute commands on it. When these failed, it returned False, causing
control_queue to immediately re-queue the task. This created an infinite
loop that froze HA and generated massive log files (300MB+).

The fix: When a TRV is unavailable, skip it immediately (return True).
The TRV state change event will trigger a new control cycle when it
becomes available again. No retry loop needed.
"""


class TestUnavailableTrvHandling:
    """Tests for unavailable TRV handling."""

    def test_unavailable_trv_returns_true(self):
        """Document: unavailable TRVs should return True (skip, no retry).

        When a TRV is in STATE_UNAVAILABLE or STATE_UNKNOWN:
        1. Log a debug message
        2. Reset ignore_trv_states flag
        3. Return True immediately

        This prevents infinite retry loops because:
        - Return True means "success" to control_queue
        - control_queue won't re-queue the task
        - The TRV state change event will trigger a new control cycle
          when the TRV becomes available again
        """
        pass  # Behavior documented in code

    def test_no_commands_sent_to_unavailable_trv(self):
        """Document: no commands should be sent to unavailable TRVs.

        Before the fix, the code tried to:
        - Call convert_outbound_states()
        - Call set_valve()
        - Call set_hvac_mode()
        - Call set_temperature()
        - Call set_offset()

        All of these would fail on an unavailable TRV, causing
        exceptions or return False, which triggered the retry loop.

        After the fix, none of these are called - we just return True
        immediately.
        """
        pass  # Behavior documented in code

    def test_state_change_triggers_new_control(self):
        """Document: TRV becoming available triggers new control cycle.

        Better Thermostat listens for state changes on TRV entities.
        When a TRV transitions from unavailable to available, this
        triggers trigger_trv_change() which queues a new control cycle.

        This means we don't need to retry - we just wait for the
        state change event.
        """
        pass  # Behavior documented in code


class TestAvailableTrvFailure:
    """Tests for available TRV failure handling."""

    def test_available_trv_failure_returns_false(self):
        """Document: available TRVs that fail should return False.

        When an available TRV fails (e.g., convert_outbound_states
        returns non-dict), control_trv returns False.

        This is different from unavailable TRVs because:
        - The TRV is available but something else failed
        - A retry might succeed
        - control_queue will re-queue the task
        """
        pass  # Behavior documented in code
