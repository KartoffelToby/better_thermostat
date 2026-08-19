"""Tests for the TRVZB setpoint, HVAC mode and valve override quirks."""

import asyncio
import contextlib
import importlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.better_thermostat.trv import Trv

quirk = importlib.import_module("custom_components.better_thermostat.model_fixes.TRVZB")


def _make_self():
    """Create a mock BetterThermostat with a spied service-call layer."""
    mock_self = MagicMock()
    mock_self.device_name = "test_thermostat"
    mock_self.context = MagicMock()
    mock_self.hass.services.async_call = AsyncMock()
    return mock_self


class TestOverrideSetTemperature:
    """The quirk declines so the generic adapter performs the write."""

    @pytest.mark.asyncio
    async def test_returns_false_without_service_call(self):
        """The override returns False and issues no service call."""
        mock_self = _make_self()

        handled = await quirk.override_set_temperature(mock_self, "climate.trv1", 21.0)

        assert handled is False
        mock_self.hass.services.async_call.assert_not_awaited()


class TestOverrideSetHvacMode:
    """The quirk declines so the generic adapter performs the write."""

    @pytest.mark.asyncio
    async def test_returns_false_without_service_call(self):
        """The override returns False and issues no service call."""
        mock_self = _make_self()

        handled = await quirk.override_set_hvac_mode(mock_self, "climate.trv1", "heat")

        assert handled is False
        mock_self.hass.services.async_call.assert_not_awaited()


ENTITY = "climate.trv1"


def _make_valve_self(last_pct=40, *, in_maintenance=False):
    """Create a mock BetterThermostat whose TRV records a commanded valve percent."""
    mock_self = _make_self()
    mock_self.in_maintenance = in_maintenance
    trv_state = Trv(entity_id=ENTITY)
    trv_state.last_valve_percent = last_pct
    mock_self.real_trvs = {ENTITY: trv_state}
    mock_self.hass.async_create_background_task = lambda coro, name=None: (
        asyncio.ensure_future(coro)
    )
    return mock_self, trv_state


async def _settle(task):
    """Cancel a scheduled valve write, if there is one, and wait for it.

    ``Task.cancel()`` only requests cancellation, so a test that ends on it
    leaves the write pending into teardown. ``None`` stands for a call that
    scheduled nothing, which is a state several of these tests assert on.
    """
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.fixture
def writes(monkeypatch):
    """Record every valve percentage the quirk puts on the wire."""
    recorded = []

    async def _write(_self, _entity_id, percent):
        recorded.append(percent)
        return True

    monkeypatch.setattr(quirk, "maybe_set_sonoff_valve_percent", _write)
    return recorded


class TestOverrideSetValve:
    """The de-sticking bump must never cost the requested position."""

    @pytest.mark.asyncio
    async def test_a_close_bumps_open_and_defers_the_target(self, writes):
        """A close drives the valve open first and schedules the target."""
        mock_self, trv_state = _make_valve_self(last_pct=40)

        handled = await quirk.override_set_valve(mock_self, ENTITY, 30)
        task = trv_state.extra.get("_trvzb_valve_bump_task")

        try:
            assert handled is True
            assert writes == [50]
            assert task is not None and not task.done()
        finally:
            await _settle(task)

    @pytest.mark.asyncio
    async def test_a_close_superseding_a_due_bump_writes_the_target(self, writes):
        """A close arriving before the deferred write lands goes out directly."""
        mock_self, trv_state = _make_valve_self(last_pct=40)
        await quirk.override_set_valve(mock_self, ENTITY, 30)
        first_task = trv_state.extra["_trvzb_valve_bump_task"]
        trv_state.last_valve_percent = 30

        handled = await quirk.override_set_valve(mock_self, ENTITY, 20)
        await asyncio.sleep(0)

        assert handled is True
        # 50 is the de-sticking bump of the first close; 20 is the new target.
        # A second bump would drive the valve open again and drop the target.
        assert writes == [50, 20]
        assert first_task.done()
        assert "_trvzb_valve_bump_task" not in trv_state.extra

    @pytest.mark.asyncio
    async def test_repeated_closes_always_land_the_latest_target(
        self, writes, monkeypatch
    ):
        """Closes faster than the delay still put the newest position on the wire."""
        monkeypatch.setattr(quirk, "_TRVZB_CLOSE_BUMP_DELAY_S", 30.0)
        mock_self, trv_state = _make_valve_self(last_pct=40)

        for target in (38, 36, 34, 32):
            await quirk.override_set_valve(mock_self, ENTITY, target)
            trv_state.last_valve_percent = target
        await asyncio.sleep(0)

        await _settle(trv_state.extra.get("_trvzb_valve_bump_task"))

        assert writes[-1] == 32, (
            "the newest requested position never reached the device"
        )
        assert max(writes) == 50, "the valve was driven further open than any bump"

    @pytest.mark.asyncio
    async def test_a_completed_bump_does_not_suppress_the_next_de_stick(
        self, writes, monkeypatch
    ):
        """Once the deferred write has run, the next close bumps again."""
        monkeypatch.setattr(quirk, "_TRVZB_CLOSE_BUMP_DELAY_S", 0.0)
        mock_self, trv_state = _make_valve_self(last_pct=40)

        await quirk.override_set_valve(mock_self, ENTITY, 30)
        await trv_state.extra["_trvzb_valve_bump_task"]
        trv_state.last_valve_percent = 30

        await quirk.override_set_valve(mock_self, ENTITY, 20)
        await _settle(trv_state.extra.get("_trvzb_valve_bump_task"))

        assert writes == [50, 30, 40]

    @pytest.mark.asyncio
    async def test_an_opening_command_writes_directly(self, writes):
        """Opening needs no de-sticking, so the position goes out unchanged."""
        mock_self, trv_state = _make_valve_self(last_pct=40)

        handled = await quirk.override_set_valve(mock_self, ENTITY, 60)

        assert handled is True
        assert writes == [60]
        assert "_trvzb_valve_bump_task" not in trv_state.extra

    @pytest.mark.asyncio
    async def test_valve_maintenance_writes_directly(self, writes):
        """Maintenance drives the valve itself and takes no deferred steps."""
        mock_self, trv_state = _make_valve_self(last_pct=40, in_maintenance=True)

        handled = await quirk.override_set_valve(mock_self, ENTITY, 0)

        assert handled is True
        assert writes == [0]
        assert "_trvzb_valve_bump_task" not in trv_state.extra

    @pytest.mark.asyncio
    async def test_an_unknown_last_position_writes_directly(self, writes):
        """With no recorded position there is nothing to close further from."""
        mock_self, trv_state = _make_valve_self(last_pct=None)

        handled = await quirk.override_set_valve(mock_self, ENTITY, 30)

        assert handled is True
        assert writes == [30]
        assert "_trvzb_valve_bump_task" not in trv_state.extra
