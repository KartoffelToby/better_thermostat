"""What the async retry decorator in utils/retry.py writes to the log."""

import logging
from unittest.mock import AsyncMock, patch

from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.better_thermostat.utils.retry import async_retry

_RETRY = "custom_components.better_thermostat.utils.retry"


class TestWhatTheLogCarries:
    """A failure is reported in one line; its traceback is for debug logging.

    The caller the error is handed back to decides how loud it gets, so the
    retry helper does not print a traceback of its own at every attempt.
    """

    @staticmethod
    async def _fail(caplog, error, retries):
        @async_retry(retries=retries)
        async def write(self, entity_id):
            raise error

        with caplog.at_level(logging.DEBUG, logger=_RETRY):
            with patch(f"{_RETRY}.asyncio.sleep", new=AsyncMock()):
                with pytest.raises(type(error)):
                    await write(object(), "climate.trv")
        return [r for r in caplog.records if r.name == _RETRY]

    @pytest.mark.asyncio
    async def test_the_final_failure_is_one_warning_without_traceback(self, caplog):
        """The line that ends the attempts is a warning; debug holds the traceback."""
        records = await self._fail(caplog, HomeAssistantError("no answer"), 2)

        final = [r for r in records if r.levelno >= logging.WARNING][-1]
        assert final.levelno == logging.WARNING
        assert final.exc_info is None
        debug = [r for r in records if r.levelno == logging.DEBUG]
        assert len(debug) == 3
        assert all(r.exc_info for r in debug)

    @pytest.mark.asyncio
    async def test_an_attempt_that_is_retried_logs_no_traceback(self, caplog):
        """Each retried attempt is one line at its level; debug holds the traceback."""
        records = await self._fail(caplog, HomeAssistantError("no answer"), 2)

        attempts = [
            r
            for r in records
            if "retrying in" in r.getMessage() and r.levelno > logging.DEBUG
        ]
        assert len(attempts) == 2
        assert all(r.levelno == logging.ERROR and not r.exc_info for r in attempts)
        assert not [r for r in records if r.levelno > logging.DEBUG and r.exc_info]
