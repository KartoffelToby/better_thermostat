"""Tests for control_queue function in utils/controlling.py."""

import asyncio
import contextlib
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.climate.const import HVACMode
import pytest

from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.decide import running_kernel_state
from custom_components.better_thermostat.core.recorder import FlightRecorder
from custom_components.better_thermostat.core.snapshot import WorldSnapshot
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.controlling import (
    TaskManager,
    control_queue,
)
from tests.factories import ThermostatStandIn


def _tracked_trv(entity_id: str) -> Trv:
    """Build the record the entity keeps for one controlled TRV."""
    return Trv.from_legacy_dict(entity_id, {})


def _thermostat() -> ThermostatStandIn:
    """Build a heating room at rest that one control cycle can observe."""
    bt = ThermostatStandIn()
    bt.attr_hvac_action = None
    bt.clock = FakeClock()
    bt.kernel_state = running_kernel_state()
    bt.flight_recorder = FlightRecorder()
    bt.bt_hvac_mode = HVACMode.HEAT
    bt.heat_target_temperature = 21.0
    bt.cool_target_temperature = None
    bt.bt_min_temp = 5.0
    bt.bt_max_temp = 30.0
    bt.room_temperature = 20.0
    bt.room_temperature_filtered = None
    bt.temp_slope = None
    bt.call_for_heat = True
    bt.preset_mode = None
    bt.tolerance = 0.3
    bt.outdoor_sensor = None
    bt.weather_entity = None
    bt.calculate_heat_loss = AsyncMock()
    return bt


def _idle_room() -> ThermostatStandIn:
    """Build a room whose loop is free to take the next request."""
    bt = _thermostat()
    bt.device_name = "test_thermostat"
    bt.in_maintenance = False
    bt.ignore_states = False
    bt.startup_running = False
    bt.calculate_heating_power = AsyncMock()
    bt.cooler_entity_id = None
    bt.real_trvs = {}
    return bt


class _HeldPolls:
    """Count the one-second polls a held loop sleeps instead of working."""

    def __init__(self) -> None:
        self.count = 0
        self._patch = None

    def __enter__(self) -> _HeldPolls:
        inner = asyncio.sleep

        async def counting(delay, result=None):
            if delay == 1:
                self.count += 1
            return await inner(delay, result)

        self._patch = patch("asyncio.sleep", new=counting)
        self._patch.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._patch.stop()


