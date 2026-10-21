"""Drift guard binding the benchmark's input stage to production.

``input_stage.RoomTemperatureInput`` re-implements the acceptance gate of
``events/temperature.trigger_temperature_change`` and the EMA of
``_update_room_temperature_ema``, whose thresholds are locals and entity
attributes rather than importable constants. This test drives the real
production handler through a thermostat stand-in with the same reading
sequence as the benchmark stage and asserts both commit the same value
after every reading, so retuning the production gate without the
benchmark fails here. Production's debounce and plateau timers run on a
fake scheduler that fires them, in order, once their time has come.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import inspect
import math
from typing import Any
from unittest.mock import MagicMock, patch

from homeassistant.core import State
from homeassistant.util import dt as dt_util
import pytest

from custom_components.better_thermostat.core.decide import running_kernel_state
from custom_components.better_thermostat.events.temperature import (
    _update_room_temperature_ema,
    trigger_temperature_change,
)
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.const import CONF_HOMEMATICIP
from tests.benchmark.input_stage import EMA_TAU_S, RoomTemperatureInput
from tests.factories import ThermostatStandIn

_SENSOR = "sensor.room"
_MODULE = "custom_components.better_thermostat.events.temperature"

# Readings in °C: sub-threshold moves, accumulations, flickers back onto
# the committed value, significant steps, a direction flip, a reading
# inside the 5 s debounce, and a small move that holds into the plateau.
_SEQUENCE: list[tuple[float, float]] = [
    (0.0, 20.00),
    (60.0, 20.05),
    (120.0, 20.06),
    (180.0, 20.10),
    (240.0, 20.06),
    (300.0, 20.10),
    (360.0, 20.30),
    (363.0, 20.50),
    (420.0, 20.45),
    (480.0, 20.40),
    (540.0, 20.32),
    (600.0, 20.31),
    (660.0, 20.10),
    (720.0, 20.12),
    (780.0, 20.15),
    (840.0, 20.10),
    (900.0, 20.16),
    (1080.0, 20.17),
    (1200.0, 20.40),
]


def _thermostat() -> ThermostatStandIn:
    bt = ThermostatStandIn()
    bt.kernel_state = running_kernel_state()
    bt.hass = MagicMock()
    bt.device_name = "drift-guard"
    bt.sensor_entity_id = _SENSOR
    bt.room_temperature = None
    bt.prev_stable_temperature = None
    bt.last_change_direction = 0
    bt.last_known_external_temperature = None
    bt.last_external_sensor_change = None
    bt.room_temperature_ema_tau_seconds = 300.0
    bt._room_temperature_ema_monotonic = None
    bt.room_temperature_ema = None
    bt.room_temperature_filtered = None
    bt.accum_delta = 0.0
    bt.accum_dir = 0
    bt.pending_temperature = None
    bt.pending_since = None
    bt.plateau_timer_cancel = None
    bt._temperature_filter_lock = None
    bt.in_maintenance = False
    bt._control_needed_after_maintenance = False
    bt.startup_running = False
    bt.is_removed = False
    bt.control_queue_task = MagicMock()
    bt.async_write_ha_state = MagicMock()
    bt.all_trvs = [{"advanced": {CONF_HOMEMATICIP: False}}]
    bt.real_trvs = dict[str, Trv]()
    return bt


def _event(previous: float | None, value: float) -> MagicMock:
    event = MagicMock()
    event.data = {
        "old_state": None if previous is None else State(_SENSOR, f"{previous:.2f}"),
        "new_state": State(_SENSOR, f"{value:.2f}"),
    }
    return event


class _Scheduler:
    """Stand-in for ``async_call_later`` on a simulated clock."""

    def __init__(self, clock: dict[str, float], now: Callable[[], datetime]) -> None:
        self._clock = clock
        self._now = now
        self._due: list[tuple[float, int, Callable[[datetime], Any]]] = []
        self._next_id = 0
        self.spawned: list[Any] = []

    def call_later(
        self, _hass: object, delay: float, action: Callable[[datetime], Any]
    ) -> Callable[[], None]:
        handle = self._next_id
        self._next_id += 1
        self._due.append((self._clock["t"] + delay, handle, action))

        def _cancel() -> None:
            self._due = [d for d in self._due if d[1] != handle]

        return _cancel

    async def run_until(self, t: float) -> None:
        """Fire every timer due by ``t``, at its own time."""
        while self._due:
            self._due.sort(key=lambda d: (d[0], d[1]))
            due_t, _, action = self._due[0]
            if due_t > t:
                break
            self._due.pop(0)
            self._clock["t"] = due_t
            result = action(self._now())
            if inspect.isawaitable(result):
                await result
            while self.spawned:
                await self.spawned.pop(0)
        self._clock["t"] = t


@pytest.mark.asyncio
async def test_the_benchmark_gate_commits_what_production_commits() -> None:
    """After every reading both stages hold the same committed value."""
    bt = _thermostat()
    stage = RoomTemperatureInput()
    start = datetime(2026, 1, 1, tzinfo=dt_util.UTC)
    clock = {"t": 0.0}

    def _now() -> datetime:
        return start + timedelta(seconds=clock["t"])

    scheduler = _Scheduler(clock, _now)
    bt._spawn_owned = lambda coro, name=None: scheduler.spawned.append(coro)
    sensor_state: dict[str, State] = {}
    bt.hass.states.get = sensor_state.get

    previous: float | None = None
    with (
        patch(f"{_MODULE}.dt_util.now", side_effect=_now),
        patch(f"{_MODULE}.monotonic", side_effect=lambda: clock["t"]),
        patch(f"{_MODULE}.async_call_later", side_effect=scheduler.call_later),
        patch(f"{_MODULE}.ir"),
    ):
        for t, value in _SEQUENCE:
            await scheduler.run_until(t)
            if bt.last_external_sensor_change is None:
                bt.last_external_sensor_change = _now() - timedelta(days=1)
            sensor_state[_SENSOR] = State(_SENSOR, f"{value:.2f}")
            await trigger_temperature_change(bt, _event(previous, value))
            stage.update(t, value)
            previous = value
            assert stage.room_temperature == pytest.approx(bt.room_temperature), (
                f"t={t:.0f} s, reading {value:.2f}: production committed "
                f"{bt.room_temperature}, benchmark {stage.room_temperature}"
            )


def test_the_benchmark_ema_has_the_production_time_constant() -> None:
    """One minute of production's EMA moves as far as EMA_TAU_S predicts."""
    bt = _thermostat()
    clock = {"t": 0.0}
    with patch(f"{_MODULE}.monotonic", side_effect=lambda: clock["t"]):
        _update_room_temperature_ema(bt, 20.0)
        clock["t"] = 60.0
        ema = _update_room_temperature_ema(bt, 21.0)
    assert ema == pytest.approx(20.0 + (1.0 - math.exp(-60.0 / EMA_TAU_S)))
