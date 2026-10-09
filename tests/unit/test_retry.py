"""Tests for the async retry decorator in utils/retry.py."""

import asyncio
import logging
from unittest.mock import AsyncMock, patch

from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceNotFound,
    ServiceNotSupported,
    ServiceValidationError,
)
import pytest
import voluptuous as vol

from custom_components.better_thermostat.utils.retry import (
    DeviceCallTimeoutError,
    async_retry,
    device_call_deadline,
)

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
    async def test_a_call_past_its_deadline_is_handed_back_on_the_first_attempt(self):
        """A device that did not answer within the deadline is not asked again.

        The call already waited the whole deadline, and the next attempt at a
        device that does not answer waits it again.
        """
        attempts = []

        @async_retry(retries=5)
        async def write(self, entity_id):
            attempts.append(entity_id)
            async with device_call_deadline():
                await asyncio.Event().wait()

        with (
            patch(f"{_RETRY}.DEVICE_CALL_TIMEOUT_S", 0.01),
            patch(f"{_RETRY}.asyncio.sleep", new=AsyncMock()) as sleep,
        ):
            with pytest.raises(DeviceCallTimeoutError):
                await write(object(), "climate.trv")

        assert len(attempts) == 1
        sleep.assert_not_awaited()

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
        """A missing attribute is a programming error, not a busy device.

        Retrying it spends the whole backoff budget before the traceback
        arrives, which buries the line that names the real defect.
        """
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
        """Data a service schema rejects comes back on the first attempt.

        Repeating a payload the schema has already refused cannot change its
        verdict, so the caller gets the error without waiting out a backoff
        budget that would end in the same place.
        """
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

    @pytest.mark.asyncio
    async def test_an_entity_id_passed_by_keyword_names_the_device(self, caplog):
        """The keyword spelling of the id labels the line as well."""

        @async_retry(retries=1)
        async def write(*, entity_id):
            raise HomeAssistantError("device did not answer")

        with caplog.at_level(logging.DEBUG):
            with patch(f"{_RETRY}.asyncio.sleep", new=AsyncMock()):
                with pytest.raises(HomeAssistantError):
                    await write(entity_id="climate.trv")

        assert "to entity climate.trv" in caplog.text


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


class TestDeviceCallDeadline:
    """A device call either returns within the deadline or fails at it."""

    @pytest.mark.asyncio
    async def test_a_call_that_answers_in_time_returns_its_value(self):
        """A call well inside the deadline is left alone."""

        async def read():
            return 21.5

        async with device_call_deadline():
            value = await read()

        assert value == 21.5

    @pytest.mark.asyncio
    async def test_a_call_that_never_answers_is_cancelled_at_the_deadline(self):
        """The call is cancelled and its caller gets a failure to handle."""
        cancelled = asyncio.Event()

        async def never_answers():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        with patch(f"{_RETRY}.DEVICE_CALL_TIMEOUT_S", 0.01):
            with pytest.raises(DeviceCallTimeoutError):
                async with device_call_deadline():
                    await never_answers()

        assert cancelled.is_set()

    @pytest.mark.asyncio
    async def test_a_timeout_the_call_raises_itself_is_passed_on_unchanged(self):
        """A device that reports its own timeout keeps the retry chain it had."""
        with pytest.raises(TimeoutError) as raised:
            async with device_call_deadline():
                raise TimeoutError("radio did not acknowledge")

        assert type(raised.value) is TimeoutError


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
        [
            pytest.param(HomeAssistantError("no answer"), 5, id="gave_up"),
            pytest.param(ServiceValidationError("out of range"), 5, id="refused"),
        ],
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

    @pytest.mark.asyncio
    async def test_a_louder_retry_level_carries_no_traceback_without_debug(
        self, caplog
    ):
        """A caller's louder retry level does not bring the traceback back.

        The retried attempts are logged at the caller's level, but the
        traceback goes with them only while debug logging is on, the same
        rule the warning that ends the attempts follows.
        """
        attempts = []

        @async_retry(retries=2, log_level=logging.WARNING)
        async def write(self, entity_id):
            attempts.append(entity_id)
            raise HomeAssistantError("no answer")

        with caplog.at_level(logging.INFO, logger=_RETRY):
            with patch(f"{_RETRY}.asyncio.sleep", new=AsyncMock()):
                with pytest.raises(HomeAssistantError):
                    await write(object(), "climate.trv")
        records = [r for r in caplog.records if r.name == _RETRY]

        assert [r.levelno for r in records] == [logging.WARNING] * 3
        assert not any(r.exc_info for r in records)
