"""A device command its library cancelled is a lost connection, not a cancelled task.

When the Z-Wave JS or Matter connection drops, the client library cancels the
future of every command still in flight (``zwave_js_server`` client.py,
``future.cancel()`` in ``listen``). The service call waiting on it raises
``asyncio.CancelledError`` in a task nobody cancelled. Read as a cancellation,
it ends the task that made the write and slips past every handler that catches
device failures. Read as the lost connection it is, the write is retried and
counted as failed. Only a cancellation of the task itself still ends it.
"""

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import UnitOfTemperature
from homeassistant.core import State
import pytest

from custom_components.better_thermostat.adapters.delegate import set_calibration_offset
from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.decide import running_kernel_state
from custom_components.better_thermostat.core.recorder import FlightRecorder
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.controlling import control_queue
from custom_components.better_thermostat.utils.retry import (
    CommandCancelledError,
    command_cancellation_as_disconnect,
)
from tests.factories import ThermostatStandIn, trv_from_legacy_dict

_CTRL = "custom_components.better_thermostat.utils.controlling"
ENTITY_ID = "climate.trv"


async def _cancelled_by_the_library(*_call):
    """Await a future the way a client library cancels it on a lost connection."""
    future = asyncio.get_running_loop().create_future()
    future.cancel()
    await future


class TestTheConversion:
    """The context manager tells the library's cancellation from the task's own."""

    @pytest.mark.asyncio
    async def test_a_cancelled_command_becomes_a_lost_connection(self):
        """A future cancelled under a running task surfaces as a connection error."""
        with pytest.raises(CommandCancelledError):
            with command_cancellation_as_disconnect():
                await _cancelled_by_the_library()

    @pytest.mark.asyncio
    async def test_a_cancelled_task_stays_cancelled(self):
        """Cancelling the task that waits on the command passes through unchanged."""
        started = asyncio.Event()

        async def wait_on_the_device():
            with command_cancellation_as_disconnect():
                started.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(wait_on_the_device())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()

    @pytest.mark.asyncio
    async def test_a_timeout_still_reads_as_a_timeout(self):
        """``asyncio.timeout`` cancels the task, so its expiry is not converted."""
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0):
                with command_cancellation_as_disconnect():
                    await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_a_trv_write_the_library_cancelled_is_retried_and_fails_cleanly():
    """The delegate retries the cancelled write and answers False once spent."""
    bt = ThermostatStandIn()
    bt.device_name = "Test BT"
    bt.hass = MagicMock()
    trv = Trv(entity_id=ENTITY_ID)
    trv.adapter = MagicMock()
    trv.adapter.set_calibration_offset = AsyncMock(
        side_effect=_cancelled_by_the_library
    )
    bt.real_trvs = {ENTITY_ID: trv}

    async def _skip_delay(_seconds):
        return None

    with (
        patch("asyncio.sleep", new=_skip_delay),
        patch(
            "custom_components.better_thermostat.adapters.delegate."
            "calibration_entity_disabled",
            return_value=False,
        ),
    ):
        result = await set_calibration_offset(bt, ENTITY_ID, 1.0)

    assert result is False
    assert trv.adapter.set_calibration_offset.await_count > 1
    assert "offset" in trv.unreachable_write_channels


def _room() -> ThermostatStandIn:
    """Build a heating room at rest whose loop is free to take a request."""
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
    bt.temperature_slope = None
    bt.call_for_heat = True
    bt.preset_mode = None
    bt.tolerance = 0.3
    bt.outdoor_sensor_entity_id = None
    bt.weather_entity_id = None
    bt.calculate_heat_loss = AsyncMock()
    bt.device_name = "test_thermostat"
    bt.in_maintenance = False
    bt.ignore_states = False
    bt.startup_running = False
    bt.calculate_heating_power = AsyncMock()
    bt.cooler_entity_id = None
    bt.real_trvs = {ENTITY_ID: trv_from_legacy_dict(ENTITY_ID, {})}
    return bt


async def _wait_until(predicate, timeout=5.0):
    """Yield to the loop until ``predicate`` holds."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_trv_pass_that_ended_cancelled_is_counted_as_failed():
    """The cycle is requested again for a TRV whose pass ended cancelled.

    A write outside the delegate can still end a TRV's pass with the
    library's cancellation; the cycle counts it like any other failure.
    """
    bt = _room()
    queue = asyncio.Queue(maxsize=10)
    bt.control_queue_task = queue
    control_trv = AsyncMock(side_effect=[asyncio.CancelledError(), True])

    with (
        patch(f"{_CTRL}.control_trv", new=control_trv),
        # The failed-cycle backoff is collapsed so the retry follows at once.
        patch(f"{_CTRL}.FAILED_CYCLE_BACKOFF_S", 0),
    ):
        loop = asyncio.create_task(control_queue(bt))
        await queue.put(bt)
        await _wait_until(lambda: control_trv.await_count > 1)
        loop.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop

    assert control_trv.await_count == 2


@pytest.mark.asyncio
async def test_cancelling_the_loop_still_ends_it():
    """A cancellation of the loop itself is not mistaken for a failed write."""
    bt = _room()
    queue = asyncio.Queue()
    bt.control_queue_task = queue
    started = asyncio.Event()

    async def hang(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    with patch(f"{_CTRL}.control_trv", new=hang):
        loop = asyncio.create_task(control_queue(bt))
        await queue.put(bt)
        await asyncio.wait_for(started.wait(), timeout=1)
        loop.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(loop, timeout=1)

    assert loop.cancelled()


@pytest.mark.asyncio
async def test_a_tweak_the_library_cancelled_fails_that_trv_not_the_startup():
    """A cancelled initial tweak marks the TRV failed and startup goes on.

    The tweaks write straight to the device's helper entities, outside the
    delegate, and a Z-Wave JS connection that is still settling while Home
    Assistant boots is the likeliest moment for a cancelled command.
    """
    bt = ThermostatStandIn()
    bt.device_name = "Test BT"
    bt.hass = MagicMock()
    bt.hass.config.units.temperature_unit = UnitOfTemperature.CELSIUS
    bt.hass.states.get.return_value = State(
        ENTITY_ID, "heat", {"min_temp": 5.0, "max_temp": 30.0, "temperature": 21.0}
    )
    bt.all_entities = list[str]()
    bt.cooler_entity_id = None
    bt.bt_target_temperature_step = None
    bt._configured_temperature_step = None
    bt.heat_target_temperature = 21.0
    bt.bt_hvac_mode = HVACMode.HEAT
    bt.context = MagicMock()
    bt.real_trvs = {ENTITY_ID: Trv(entity_id=ENTITY_ID, calibration=1)}

    with (
        patch("custom_components.better_thermostat.climate.init", autospec=True),
        patch(
            "custom_components.better_thermostat.climate.initial_tweak",
            autospec=True,
            side_effect=_cancelled_by_the_library,
        ),
        patch(
            "custom_components.better_thermostat.climate.control_trv",
            AsyncMock(return_value=True),
        ),
    ):
        failed = await BetterThermostat._initialize_trvs(bt, [ENTITY_ID])

    assert failed == {ENTITY_ID}
