"""Tests for the async retry decorator in utils/retry.py."""

import asyncio
import logging
from unittest.mock import AsyncMock, patch

from homeassistant.exceptions import HomeAssistantError
import pytest
import voluptuous as vol

from custom_components.better_thermostat.utils.retry import async_retry

_RETRY = "custom_components.better_thermostat.utils.retry"


class TestWhatIsWorthRetrying:
    """Which failures get another attempt and which are handed straight back.

    A retry buys something when the device or the bus was momentarily out of
    reach. It buys nothing when the call itself is wrong, and the backoff of
    six attempts delays the traceback by half a minute.
    """

    @pytest.mark.asyncio
    async def test_a_service_failure_gets_every_attempt(self):
        """A failing service call is repeated until the budget is spent."""
        attempts = []

        @async_retry(retries=5)
        async def write(self, entity_id):
            attempts.append(entity_id)
            raise HomeAssistantError("device did not answer")

        with patch(f"{_RETRY}.asyncio.sleep", new=AsyncMock()) as sleep:
            with pytest.raises(HomeAssistantError):
                await write(object(), "climate.trv")

        assert len(attempts) == 6
        assert sleep.await_count == 5

    @pytest.mark.asyncio
    async def test_a_connection_failure_gets_every_attempt(self):
        """A transport error is repeated too: the next attempt may reach."""
        attempts = []

        @async_retry(retries=2)
        async def write(self, entity_id):
            attempts.append(entity_id)
            raise ConnectionError("broker unreachable")

        with patch(f"{_RETRY}.asyncio.sleep", new=AsyncMock()):
            with pytest.raises(ConnectionError):
                await write(object(), "climate.trv")

        assert len(attempts) == 3

    @pytest.mark.asyncio
    async def test_a_timeout_gets_every_attempt(self):
        """A timed-out call is repeated: the device may answer the next one."""
        attempts = []

        @async_retry(retries=2)
        async def write(self, entity_id):
            attempts.append(entity_id)
            raise TimeoutError

        with patch(f"{_RETRY}.asyncio.sleep", new=AsyncMock()):
            with pytest.raises(TimeoutError):
                await write(object(), "climate.trv")

        assert len(attempts) == 3

    @pytest.mark.asyncio
    async def test_a_broken_call_is_handed_back_on_the_first_attempt(self):
        """A wrong call is not repeated, and nothing is slept away over it."""
        attempts = []

        @async_retry(retries=5)
        async def write(self, entity_id):
            attempts.append(entity_id)
            raise TypeError("set_temperature() takes 3 positional arguments")

        with patch(f"{_RETRY}.asyncio.sleep", new=AsyncMock()) as sleep:
            with pytest.raises(TypeError):
                await write(object(), "climate.trv")

        assert len(attempts) == 1
        sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_missing_attribute_is_handed_back_on_the_first_attempt(self):
        """An attribute the object never grows is not waited for."""
        attempts = []

        @async_retry(retries=5)
        async def write(self, entity_id):
            attempts.append(entity_id)
            raise AttributeError("'NoneType' object has no attribute 'adapter'")

        with patch(f"{_RETRY}.asyncio.sleep", new=AsyncMock()) as sleep:
            with pytest.raises(AttributeError):
                await write(object(), "climate.trv")

        assert len(attempts) == 1
        sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_rejected_service_payload_is_handed_back_on_the_first_attempt(self):
        """Data a service schema rejects is rejected again on every attempt."""
        attempts = []

        @async_retry(retries=5)
        async def write(self, entity_id):
            attempts.append(entity_id)
            raise vol.Invalid("value must be at most 30")

        with patch(f"{_RETRY}.asyncio.sleep", new=AsyncMock()) as sleep:
            with pytest.raises(vol.Invalid):
                await write(object(), "climate.trv")

        assert len(attempts) == 1
        sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_explicitly_named_exception_still_selects_what_is_caught(self):
        """An exception outside ``exceptions`` passes through untouched."""
        attempts = []

        @async_retry(retries=5, exceptions=(TimeoutError,))
        async def write(self, entity_id):
            attempts.append(entity_id)
            raise HomeAssistantError("device did not answer")

        with patch(f"{_RETRY}.asyncio.sleep", new=AsyncMock()) as sleep:
            with pytest.raises(HomeAssistantError):
                await write(object(), "climate.trv")

        assert len(attempts) == 1
        sleep.assert_not_awaited()


class TestWhatTheLogLineNames:
    """The entity the retry log points at."""

    @pytest.mark.asyncio
    async def test_the_entity_id_names_the_failing_device(self, caplog):
        """The second positional argument labels the line when it is an id."""

        @async_retry(retries=1)
        async def write(self, entity_id):
            raise HomeAssistantError("device did not answer")

        with caplog.at_level(logging.DEBUG):
            with patch(f"{_RETRY}.asyncio.sleep", new=AsyncMock()):
                with pytest.raises(HomeAssistantError):
                    await write(object(), "climate.trv")

        assert "to entity climate.trv" in caplog.text

    @pytest.mark.asyncio
    async def test_an_argument_that_is_no_entity_id_is_not_named_as_one(self, caplog):
        """A signature of a different shape leaves the line unlabelled."""

        @async_retry(retries=1)
        async def write(self, temperature):
            raise HomeAssistantError("device did not answer")

        with caplog.at_level(logging.DEBUG):
            with patch(f"{_RETRY}.asyncio.sleep", new=AsyncMock()):
                with pytest.raises(HomeAssistantError):
                    await write(object(), 21.5)

        assert "to entity" not in caplog.text


class TestWhatComesBack:
    """The value a call that eventually succeeds hands back."""

    @pytest.mark.asyncio
    async def test_a_call_that_succeeds_on_a_later_attempt_returns_its_value(self):
        """The result of the successful attempt reaches the caller."""
        attempts = []

        @async_retry(retries=3)
        async def read(self, entity_id):
            attempts.append(entity_id)
            if len(attempts) < 3:
                raise HomeAssistantError("device did not answer")
            return {"support_offset": True}

        with patch(f"{_RETRY}.asyncio.sleep", new=AsyncMock()):
            result = await read(object(), "climate.trv")

        assert result == {"support_offset": True}
        assert len(attempts) == 3

    @pytest.mark.asyncio
    async def test_a_cancelled_call_is_not_retried(self):
        """Cancellation ends the call instead of buying it another attempt."""
        attempts = []

        @async_retry(retries=5)
        async def write(self, entity_id):
            attempts.append(entity_id)
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await write(object(), "climate.trv")

        assert len(attempts) == 1
