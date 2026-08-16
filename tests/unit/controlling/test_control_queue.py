"""Tests for control_queue function in utils/controlling.py."""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.decide import running_kernel_state
from custom_components.better_thermostat.utils.controlling import control_queue


class TestControlQueue:
    """Test control_queue function."""

    @pytest.mark.asyncio
    async def test_creates_task_manager_if_not_exists(self):
        """Test that TaskManager is created if it doesn't exist."""
        mock_self = Mock()
        mock_self.in_maintenance = False
        mock_self.ignore_states = False
        mock_self.startup_running = False
        mock_self.device_name = "test_thermostat"

        # Create a queue that will cancel the loop after first iteration
        queue = asyncio.Queue()
        mock_self.control_queue_task = queue

        # Cancel after queue.get() to exit the loop
        async def cancel_after_get():
            await asyncio.sleep(0.01)
            # Don't put anything, let it hang

        cancel_task = asyncio.create_task(cancel_after_get())

        # Run control_queue in background and cancel it
        queue_task = asyncio.create_task(control_queue(mock_self))
        await asyncio.sleep(0.02)
        queue_task.cancel()

        try:
            await queue_task
        except asyncio.CancelledError:
            pass

        await cancel_task

        # TaskManager should be created
        assert hasattr(mock_self, "task_manager")
        assert mock_self.task_manager is not None

    @pytest.mark.asyncio
    async def test_skips_when_in_maintenance(self):
        """Test that control loop skips when in_maintenance is True."""
        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.in_maintenance = True
        mock_self.ignore_states = False
        mock_self.startup_running = False
        mock_self.calculate_heating_power = AsyncMock()

        queue = asyncio.Queue()
        mock_self.control_queue_task = queue

        # Run control_queue in background
        queue_task = asyncio.create_task(control_queue(mock_self))

        # Let it run for a bit
        await asyncio.sleep(0.05)

        # Cancel the task
        queue_task.cancel()
        try:
            await queue_task
        except asyncio.CancelledError:
            pass

        # calculate_heating_power should never be called
        mock_self.calculate_heating_power.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_ignore_states_true(self):
        """Test that control loop skips when ignore_states is True."""
        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.in_maintenance = False
        mock_self.ignore_states = True
        mock_self.startup_running = False
        mock_self.calculate_heating_power = AsyncMock()

        queue = asyncio.Queue()
        mock_self.control_queue_task = queue

        queue_task = asyncio.create_task(control_queue(mock_self))
        await asyncio.sleep(0.05)
        queue_task.cancel()

        try:
            await queue_task
        except asyncio.CancelledError:
            pass

        mock_self.calculate_heating_power.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_startup_running_true(self):
        """Test that control loop skips when startup_running is True."""
        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.in_maintenance = False
        mock_self.ignore_states = False
        mock_self.startup_running = True
        mock_self.calculate_heating_power = AsyncMock()

        queue = asyncio.Queue()
        mock_self.control_queue_task = queue

        queue_task = asyncio.create_task(control_queue(mock_self))
        await asyncio.sleep(0.05)
        queue_task.cancel()

        try:
            await queue_task
        except asyncio.CancelledError:
            pass

        mock_self.calculate_heating_power.assert_not_called()

    @pytest.mark.asyncio
    async def test_processes_task_from_queue(self):
        """Test that tasks are processed from queue."""
        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.in_maintenance = False
        mock_self.ignore_states = False
        mock_self.startup_running = False
        mock_self.calculate_heating_power = AsyncMock()
        mock_self.cooler_entity_id = None
        mock_self.real_trvs = {}

        queue = asyncio.Queue()
        mock_self.control_queue_task = queue

        # Put a task in the queue
        await queue.put(mock_self)

        queue_task = asyncio.create_task(control_queue(mock_self))

        # Wait for processing
        await asyncio.sleep(0.05)

        queue_task.cancel()
        try:
            await queue_task
        except asyncio.CancelledError:
            pass

        # Should have called calculate_heating_power
        mock_self.calculate_heating_power.assert_called_once()

    @pytest.mark.asyncio
    async def test_calls_calculate_heating_power(self):
        """Test that calculate_heating_power is called during processing."""
        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.in_maintenance = False
        mock_self.ignore_states = False
        mock_self.startup_running = False
        mock_self.calculate_heating_power = AsyncMock()
        mock_self.cooler_entity_id = None
        mock_self.real_trvs = {}

        queue = asyncio.Queue()
        mock_self.control_queue_task = queue
        await queue.put(mock_self)

        queue_task = asyncio.create_task(control_queue(mock_self))
        await asyncio.sleep(0.05)
        queue_task.cancel()

        try:
            await queue_task
        except asyncio.CancelledError:
            pass

        mock_self.calculate_heating_power.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_calculate_heating_power_exception(self):
        """Test that exceptions from calculate_heating_power are caught."""
        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.in_maintenance = False
        mock_self.ignore_states = False
        mock_self.startup_running = False
        mock_self.calculate_heating_power = AsyncMock(
            side_effect=ValueError("Test error")
        )
        mock_self.cooler_entity_id = None
        mock_self.real_trvs = {}

        queue = asyncio.Queue()
        mock_self.control_queue_task = queue
        await queue.put(mock_self)

        queue_task = asyncio.create_task(control_queue(mock_self))
        await asyncio.sleep(0.05)
        queue_task.cancel()

        try:
            await queue_task
        except asyncio.CancelledError:
            pass

        # Should have been called and exception caught
        mock_self.calculate_heating_power.assert_called_once()

    @pytest.mark.asyncio
    async def test_calls_control_cooler_when_exists(self):
        """Test that control_cooler is called when cooler_entity_id exists."""
        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.in_maintenance = False
        mock_self.ignore_states = False
        mock_self.startup_running = False
        mock_self.calculate_heating_power = AsyncMock()
        mock_self.cooler_entity_id = "climate.cooler"
        mock_self.real_trvs = {}

        queue = asyncio.Queue()
        mock_self.control_queue_task = queue
        await queue.put(mock_self)

        with patch(
            "custom_components.better_thermostat.utils.controlling.control_cooler",
            new=AsyncMock(),
        ) as mock_control_cooler:
            queue_task = asyncio.create_task(control_queue(mock_self))
            await asyncio.sleep(0.05)
            queue_task.cancel()

            try:
                await queue_task
            except asyncio.CancelledError:
                pass

            # Should have called control_cooler
            # The cooler runs on the cycle's snapshot (None here: the
            # mocked compute_control_cycle yielded no cycle).
            mock_control_cooler.assert_called_once()
            assert mock_control_cooler.call_args.args[0] is mock_self

    @pytest.mark.asyncio
    async def test_handles_control_cooler_exception(self):
        """Test that exceptions from control_cooler are caught."""
        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.in_maintenance = False
        mock_self.ignore_states = False
        mock_self.startup_running = False
        mock_self.calculate_heating_power = AsyncMock()
        mock_self.cooler_entity_id = "climate.cooler"
        mock_self.real_trvs = {}

        queue = asyncio.Queue()
        mock_self.control_queue_task = queue
        await queue.put(mock_self)

        with patch(
            "custom_components.better_thermostat.utils.controlling.control_cooler",
            new=AsyncMock(side_effect=ValueError("Test error")),
        ) as mock_control_cooler:
            queue_task = asyncio.create_task(control_queue(mock_self))
            await asyncio.sleep(0.05)
            queue_task.cancel()

            try:
                await queue_task
            except asyncio.CancelledError:
                pass

            mock_control_cooler.assert_called_once()

    @pytest.mark.asyncio
    async def test_runs_control_trv_in_parallel(self):
        """Test that control_trv is called for each TRV in parallel."""
        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.in_maintenance = False
        mock_self.ignore_states = False
        mock_self.startup_running = False
        mock_self.calculate_heating_power = AsyncMock()
        mock_self.cooler_entity_id = None
        mock_self.real_trvs = {
            "climate.trv1": {},
            "climate.trv2": {},
            "climate.trv3": {},
        }

        queue = asyncio.Queue()
        mock_self.control_queue_task = queue
        await queue.put(mock_self)

        with patch(
            "custom_components.better_thermostat.utils.controlling.control_trv",
            new=AsyncMock(return_value=True),
        ) as mock_control_trv:
            queue_task = asyncio.create_task(control_queue(mock_self))
            await asyncio.sleep(0.05)
            queue_task.cancel()

            try:
                await queue_task
            except asyncio.CancelledError:
                pass

            # Should have called control_trv 3 times
            assert mock_control_trv.call_count == 3

            # Verify each TRV was processed
            called_trvs = {call[0][1] for call in mock_control_trv.call_args_list}
            assert called_trvs == {"climate.trv1", "climate.trv2", "climate.trv3"}

    @pytest.mark.asyncio
    async def test_handles_control_trv_exceptions(self):
        """Test that exceptions from control_trv are caught and handled."""
        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.in_maintenance = False
        mock_self.ignore_states = False
        mock_self.startup_running = False
        mock_self.calculate_heating_power = AsyncMock()
        mock_self.calculate_heat_loss = AsyncMock()
        mock_self.cooler_entity_id = None
        mock_self.real_trvs = {"climate.trv1": {}, "climate.trv2": {}}

        queue = asyncio.Queue()
        mock_self.control_queue_task = queue
        await queue.put(mock_self)

        call_count = 0

        async def _side_effect(self_arg, entity_id, cycle=None):
            """First TRV raises, second succeeds.  All retries succeed."""
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("Test error")
            return True

        with patch(
            "custom_components.better_thermostat.utils.controlling.control_trv",
            new=AsyncMock(side_effect=_side_effect),
        ):
            queue_task = asyncio.create_task(control_queue(mock_self))
            await asyncio.sleep(0.1)
            queue_task.cancel()

            try:
                await queue_task
            except asyncio.CancelledError:
                pass

            # Both TRVs should have been attempted on first pass
            assert call_count >= 2

    @pytest.mark.asyncio
    async def test_retries_when_result_false(self):
        """Test that task is retried when control_trv returns False.

        When control_trv returns False the queue should call put_nowait
        to schedule a retry.  We verify by counting how many times
        control_trv is called (>1 means the retry was re-queued and consumed).
        """
        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.in_maintenance = False
        mock_self.ignore_states = False
        mock_self.startup_running = False
        mock_self.calculate_heating_power = AsyncMock()
        mock_self.calculate_heat_loss = AsyncMock()
        mock_self.cooler_entity_id = None
        mock_self.real_trvs = {"climate.trv1": {}}

        queue = asyncio.Queue(maxsize=10)
        mock_self.control_queue_task = queue
        await queue.put(mock_self)

        trv_call_count = 0

        async def _trv_side_effect(self_arg, entity_id, cycle=None):
            nonlocal trv_call_count
            trv_call_count += 1
            return False

        with (
            patch(
                "custom_components.better_thermostat.utils.controlling.control_trv",
                new=AsyncMock(side_effect=_trv_side_effect),
            ),
            # The failed-cycle backoff is collapsed so the retry lands
            # inside the window this test waits for.
            patch(
                "custom_components.better_thermostat.utils.controlling.FAILED_CYCLE_BACKOFF_S",
                0,
            ),
        ):
            queue_task = asyncio.create_task(control_queue(mock_self))
            await asyncio.sleep(0.1)

            queue_task.cancel()
            try:
                await queue_task
            except asyncio.CancelledError:
                pass

            # control_trv returning False triggers put_nowait, which re-queues
            # the task.  The loop then consumes it → control_trv is called > 1 time.
            assert trv_call_count > 1

    @pytest.mark.asyncio
    async def test_handles_queue_full_when_retrying(self):
        """Test that QueueFull is handled gracefully when retrying."""
        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.in_maintenance = False
        mock_self.ignore_states = False
        mock_self.startup_running = False
        mock_self.calculate_heating_power = AsyncMock()
        mock_self.cooler_entity_id = None
        mock_self.real_trvs = {"climate.trv1": {}}

        # Create queue with maxsize=1
        queue = asyncio.Queue(maxsize=1)
        mock_self.control_queue_task = queue
        await queue.put(mock_self)

        with (
            patch(
                "custom_components.better_thermostat.utils.controlling.control_trv"
            ) as mock_control_trv,
            # The failed-cycle backoff is collapsed so the retry lands
            # inside the window this test waits for.
            patch(
                "custom_components.better_thermostat.utils.controlling.FAILED_CYCLE_BACKOFF_S",
                0,
            ),
        ):
            mock_control_trv.return_value = False

            queue_task = asyncio.create_task(control_queue(mock_self))
            await asyncio.sleep(0.05)
            queue_task.cancel()

            try:
                await queue_task
            except asyncio.CancelledError:
                pass

            # control_trv returning False re-requests the cycle, which the
            # loop consumes again → control_trv is called more than once.
            assert mock_control_trv.await_count > 1
            # The single slot never overflows: a pending request coalesces
            # instead of being enqueued a second time.
            assert queue.qsize() <= 1

    @pytest.mark.asyncio
    async def test_sets_ignore_states_during_processing(self):
        """Test that ignore_states is set to True during processing."""
        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.in_maintenance = False
        mock_self.ignore_states = False
        mock_self.startup_running = False
        mock_self.calculate_heating_power = AsyncMock()
        mock_self.cooler_entity_id = None
        mock_self.real_trvs = {}

        queue = asyncio.Queue()
        mock_self.control_queue_task = queue

        ignore_states_values = []

        async def capture_ignore_states():
            await asyncio.sleep(0.01)
            ignore_states_values.append(mock_self.ignore_states)

        mock_self.calculate_heating_power.side_effect = capture_ignore_states

        await queue.put(mock_self)

        queue_task = asyncio.create_task(control_queue(mock_self))
        await asyncio.sleep(0.05)
        queue_task.cancel()

        try:
            await queue_task
        except asyncio.CancelledError:
            pass

        # ignore_states should have been True during processing
        assert True in ignore_states_values

    @pytest.mark.asyncio
    async def test_resets_ignore_states_after_processing(self):
        """Test that ignore_states is reset to False after processing."""
        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.in_maintenance = False
        mock_self.ignore_states = False
        mock_self.startup_running = False
        mock_self.calculate_heating_power = AsyncMock()
        mock_self.cooler_entity_id = None
        mock_self.real_trvs = {}

        queue = asyncio.Queue()
        mock_self.control_queue_task = queue
        await queue.put(mock_self)

        queue_task = asyncio.create_task(control_queue(mock_self))
        await asyncio.sleep(0.05)
        queue_task.cancel()

        try:
            await queue_task
        except asyncio.CancelledError:
            pass

        # ignore_states should be False after processing (finally block)
        assert mock_self.ignore_states is False

    @pytest.mark.asyncio
    async def test_finally_block_resets_ignore_states(self):
        """Test that finally block always resets ignore_states."""
        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.in_maintenance = False
        mock_self.ignore_states = True  # Start as True
        mock_self.startup_running = False

        queue = asyncio.Queue()
        mock_self.control_queue_task = queue

        queue_task = asyncio.create_task(control_queue(mock_self))
        await asyncio.sleep(0.01)
        queue_task.cancel()

        try:
            await queue_task
        except asyncio.CancelledError:
            pass

        # Finally block should have reset it
        assert mock_self.ignore_states is False

    @pytest.mark.asyncio
    async def test_does_not_reset_ignore_states_if_in_maintenance(self):
        """Test that ignore_states is not reset if in_maintenance is True."""
        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.ignore_states = True
        mock_self.startup_running = False
        mock_self.calculate_heating_power = AsyncMock()
        mock_self.cooler_entity_id = None
        mock_self.real_trvs = {}

        queue = asyncio.Queue()
        mock_self.control_queue_task = queue

        # Set in_maintenance after starting
        mock_self.in_maintenance = False
        await queue.put(mock_self)

        queue_task = asyncio.create_task(control_queue(mock_self))
        await asyncio.sleep(0.02)

        # Set in_maintenance during processing
        mock_self.in_maintenance = True
        await asyncio.sleep(0.02)

        queue_task.cancel()
        try:
            await queue_task
        except asyncio.CancelledError:
            pass

        # ignore_states should NOT be reset because in_maintenance is True
        # Note: This tests the finally block behavior (lines 135-137)
        assert mock_self.ignore_states is True


