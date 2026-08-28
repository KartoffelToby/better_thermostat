"""What the delegate does when a write or an import does not go through.

The delegate sits between the control cycle and one ecosystem's adapter,
and it answers two different questions with the same ``False``: the device
has no channel for this command, or the command was attempted and failed.
Only the second one is worth another attempt, and only the second one is
worth telling anybody about. Keeping them apart is what makes the retry
around the write reachable at all, and what keeps an unreachable device
out of the silence a missing channel belongs in.
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.better_thermostat.adapters import delegate
from custom_components.better_thermostat.adapters.base import AdapterCapabilities
from custom_components.better_thermostat.trv import Trv

ENTITY_ID = "climate.trv"
VALVE_ENTITY = "number.trv_valve_position"

_DELEGATE = "custom_components.better_thermostat.adapters.delegate"
_RETRY = "custom_components.better_thermostat.utils.retry"

# Attempts one write is worth: the first one plus the delegate's retries.
ATTEMPTS = 6


def _thermostat(adapter, quirks=None):
    """A thermostat with one TRV whose valve channel is ready to write to."""
    thermostat = MagicMock()
    thermostat.device_name = "Test BT"
    trv = Trv(entity_id=ENTITY_ID)
    trv.valve_position_entity = VALVE_ENTITY
    trv.valve_position_writable = True
    trv.adapter = adapter
    trv.model_quirks = quirks if quirks is not None else MagicMock(spec=[])
    thermostat.real_trvs = {ENTITY_ID: trv}
    return thermostat


def _valve_adapter(**kwargs):
    """An adapter that declares a valve channel and writes through it."""
    return SimpleNamespace(
        CAPABILITIES=AdapterCapabilities(offset_write=True, valve_write=True),
        set_valve=AsyncMock(**kwargs),
    )


class TestAValveWriteThatFails:
    """An unreachable device is worth another attempt, and worth reporting."""

    @pytest.mark.asyncio
    async def test_a_write_that_raises_is_attempted_again(self):
        """One dropped Zigbee message must not cost the whole cycle.

        The write is the only part of the call that a second attempt can
        change, so the failure has to reach the retry around it.
        """
        adapter = _valve_adapter(side_effect=ConnectionError("device unreachable"))
        thermostat = _thermostat(adapter)

        with patch(f"{_RETRY}.asyncio.sleep", new=AsyncMock()):
            answer = await delegate.set_valve(thermostat, ENTITY_ID, 50)

        assert answer is False
        assert adapter.set_valve.await_count == ATTEMPTS

    @pytest.mark.asyncio
    async def test_a_write_through_a_model_quirk_is_attempted_again(self):
        """A quirk drives the same wire, so it is worth the same attempts."""
        quirks = SimpleNamespace(
            override_set_valve=AsyncMock(side_effect=OSError("bus error"))
        )
        adapter = _valve_adapter(return_value=None)
        thermostat = _thermostat(adapter, quirks=quirks)

        with patch(f"{_RETRY}.asyncio.sleep", new=AsyncMock()):
            answer = await delegate.set_valve(thermostat, ENTITY_ID, 50)

        assert answer is False
        assert quirks.override_set_valve.await_count == ATTEMPTS

    @pytest.mark.asyncio
    async def test_a_write_nobody_could_make_is_reported(self, caplog):
        """A radiator stuck at the wrong position leaves a trace to follow."""
        adapter = _valve_adapter(side_effect=ConnectionError("device unreachable"))
        thermostat = _thermostat(adapter)

        with (
            caplog.at_level(logging.DEBUG),
            patch(f"{_RETRY}.asyncio.sleep", new=AsyncMock()),
        ):
            await delegate.set_valve(thermostat, ENTITY_ID, 50)

        assert [
            record.getMessage()
            for record in caplog.records
            if record.name == _DELEGATE and record.levelno >= logging.WARNING
        ]


class TestAValveCommandThatGoesNowhere:
    """A command with no channel to take it is not a failure to retry."""

    @pytest.mark.asyncio
    async def test_an_undeclared_valve_channel_costs_no_attempt(self):
        """No number of attempts turns a missing channel into one."""
        adapter = SimpleNamespace(
            CAPABILITIES=AdapterCapabilities(offset_write=True, valve_write=False),
            set_valve=AsyncMock(),
        )
        thermostat = _thermostat(adapter)

        answer = await delegate.set_valve(thermostat, ENTITY_ID, 50)

        assert answer is False
        adapter.set_valve.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_position_that_is_not_a_number_is_refused(self):
        """A value no attempt can fix is not put on the wire."""
        adapter = _valve_adapter(return_value=None)
        thermostat = _thermostat(adapter)

        answer = await delegate.set_valve(thermostat, ENTITY_ID, "half open")

        assert answer is False
        adapter.set_valve.assert_not_awaited()


class TestAnAdapterThatCannotBeImported:
    """The generic adapter catches every ecosystem, including a broken one."""

    @pytest.mark.asyncio
    async def test_the_import_error_is_logged_with_its_traceback(self, caplog):
        """A broken adapter module reads as an unsupported ecosystem.

        Both end up on the generic adapter and both say so at INFO, so the
        import error itself is the only thing that tells them apart.
        """
        generic = SimpleNamespace(name="generic")
        thermostat = MagicMock()
        thermostat.device_name = "Test BT"
        imports = AsyncMock(
            side_effect=[ImportError("cannot import name draft_mode"), generic]
        )

        with (
            caplog.at_level(logging.DEBUG),
            patch(f"{_DELEGATE}.async_import_module", imports),
        ):
            adapter = await delegate.load_adapter(thermostat, "tado", ENTITY_ID)

        assert adapter is generic
        assert "cannot import name draft_mode" in caplog.text
