"""Tests for the async retry decorator in utils/retry.py."""

from unittest.mock import AsyncMock, patch

from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceNotFound,
    ServiceNotSupported,
    ServiceValidationError,
)
import pytest

from custom_components.better_thermostat.utils.retry import async_retry

_RETRY = "custom_components.better_thermostat.utils.retry"


class _ServiceNotFound(ServiceNotFound):
    """``ServiceNotFound`` with a message that needs no running instance.

    Home Assistant renders these messages from translations, which asks for
    the running instance; the retry log line formats the error.
    """

    def __str__(self) -> str:
        return "service not found"


class _ServiceNotSupported(ServiceNotSupported):
    """``ServiceNotSupported`` with a message that needs no running instance."""

    def __str__(self) -> str:
        return "service not supported"


class TestWhatIsWorthRetrying:
    """Which failures get another attempt and which are handed straight back.

    A retry buys something when the device or the bus was momentarily out of
    reach. It buys nothing when Home Assistant refuses the payload itself,
    and the backoff of six attempts delays the traceback by half a minute.
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
    async def test_a_payload_the_entity_refuses_is_handed_back_on_the_first_attempt(
        self,
    ):
        """A payload Home Assistant validates and refuses comes back at once.

        Home Assistant checks a climate payload against the entity itself, a
        setpoint against its range and a mode against the modes it offers, and
        refuses one that fails with ``ServiceValidationError``. The same
        payload fails the same check on every attempt.
        """
        attempts = []

        @async_retry(retries=5)
        async def write(self, entity_id):
            attempts.append(entity_id)
            raise ServiceValidationError("temperature outside the accepted range")

        with patch(f"{_RETRY}.asyncio.sleep", new=AsyncMock()) as sleep:
            with pytest.raises(ServiceValidationError):
                await write(object(), "climate.trv")

        assert len(attempts) == 1
        sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_action_the_entity_does_not_support_is_handed_back_at_once(self):
        """An entity that lacks the feature an action needs refuses it every time."""
        attempts = []

        @async_retry(retries=5)
        async def write(self, entity_id):
            attempts.append(entity_id)
            raise _ServiceNotSupported("climate", "set_temperature", entity_id)

        with patch(f"{_RETRY}.asyncio.sleep", new=AsyncMock()) as sleep:
            with pytest.raises(ServiceNotSupported):
                await write(object(), "climate.trv")

        assert len(attempts) == 1
        sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_service_missing_while_its_integration_loads_is_retried(self):
        """A service not registered yet is there on a later attempt.

        Home Assistant reports a service whose integration is still loading
        or reloading as not found, and registers it again moments later.
        """
        attempts = []

        @async_retry(retries=5)
        async def write(self, entity_id):
            attempts.append(entity_id)
            if len(attempts) < 3:
                raise _ServiceNotFound("number", "set_value")
            return "written"

        with patch(f"{_RETRY}.asyncio.sleep", new=AsyncMock()):
            result = await write(object(), "climate.trv")

        assert result == "written"
        assert len(attempts) == 3