class TestControlQueueOnADualRoleEntity:
    """Dispatch of a device named as both a controlled thermostat and the cooler.

    Such a device takes one mode and one setpoint, so exactly one channel
    drives it per cycle. The cooling decision control_cooler latched is what
    says which.
    """

    SHARED_ID = "climate.reversible_ac"
    _CTRL = "custom_components.better_thermostat.utils.controlling"

    @classmethod
    def _make_self(cls, *, hvac_mode_decided, real_trvs=None):
        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.in_maintenance = False
        mock_self.ignore_states = False
        mock_self.startup_running = False
        mock_self.calculate_heating_power = AsyncMock()
        mock_self.calculate_heat_loss = AsyncMock()
        mock_self.cooler_entity_id = cls.SHARED_ID
        mock_self.real_trvs = (
            {cls.SHARED_ID: Mock()} if real_trvs is None else real_trvs
        )
        mock_self._cooler_last_sent = {"hvac_mode_decided": hvac_mode_decided}
        mock_self.control_queue_task = asyncio.Queue()
        # The heartbeat the cycle stamps replaces a field of the kernel state,
        # so it has to be the real immutable value, not a stand-in.
        mock_self.kernel_state = running_kernel_state()
        mock_self.clock = FakeClock()
        return mock_self

    @classmethod
    async def _run_one_cycle(cls, mock_self):
        await mock_self.control_queue_task.put(mock_self)
        with patch(f"{cls._CTRL}.compute_control_cycle", return_value=(Mock(), Mock())):
            queue_task = asyncio.create_task(control_queue(mock_self))
            await asyncio.sleep(0.05)
            queue_task.cancel()
            try:
                await queue_task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_the_heating_channel_stands_down_while_cooling_owns_the_device(self):
        """A cycle the cooling channel drives dispatches no heating control."""
        mock_self = self._make_self(hvac_mode_decided="cool")

        with (
            patch(f"{self._CTRL}.control_cooler", new=AsyncMock()),
            patch(
                f"{self._CTRL}.control_trv", new=AsyncMock(return_value=True)
            ) as mock_control_trv,
        ):
            await self._run_one_cycle(mock_self)

        mock_control_trv.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_heating_channel_drives_the_device_on_every_other_cycle(self):
        """A cooling decision of OFF leaves the device to the heating channel."""
        mock_self = self._make_self(hvac_mode_decided="off")

        with (
            patch(f"{self._CTRL}.control_cooler", new=AsyncMock()),
            patch(
                f"{self._CTRL}.control_trv", new=AsyncMock(return_value=True)
            ) as mock_control_trv,
        ):
            await self._run_one_cycle(mock_self)

        assert mock_control_trv.call_count == 1
        assert mock_control_trv.call_args_list[0][0][1] == self.SHARED_ID

    @pytest.mark.asyncio
    async def test_a_cooling_pass_that_raised_leaves_the_device_to_the_heating_channel(
        self,
    ):
        """A latch no cycle wrote is not read as a handover.

        The cooling pass raised before it decided, so the decision standing
        there belongs to an earlier cycle and says nothing about this one.
        """
        mock_self = self._make_self(hvac_mode_decided="cool")

        with (
            patch(
                f"{self._CTRL}.control_cooler",
                new=AsyncMock(side_effect=ValueError("cooler unreachable")),
            ),
            patch(
                f"{self._CTRL}.control_trv", new=AsyncMock(return_value=True)
            ) as mock_control_trv,
        ):
            await self._run_one_cycle(mock_self)

        assert mock_control_trv.call_count == 1
        assert mock_control_trv.call_args_list[0][0][1] == self.SHARED_ID

    @pytest.mark.asyncio
    async def test_a_failing_trv_is_named_correctly_when_a_device_was_skipped(
        self, caplog
    ):
        """The error names the device that failed, not the one left out.

        The results of the dispatched controls line up with the devices that
        were dispatched, which is a shorter list than the configured ones as
        soon as one of them goes to the cooling channel.
        """
        radiator = "climate.radiator"
        mock_self = self._make_self(
            hvac_mode_decided="cool",
            real_trvs={self.SHARED_ID: Mock(), radiator: Mock()},
        )

        with (
            patch(f"{self._CTRL}.control_cooler", new=AsyncMock()),
            patch(
                f"{self._CTRL}.control_trv",
                new=AsyncMock(side_effect=ValueError("valve unreachable")),
            ),
            caplog.at_level("ERROR"),
        ):
            await self._run_one_cycle(mock_self)

        errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
        assert any(radiator in message for message in errors)
        assert not any(self.SHARED_ID in message for message in errors)

    @pytest.mark.asyncio
    async def test_a_distinct_cooler_leaves_every_trv_dispatched(self):
        """An installation without the overlap takes no new branch."""
        mock_self = self._make_self(
            hvac_mode_decided="cool", real_trvs={"climate.radiator": Mock()}
        )
        mock_self.cooler_entity_id = "climate.split_unit"

        with (
            patch(f"{self._CTRL}.control_cooler", new=AsyncMock()),
            patch(
                f"{self._CTRL}.control_trv", new=AsyncMock(return_value=True)
            ) as mock_control_trv,
        ):
            await self._run_one_cycle(mock_self)

        assert mock_control_trv.call_count == 1
        assert mock_control_trv.call_args_list[0][0][1] == "climate.radiator"

    @pytest.mark.asyncio
    async def test_the_heating_band_still_advances_on_a_cycle_that_dispatches_nothing(
        self,
    ):
        """The hysteresis band is advanced by the cycle, not by a device.

        With the shared device as the room's only one, a cooling cycle
        dispatches no heating control at all, and the band would otherwise rest
        on the state the last heating cycle left it in.
        """
        mock_self = self._make_self(hvac_mode_decided="cool")

        with (
            patch(f"{self._CTRL}.control_cooler", new=AsyncMock()),
            patch(f"{self._CTRL}.control_trv", new=AsyncMock(return_value=True)),
        ):
            await self._run_one_cycle(mock_self)

        mock_self._commit_hvac_action.assert_called_once_with(
            mock_self._compute_hvac_action_pure.return_value
        )

    @pytest.mark.asyncio
    async def test_a_cycle_that_dispatches_nothing_still_counts_as_a_control_cycle(
        self,
    ):
        """The control watchdog reads a cooling run as a running loop.

        Leaving the room's only device to the cooling channel is a decision the
        cycle reached, and a cooling run outlasts the watchdog window easily.
        """
        mock_self = self._make_self(hvac_mode_decided="cool")
        mock_self.clock.advance(1234.0)

        with (
            patch(f"{self._CTRL}.control_cooler", new=AsyncMock()),
            patch(f"{self._CTRL}.control_trv", new=AsyncMock(return_value=True)),
        ):
            await self._run_one_cycle(mock_self)

        assert mock_self.kernel_state.last_control_monotonic == 1234.0