async def _stop(task: asyncio.Task) -> None:
    """Cancel the queue consumer and wait until it has unwound."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


class TestControlQueue:
    """Test control_queue function."""

    @pytest.mark.asyncio
    async def test_creates_task_manager_if_not_exists(self):
        """An entity that starts without a task manager gets one."""
        mock_self = _idle_room()
        # The stand-in answers ``task_manager`` with a mock; deleting it makes
        # the entity start without one, which is the case under test.
        del mock_self.task_manager
        mock_self.control_queue_task = asyncio.Queue()

        queue_task = asyncio.create_task(control_queue(mock_self))
        await _wait_until(
            lambda: isinstance(getattr(mock_self, "task_manager", None), TaskManager)
        )
        await _stop(queue_task)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "hold", ["in_maintenance", "ignore_states", "startup_running"]
    )
    async def test_a_held_loop_takes_no_request_until_released(self, hold):
        """A queued request waits while the loop is held, then runs.

        A valve maintenance run, a cycle already in flight and a running
        startup each hold the loop. Once the hold lifts, the same request is
        processed, so it was the hold that kept it waiting.
        """
        mock_self = _idle_room()
        setattr(mock_self, hold, True)
        queue = asyncio.Queue()
        mock_self.control_queue_task = queue
        await queue.put(mock_self)

        with _HeldPolls() as polls:
            queue_task = asyncio.create_task(control_queue(mock_self))
            await _wait_until(lambda: polls.count >= 3)

            assert queue.qsize() == 1
            mock_self.calculate_heating_power.assert_not_called()

            setattr(mock_self, hold, False)
            await asyncio.wait_for(queue.join(), timeout=1)
            await _stop(queue_task)

        mock_self.calculate_heating_power.assert_called_once()

    @pytest.mark.asyncio
    async def test_processes_task_from_queue(self):
        """A queued request runs one control pass."""
        mock_self = _idle_room()
        queue = asyncio.Queue()
        mock_self.control_queue_task = queue
        await queue.put(mock_self)

        queue_task = asyncio.create_task(control_queue(mock_self))
        await asyncio.wait_for(queue.join(), timeout=1)
        await _stop(queue_task)

        mock_self.calculate_heating_power.assert_called_once()
        mock_self.calculate_heat_loss.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_calculate_heating_power_exception(self):
        """A failing heating-power estimate does not end the pass."""
        mock_self = _idle_room()
        mock_self.calculate_heating_power = AsyncMock(
            side_effect=ValueError("Test error")
        )
        queue = asyncio.Queue()
        mock_self.control_queue_task = queue
        await queue.put(mock_self)

        queue_task = asyncio.create_task(control_queue(mock_self))
        await asyncio.wait_for(queue.join(), timeout=1)
        await _stop(queue_task)

        mock_self.calculate_heating_power.assert_called_once()
        mock_self.calculate_heat_loss.assert_called_once()

    @pytest.mark.asyncio
    async def test_calls_control_cooler_when_exists(self):
        """A configured cooler is controlled on the cycle's snapshot."""
        mock_self = _idle_room()
        mock_self.cooler_entity_id = "climate.cooler"
        queue = asyncio.Queue()
        mock_self.control_queue_task = queue
        await queue.put(mock_self)

        with patch(
            "custom_components.better_thermostat.utils.controlling.control_cooler",
            new=AsyncMock(),
        ) as mock_control_cooler:
            queue_task = asyncio.create_task(control_queue(mock_self))
            await asyncio.wait_for(queue.join(), timeout=1)
            await _stop(queue_task)

        mock_control_cooler.assert_called_once()
        assert mock_control_cooler.call_args.args[0] is mock_self
        assert isinstance(mock_control_cooler.call_args.args[1], WorldSnapshot)

    @pytest.mark.asyncio
    async def test_handles_control_cooler_exception(self):
        """A failing cooler pass does not end the pass."""
        mock_self = _idle_room()
        mock_self.cooler_entity_id = "climate.cooler"
        queue = asyncio.Queue()
        mock_self.control_queue_task = queue
        await queue.put(mock_self)

        with patch(
            "custom_components.better_thermostat.utils.controlling.control_cooler",
            new=AsyncMock(side_effect=ValueError("Test error")),
        ) as mock_control_cooler:
            queue_task = asyncio.create_task(control_queue(mock_self))
            await asyncio.wait_for(queue.join(), timeout=1)
            await _stop(queue_task)

        mock_control_cooler.assert_called_once()
        assert mock_self.ignore_states is False

    @pytest.mark.asyncio
    async def test_runs_control_trv_in_parallel(self):
        """Every TRV is controlled once per pass."""
        mock_self = _idle_room()
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
            await asyncio.wait_for(queue.join(), timeout=1)
            await _stop(queue_task)

        called_trvs = [call.args[1] for call in mock_control_trv.call_args_list]
        assert sorted(called_trvs) == ["climate.trv1", "climate.trv2", "climate.trv3"]

    @pytest.mark.asyncio
    async def test_handles_control_trv_exceptions(self):
        """A TRV that raises does not keep the others from being controlled."""
        mock_self = _idle_room()
        mock_self.real_trvs = {
            entity_id: _tracked_trv(entity_id)
            for entity_id in ("climate.trv1", "climate.trv2")
        }
        queue = asyncio.Queue()
        mock_self.control_queue_task = queue
        await queue.put(mock_self)

        attempted = []

        async def _side_effect(self_arg, entity_id, cycle=None):
            """The first TRV raises once; every other call succeeds."""
            attempted.append(entity_id)
            if len(attempted) == 1:
                raise ValueError("Test error")
            return True

        with patch(
            "custom_components.better_thermostat.utils.controlling.control_trv",
            new=AsyncMock(side_effect=_side_effect),
        ):
            queue_task = asyncio.create_task(control_queue(mock_self))
            await _wait_until(
                lambda: {"climate.trv1", "climate.trv2"} <= set(attempted)
            )
            await _stop(queue_task)

    @pytest.mark.asyncio
    async def test_retries_when_result_false(self):
        """A TRV that reports failure gets the cycle requested again."""
        mock_self = _idle_room()
        mock_self.real_trvs = {"climate.trv1": _tracked_trv("climate.trv1")}
        queue = asyncio.Queue(maxsize=10)
        mock_self.control_queue_task = queue
        await queue.put(mock_self)

        with (
            patch(
                "custom_components.better_thermostat.utils.controlling.control_trv",
                new=AsyncMock(return_value=False),
            ) as mock_control_trv,
            # The failed-cycle backoff is collapsed so the retry follows at once.
            patch(
                "custom_components.better_thermostat.utils.controlling.FAILED_CYCLE_BACKOFF_S",
                0,
            ),
        ):
            queue_task = asyncio.create_task(control_queue(mock_self))
            await _wait_until(lambda: mock_control_trv.await_count > 1)
            await _stop(queue_task)

    @pytest.mark.asyncio
    async def test_handles_queue_full_when_retrying(self):
        """A retry coalesces with a pending request in a single-slot queue."""
        mock_self = _idle_room()
        mock_self.real_trvs = {"climate.trv1": _tracked_trv("climate.trv1")}
        queue = asyncio.Queue(maxsize=1)
        mock_self.control_queue_task = queue
        await queue.put(mock_self)

        with (
            patch(
                "custom_components.better_thermostat.utils.controlling.control_trv",
                new=AsyncMock(return_value=False),
            ) as mock_control_trv,
            # The failed-cycle backoff is collapsed so the retry follows at once.
            patch(
                "custom_components.better_thermostat.utils.controlling.FAILED_CYCLE_BACKOFF_S",
                0,
            ),
        ):
            queue_task = asyncio.create_task(control_queue(mock_self))
            await _wait_until(lambda: mock_control_trv.await_count > 2)
            await _stop(queue_task)

        assert queue.qsize() <= 1

    @pytest.mark.asyncio
    async def test_sets_ignore_states_during_processing(self):
        """Inbound device reports are held off while a pass runs."""
        mock_self = _idle_room()
        queue = asyncio.Queue()
        mock_self.control_queue_task = queue

        ignore_states_values = []

        async def capture_ignore_states():
            ignore_states_values.append(mock_self.ignore_states)

        mock_self.calculate_heating_power.side_effect = capture_ignore_states
        await queue.put(mock_self)

        queue_task = asyncio.create_task(control_queue(mock_self))
        await asyncio.wait_for(queue.join(), timeout=1)
        await _stop(queue_task)

        assert ignore_states_values == [True]
        assert mock_self.ignore_states is False

    @pytest.mark.asyncio
    async def test_finally_block_resets_ignore_states(self):
        """Stopping the loop releases a hold on inbound reports."""
        mock_self = _idle_room()
        mock_self.ignore_states = True
        mock_self.control_queue_task = asyncio.Queue()

        with _HeldPolls() as polls:
            queue_task = asyncio.create_task(control_queue(mock_self))
            await _wait_until(lambda: polls.count >= 1)
            await _stop(queue_task)

        assert mock_self.ignore_states is False

    @pytest.mark.asyncio
    async def test_does_not_reset_ignore_states_if_in_maintenance(self):
        """Stopping the loop during maintenance leaves the hold to maintenance.

        The maintenance run set ``ignore_states`` itself and releases it when
        it ends.
        """
        mock_self = _idle_room()
        mock_self.ignore_states = True
        mock_self.in_maintenance = True
        mock_self.control_queue_task = asyncio.Queue()

        with _HeldPolls() as polls:
            queue_task = asyncio.create_task(control_queue(mock_self))
            await _wait_until(lambda: polls.count >= 1)
            await _stop(queue_task)

        assert mock_self.ignore_states is True

    @pytest.mark.asyncio
    async def test_an_item_that_carries_no_cycle_is_still_marked_done(self):
        """An item without a cycle leaves the queue joinable.

        The queue counts an item as unfinished until the worker acknowledges
        it, and ``join`` is what reads that count. An item that asks for no
        control pass still has to be acknowledged, or the count never clears.
        """
        mock_self = _thermostat()
        mock_self.device_name = "test_thermostat"
        mock_self.in_maintenance = False
        mock_self.ignore_states = False
        mock_self.startup_running = False
        mock_self.calculate_heating_power = AsyncMock()
        mock_self.cooler_entity_id = None
        mock_self.real_trvs = {}

        queue = asyncio.Queue()
        mock_self.control_queue_task = queue
        await queue.put(None)

        queue_task = asyncio.create_task(control_queue(mock_self))
        try:
            await asyncio.wait_for(queue.join(), timeout=1)
        finally:
            queue_task.cancel()
            try:
                await queue_task
            except asyncio.CancelledError:
                pass

        # The item asked for no control pass, so none was run.
        mock_self.calculate_heating_power.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_item_cancelled_mid_flight_is_still_marked_done(self):
        """Cancellation lands between taking an item and finishing with it.

        The worker is cancelled when the entity goes away, and it is
        suspended inside the work far more often than between two items. The
        item it had taken stays counted as unfinished until it is
        acknowledged, so a shutdown that waits on that count never returns.
        """
        started = asyncio.Event()

        async def never_finishes():
            started.set()
            await asyncio.Event().wait()

        mock_self = _thermostat()
        mock_self.device_name = "test_thermostat"
        mock_self.in_maintenance = False
        mock_self.ignore_states = False
        mock_self.startup_running = False
        mock_self.calculate_heating_power = never_finishes
        mock_self.cooler_entity_id = None
        mock_self.real_trvs = {}

        queue = asyncio.Queue()
        mock_self.control_queue_task = queue
        await queue.put(["climate.trv"])

        queue_task = asyncio.create_task(control_queue(mock_self))
        await asyncio.wait_for(started.wait(), timeout=1)
        queue_task.cancel()
        try:
            await queue_task
        except asyncio.CancelledError:
            pass

        await asyncio.wait_for(queue.join(), timeout=1)


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
    def _make_self(cls, *, hvac_mode_decided, real_trvs=None):
        mock_self = _thermostat()
        mock_self.device_name = "test_thermostat"
        mock_self.in_maintenance = False
        mock_self.ignore_states = False
        mock_self.startup_running = False
        mock_self.calculate_heating_power = AsyncMock()
        mock_self.calculate_heat_loss = AsyncMock()
        mock_self.cooler_entity_id = cls.SHARED_ID
        mock_self.real_trvs = (
            {cls.SHARED_ID: _tracked_trv(cls.SHARED_ID)}
            if real_trvs is None
            else real_trvs
        )
        mock_self._cooler_last_sent = {"hvac_mode_decided": hvac_mode_decided}
        mock_self.control_queue_task = asyncio.Queue()
        return mock_self

    @classmethod
    async def _run_one_cycle(cls, mock_self, until=None):
        """Run one queued control cycle and stop the loop again.

        Parameters
        ----------
        mock_self : Mock
            the stand-in entity the cycle is queued on and run against
        until : Callable[[], bool] or None
            what marks the cycle under test as finished. A cycle whose TRV
            controls all succeed marks the queued item done and puts nothing
            back, so ``Queue.join`` returns exactly when it completes and None
            selects that wait. A cycle that fails schedules a retry that puts
            the item back, so those cases pass a predicate over what the
            assertions read instead.

        Returns
        -------
        None
        """
        await mock_self.control_queue_task.put(mock_self)
        with patch(f"{cls._CTRL}.compute_control_cycle", return_value=(Mock(), Mock())):
            queue_task = asyncio.create_task(control_queue(mock_self))
            try:
                if until is None:
                    await asyncio.wait_for(
                        mock_self.control_queue_task.join(), timeout=5
                    )
                else:
                    await _wait_until(until)
            finally:
                queue_task.cancel()
                try:
                    await queue_task
                except asyncio.CancelledError:
                    pass

    @pytest.mark.parametrize(
        ("awaiting", "cooler_passes"),
        [
            pytest.param(True, 0, id="awaiting"),
            pytest.param(False, 1, id="initialised"),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_cooler_awaiting_initialization_is_not_controlled(
        self, awaiting, cooler_passes
    ):
        """A cooler that is also a TRV still being set up gets no cooling pass.

        The cooling channel writes a mode and a setpoint to the same device the
        heating channel leaves alone until its initialisation is done.
        """
        mock_self = self._make_self(hvac_mode_decided="heat")
        mock_self.real_trvs[self.SHARED_ID].awaiting_initialization = awaiting

        with (
            patch(f"{self._CTRL}.control_cooler", new=AsyncMock()) as control_cooler,
            patch(f"{self._CTRL}.control_trv", new=AsyncMock(return_value=True)),
        ):
            await self._run_one_cycle(mock_self)

        assert control_cooler.await_count == cooler_passes

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
            real_trvs={
                self.SHARED_ID: _tracked_trv(self.SHARED_ID),
                radiator: _tracked_trv(radiator),
            },
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
            # The cycle fails its only dispatched control and schedules its own
            # retry, so the wait ends on the first failure report and the first
            # pass is what the assertions below are about.
            await self._run_one_cycle(mock_self, until=lambda: bool(_errors()))

        errors = _errors()
        assert any(radiator in message for message in errors)
        assert not any(self.SHARED_ID in message for message in errors)

    @pytest.mark.asyncio
    async def test_a_distinct_cooler_leaves_every_trv_dispatched(self):
        """An installation without the overlap dispatches every thermostat."""
        mock_self = self._make_self(
            hvac_mode_decided="cool",
            real_trvs={"climate.radiator": _tracked_trv("climate.radiator")},
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
