"""What the delegate does when a valve write does not go through.

The delegate sits between the control cycle and one ecosystem's adapter,
and it tells two outcomes apart: the device has no channel for this command
(``None``), or the command was attempted and failed (``False``). Only the
second one is worth another attempt, and only the second one is worth
telling anybody about.
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.better_thermostat.adapters import delegate
from custom_components.better_thermostat.trv import Trv

ENTITY_ID = "climate.trv"
VALVE_ENTITY = "number.trv_valve_position"

_DELEGATE = "custom_components.better_thermostat.adapters.delegate"
_RETRY = "custom_components.better_thermostat.utils.retry"

# Attempts one write is worth: the first one plus the delegate's retries.
ATTEMPTS = 6


@pytest.fixture(autouse=True)
def _no_helper_entity_is_disabled():
    """The entity registry marks none of the TRV's helper entities disabled.

    The stand-in Home Assistant carries no registry of its own; an empty
    one answers every helper lookup with "no entry", which the write path
    treats as enabled.
    """
    registry = MagicMock()
    registry.async_get.return_value = None
    with patch(
        "custom_components.better_thermostat.utils.helpers.er.async_get",
        return_value=registry,
    ):
        yield


def _thermostat(adapter, quirks=None):
    """A thermostat with one TRV whose valve channel is ready to write to."""
    thermostat = MagicMock()
    thermostat.device_name = "Test BT"
    trv = Trv.from_legacy_dict(ENTITY_ID, {})
    trv.valve_position_entity = VALVE_ENTITY
    trv.valve_position_writable = True
    trv.adapter = adapter
    trv.model_quirks = quirks if quirks is not None else MagicMock(spec=[])
    thermostat.real_trvs = {ENTITY_ID: trv}
    return thermostat


class TestAValveWriteThatFails:
    """An unreachable device is worth another attempt, and worth reporting."""

    @pytest.mark.asyncio
    async def test_a_write_that_raises_is_attempted_again(self):
        """One dropped Zigbee message must not cost the whole cycle.

        The write is the only part of the call that a second attempt can
        change, so the failure has to reach the retry around it.
        """
        adapter = SimpleNamespace(
            set_valve=AsyncMock(side_effect=ConnectionError("device unreachable"))
        )
        thermostat = _thermostat(adapter)

        with patch(f"{_RETRY}.asyncio.sleep", new=AsyncMock()):
            answer = await delegate.set_valve(thermostat, ENTITY_ID, 50)

        assert answer is False
        assert adapter.set_valve.await_count == ATTEMPTS

    @pytest.mark.asyncio
    async def test_a_write_through_a_model_quirk_is_attempted_again(self):
        """A quirk drives the same wire, so it is worth the same attempts.

        Once they are spent, the adapter's own channel still takes the
        position.
        """
        quirks = SimpleNamespace(
            override_set_valve=AsyncMock(side_effect=OSError("bus error"))
        )
        adapter = SimpleNamespace(set_valve=AsyncMock(return_value=None))
        thermostat = _thermostat(adapter, quirks=quirks)

        with patch(f"{_RETRY}.asyncio.sleep", new=AsyncMock()):
            answer = await delegate.set_valve(thermostat, ENTITY_ID, 50)

        assert quirks.override_set_valve.await_count == ATTEMPTS
        assert answer is True
        adapter.set_valve.assert_awaited_once_with(thermostat, ENTITY_ID, 50)

    @pytest.mark.asyncio
    async def test_a_write_nobody_could_make_is_reported(self, caplog):
        """A radiator stuck at the wrong position leaves a trace to follow."""
        adapter = SimpleNamespace(
            set_valve=AsyncMock(side_effect=ConnectionError("device unreachable"))
        )
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
    async def test_a_helper_entity_not_known_writable_costs_no_attempt(self):
        """No number of attempts turns a missing channel into one."""
        adapter = SimpleNamespace(set_valve=AsyncMock())
        thermostat = _thermostat(adapter)
        thermostat.real_trvs[ENTITY_ID].valve_position_writable = None

        answer = await delegate.set_valve(thermostat, ENTITY_ID, 50)

        assert answer is None
        adapter.set_valve.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_quirk_that_declines_leaves_the_adapter_to_write(self):
        """A quirk that did not take the position hands it to the adapter."""
        quirks = SimpleNamespace(override_set_valve=AsyncMock(return_value=False))
        adapter = SimpleNamespace(set_valve=AsyncMock(return_value=None))
        thermostat = _thermostat(adapter, quirks=quirks)

        answer = await delegate.set_valve(thermostat, ENTITY_ID, 50)

        assert answer is True
        adapter.set_valve.assert_awaited_once_with(thermostat, ENTITY_ID, 50)
        assert thermostat.real_trvs[ENTITY_ID].last_valve_method == "adapter"
