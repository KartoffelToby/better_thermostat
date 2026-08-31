"""Tests for control_queue function in utils/controlling.py."""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.controlling import control_queue


def _tracked_trv(entity_id: str) -> Trv:
    """Build the record the entity keeps for one controlled TRV."""
    return Trv.from_legacy_dict(entity_id, {})


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
            mock_control_cooler.assert_called_once_with(mock_self)

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
            entity_id: _tracked_trv(entity_id)
            for entity_id in ("climate.trv1", "climate.trv2", "climate.trv3")
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
        mock_self.real_trvs = {
            entity_id: _tracked_trv(entity_id)
            for entity_id in ("climate.trv1", "climate.trv2")
        }

        queue = asyncio.Queue()
        mock_self.control_queue_task = queue
        await queue.put(mock_self)

        call_count = 0

        async def _side_effect(self_arg, entity_id):
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
        mock_self.real_trvs = {"climate.trv1": _tracked_trv("climate.trv1")}

        queue = asyncio.Queue(maxsize=10)
        mock_self.control_queue_task = queue
        await queue.put(mock_self)

        trv_call_count = 0

        async def _trv_side_effect(self_arg, entity_id):
            nonlocal trv_call_count
            trv_call_count += 1
            return False

        with patch(
            "custom_components.better_thermostat.utils.controlling.control_trv",
            new=AsyncMock(side_effect=_trv_side_effect),
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
        mock_self.real_trvs = {"climate.trv1": _tracked_trv("climate.trv1")}

        # Create queue with maxsize=1
        queue = asyncio.Queue(maxsize=1)
        mock_self.control_queue_task = queue
        await queue.put(mock_self)

        with patch(
            "custom_components.better_thermostat.utils.controlling.control_trv"
        ) as mock_control_trv:
            mock_control_trv.return_value = False

            queue_task = asyncio.create_task(control_queue(mock_self))
            await asyncio.sleep(0.05)
            queue_task.cancel()

            try:
                await queue_task
            except asyncio.CancelledError:
                pass

            # Should not crash despite queue being full

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


async def _wait_until(predicate, timeout=5.0):
    """Yield to the event loop until a predicate holds.

    Parameters
    ----------
    predicate : Callable[[], bool]
        the condition the caller is waiting for. It is re-read on every loop
        iteration, so the wait ends on the first pass through the loop that
        satisfies it rather than after a fixed budget.
    timeout : float
        the wall-clock ceiling the wait may not exceed, in seconds. Reaching
        it means the condition never came true, which is a failure rather than
        a slow machine.

    Returns
    -------
    None
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(0)


class TestControlQueueOnADualRoleEntity:
    """Dispatch of a device named as both a controlled thermostat and the cooler.

    Such a device takes one mode and one setpoint, so exactly one channel
    drives it per cycle. The cooling decision control_cooler latched is what
    says which.
    """

    SHARED_ID = "climate.reversible_ac"
    _CTRL = "custom_components.better_thermostat.utils.controlling"

    @classmethod
    def _make_self(cls, *, last_cooler_mode_decided, real_trvs=None):
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
        mock_self.last_cooler_mode_decided = last_cooler_mode_decided
        mock_self.control_queue_task = asyncio.Queue()
        return mock_self

    @staticmethod
    async def _run_one_cycle(mock_self, until=None):
        """Run one queued control cycle and stop the loop again.

        Parameters
        ----------
        mock_self : Mock
            the stand-in entity the cycle is queued on and run against
        until : Callable[[], bool] or None
            what marks the cycle under test as finished. A cycle whose TRV
            controls all succeed marks the queued item done and puts nothing
            back, so ``Queue.join`` returns exactly when it completes and None
            selects that wait. A cycle that re-queues itself for a retry keeps
            the queue permanently unfinished, so those cases pass a predicate
            over what the assertions read instead.

        Returns
        -------
        None
        """
        await mock_self.control_queue_task.put(mock_self)
        queue_task = asyncio.create_task(control_queue(mock_self))
        try:
            if until is None:
                await asyncio.wait_for(mock_self.control_queue_task.join(), timeout=5)
            else:
                await _wait_until(until)
        finally:
            queue_task.cancel()
            try:
                await queue_task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_the_heating_channel_stands_down_while_cooling_owns_the_device(self):
        """A cycle the cooling channel drives dispatches no heating control."""
        mock_self = self._make_self(last_cooler_mode_decided="cool")

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
        mock_self = self._make_self(last_cooler_mode_decided="off")

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
        mock_self = self._make_self(last_cooler_mode_decided="cool")

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
            last_cooler_mode_decided="cool",
            real_trvs={self.SHARED_ID: Mock(), radiator: Mock()},
        )

        def _errors():
            return [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]

        with (
            patch(f"{self._CTRL}.control_cooler", new=AsyncMock()),
            patch(
                f"{self._CTRL}.control_trv",
                new=AsyncMock(side_effect=ValueError("valve unreachable")),
            ),
            caplog.at_level("ERROR"),
        ):
            # The cycle fails its only dispatched control and re-queues itself
            # before it marks the taken item done, so the queue never drains
            # and the first pass is what the assertions below are about.
            await self._run_one_cycle(mock_self, until=lambda: bool(_errors()))

        errors = _errors()
        assert any(radiator in message for message in errors)
        assert not any(self.SHARED_ID in message for message in errors)

    @pytest.mark.asyncio
    async def test_a_distinct_cooler_leaves_every_trv_dispatched(self):
        """An installation without the overlap dispatches every thermostat."""
        mock_self = self._make_self(
            last_cooler_mode_decided="cool", real_trvs={"climate.radiator": Mock()}
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
        mock_self = self._make_self(last_cooler_mode_decided="cool")

        with (
            patch(f"{self._CTRL}.control_cooler", new=AsyncMock()),
            patch(f"{self._CTRL}.control_trv", new=AsyncMock(return_value=True)),
        ):
            await self._run_one_cycle(mock_self)

        mock_self._commit_hvac_action.assert_called_once_with(
            mock_self._compute_hvac_action_pure.return_value
        )
