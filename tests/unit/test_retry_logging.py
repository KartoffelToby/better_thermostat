"""What the async retry decorator in utils/retry.py writes to the log."""

import logging
from unittest.mock import AsyncMock, patch

from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.better_thermostat.utils.retry import async_retry

_RETRY = "custom_components.better_thermostat.utils.retry"


class TestWhatTheLogCarries:
    """A failed chain of attempts reaches the log as one warning.

    The caller the error is handed back to decides how loud it gets. The
    attempts before the last one are debug detail, and the traceback is
    only written where debug logging asks for it.
    """

    @staticmethod
    async def _run(caplog, level, error, retries, succeed_on=None):
        attempts = []

        @async_retry(retries=retries)
        async def write(self, entity_id):
            attempts.append(entity_id)
            if succeed_on is not None and len(attempts) == succeed_on:
                return True
            raise error

        with caplog.at_level(level, logger=_RETRY):
            with patch(f"{_RETRY}.asyncio.sleep", new=AsyncMock()):
                if succeed_on is None:
                    with pytest.raises(type(error)):
                        await write(object(), "climate.trv")
                else:
                    await write(object(), "climate.trv")
        return [r for r in caplog.records if r.name == _RETRY]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("error", "retries"),
        [pytest.param(HomeAssistantError("no answer"), 5, id="gave_up")],
    )
    async def test_a_failed_chain_is_one_warning(self, caplog, error, retries):
        """Without debug logging a failed chain leaves one warning, no traceback."""
        records = await self._run(caplog, logging.INFO, error, retries)

        assert [r.levelno for r in records] == [logging.WARNING]
        assert not records[0].exc_info

    @pytest.mark.asyncio
    async def test_a_call_that_succeeds_on_a_later_attempt_logs_nothing(self, caplog):
        """An attempt that is retried and then succeeds is no warning or error."""
        records = await self._run(
            caplog, logging.INFO, HomeAssistantError("no answer"), 5, succeed_on=2
        )

        assert records == []

    @pytest.mark.asyncio
    async def test_debug_logging_carries_every_attempt_once(self, caplog):
        """With debug logging each attempt is one line, the traceback on each."""
        records = await self._run(
            caplog, logging.DEBUG, HomeAssistantError("no answer"), 2
        )

        assert [r.levelno for r in records] == [
            logging.DEBUG,
            logging.DEBUG,
            logging.WARNING,
        ]
        assert all(r.exc_info for r in records)
