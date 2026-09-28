"""What one control cycle spends on a device write that does not go through.

Every write to a TRV runs under the room's control lock, so the time one
write spends on attempts is time every other TRV of the room waits. A
single dropped message is worth the retry chain; a device that stays out of
reach is not worth it on every cycle, because the next cycle asks again
anyway. The same holds for all four channels a cycle writes: setpoint,
mode, offset and valve.
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.better_thermostat.adapters import (
    deconz,
    delegate,
    generic,
    mqtt,
    tado,
    zwave_js,
)
from custom_components.better_thermostat.adapters.base import AdapterCapabilities
from custom_components.better_thermostat.trv import Trv
from tests.factories import make_entity_registry, make_registry_entry

ENTITY_ID = "climate.trv"
VALVE_ENTITY = "number.trv_valve_position"
CALIBRATION_ENTITY = "number.trv_local_temperature_calibration"

_RETRY = "custom_components.better_thermostat.utils.retry"
_LOGGERS = ("custom_components.better_thermostat.adapters.delegate", _RETRY)

# Attempts one write is worth while its channel is not known to be out of
# reach: the first one plus the delegate's retries.
FULL_ATTEMPTS = 6

CHANNELS = ("temperature", "hvac_mode", "offset", "valve")


@pytest.fixture(autouse=True)
def _helper_entities_registered_and_enabled():
    """The registry holds the TRV's helper entities as enabled entries."""
    registry = make_entity_registry(
        make_registry_entry(VALVE_ENTITY), make_registry_entry(CALIBRATION_ENTITY)
    )
    with patch(
        "custom_components.better_thermostat.utils.helpers.er.async_get",
        return_value=registry,
    ):
        yield


def _thermostat(adapter, quirks=None):
    """A thermostat with one TRV whose four write channels are ready."""
    thermostat = MagicMock()
    thermostat.device_name = "Test BT"
    thermostat.bt_target_temp_step = 0.5
    trv = Trv(entity_id=ENTITY_ID)
    trv.valve_position_entity = VALVE_ENTITY
    trv.valve_position_writable = True
    trv.adapter = adapter
    trv.model_quirks = quirks if quirks is not None else MagicMock(spec=[])
    thermostat.real_trvs = {ENTITY_ID: trv}
    return thermostat


def _adapter(**writes):
    """An adapter whose writes are the given mocks, answering ok otherwise."""
    return SimpleNamespace(
        CAPABILITIES=AdapterCapabilities(offset_write=True, valve_write=True),
        set_temperature=writes.get("temperature", AsyncMock(return_value=None)),
        set_hvac_mode=writes.get("hvac_mode", AsyncMock(return_value=None)),
        set_offset=writes.get("offset", AsyncMock(return_value=True)),
        set_valve=writes.get("valve", AsyncMock(return_value=None)),
    )


async def _write(channel, thermostat):
    """Run one cycle's write on ``channel``; the answer, or the exception."""
    try:
        if channel == "temperature":
            return await delegate.set_temperature(thermostat, ENTITY_ID, 21.0)
        if channel == "hvac_mode":
            return await delegate.set_hvac_mode(thermostat, ENTITY_ID, "heat")
        if channel == "offset":
            return await delegate.set_offset(thermostat, ENTITY_ID, 1.0)
        return await delegate.set_valve(thermostat, ENTITY_ID, 50)
    except Exception as exc:  # noqa: BLE001 - the caller's view of a raise
        return exc


def _unreachable():
    return HomeAssistantError("Failed to send request: device did not respond")


class TestADeviceThatStaysOutOfReach:
    """The second failing cycle costs one attempt, not the retry chain."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("channel", CHANNELS)
    async def test_the_next_cycle_gets_one_attempt_and_no_backoff(self, channel):
        """An unreachable valve must not hold the room lock for ~30 s per cycle."""
        write = AsyncMock(side_effect=_unreachable())
        thermostat = _thermostat(_adapter(**{channel: write}))
        sleeps = AsyncMock()

        with patch(f"{_RETRY}.asyncio.sleep", new=sleeps):
            await _write(channel, thermostat)
            first_cycle = write.await_count
            first_backoff = sleeps.await_count
            await _write(channel, thermostat)

        assert first_cycle == FULL_ATTEMPTS
        assert write.await_count - first_cycle == 1
        assert sleeps.await_count == first_backoff

    @pytest.mark.asyncio
    @pytest.mark.parametrize("channel", CHANNELS)
    async def test_a_write_that_gets_through_restores_the_retry(self, channel):
        """A device back in reach is worth the full chain on its next miss."""
        write = AsyncMock(
            side_effect=[_unreachable()] * FULL_ATTEMPTS
            + [None if channel != "offset" else True]
            + [_unreachable()] * FULL_ATTEMPTS
        )
        thermostat = _thermostat(_adapter(**{channel: write}))

        with patch(f"{_RETRY}.asyncio.sleep", new=AsyncMock()):
            await _write(channel, thermostat)
            await _write(channel, thermostat)
            before_third = write.await_count
            await _write(channel, thermostat)

        assert before_third == FULL_ATTEMPTS + 1
        assert write.await_count - before_third == FULL_ATTEMPTS

    @pytest.mark.asyncio
    @pytest.mark.parametrize("channel", CHANNELS)
    async def test_a_single_dropped_message_is_still_retried(self, channel):
        """A transient failure is covered inside the cycle it happens in."""
        answer_ok = True if channel == "offset" else None
        write = AsyncMock(side_effect=[_unreachable(), answer_ok])
        thermostat = _thermostat(_adapter(**{channel: write}))

        with patch(f"{_RETRY}.asyncio.sleep", new=AsyncMock()):
            answer = await _write(channel, thermostat)

        assert write.await_count == 2
        assert not isinstance(answer, Exception)
        assert answer is not False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("channel", CHANNELS)
    async def test_the_outage_is_reported_once_not_every_cycle(self, channel, caplog):
        """One warning names the outage; repeating it every cycle is noise."""
        write = AsyncMock(side_effect=_unreachable())
        thermostat = _thermostat(_adapter(**{channel: write}))

        with (
            caplog.at_level(logging.DEBUG),
            patch(f"{_RETRY}.asyncio.sleep", new=AsyncMock()),
        ):
            await _write(channel, thermostat)
            first = [
                r
                for r in caplog.records
                if r.name in _LOGGERS and r.levelno >= logging.WARNING
            ]
            caplog.clear()
            await _write(channel, thermostat)
            second = [
                r
                for r in caplog.records
                if r.name in _LOGGERS and r.levelno >= logging.WARNING
            ]

        assert first
        assert second == []

    @pytest.mark.asyncio
    async def test_one_channel_out_of_reach_leaves_the_others_their_retry(self):
        """A valve helper that fails says nothing about the setpoint write."""
        valve = AsyncMock(side_effect=_unreachable())
        temperature = AsyncMock(side_effect=[_unreachable(), None])
        thermostat = _thermostat(_adapter(valve=valve, temperature=temperature))

        with patch(f"{_RETRY}.asyncio.sleep", new=AsyncMock()):
            await _write("valve", thermostat)
            await _write("temperature", thermostat)

        assert temperature.await_count == 2


class TestAValveQuirkThatRaises:
    """A quirk that raises leaves the adapter's valve channel to try."""

    @pytest.mark.asyncio
    async def test_the_adapter_channel_takes_the_position(self):
        """The quirk's failure is not the valve's failure."""
        quirks = SimpleNamespace(
            override_set_valve=AsyncMock(side_effect=RuntimeError("quirk broke"))
        )
        adapter = _adapter(valve=AsyncMock(return_value=None))
        thermostat = _thermostat(adapter, quirks=quirks)

        with patch(f"{_RETRY}.asyncio.sleep", new=AsyncMock()):
            answer = await delegate.set_valve(thermostat, ENTITY_ID, 40)

        trv = thermostat.real_trvs[ENTITY_ID]
        assert answer is True
        adapter.set_valve.assert_awaited_once_with(thermostat, ENTITY_ID, 40)
        assert trv.last_valve_percent == 40
        assert trv.last_valve_method == "adapter"

    @pytest.mark.asyncio
    async def test_both_channels_failing_answer_false(self):
        """Only once every channel failed is the position reported as missed."""
        quirks = SimpleNamespace(
            override_set_valve=AsyncMock(side_effect=RuntimeError("quirk broke"))
        )
        adapter = _adapter(valve=AsyncMock(side_effect=_unreachable()))
        thermostat = _thermostat(adapter, quirks=quirks)

        with patch(f"{_RETRY}.asyncio.sleep", new=AsyncMock()):
            answer = await delegate.set_valve(thermostat, ENTITY_ID, 40)

        assert answer is False
        assert adapter.set_valve.await_count == FULL_ATTEMPTS
        assert thermostat.real_trvs[ENTITY_ID].last_valve_percent is None


MODE_ADAPTERS = {
    "deconz": deconz,
    "generic": generic,
    "mqtt": mqtt,
    "tado": tado,
    "zwave_js": zwave_js,
}


def _thermostat_on(adapter_module, service_call):
    thermostat = _thermostat(adapter_module)
    thermostat.hass = MagicMock()
    thermostat.hass.services.async_call = service_call
    return thermostat


class TestARefusedModeChange:
    """A mode change the device refuses is retried and reported."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", sorted(MODE_ADAPTERS))
    async def test_the_refusal_is_retried_and_answered_false(self, name, caplog):
        """A TRV left in the wrong mode must not read as switched."""
        service_call = AsyncMock(side_effect=_unreachable())
        thermostat = _thermostat_on(MODE_ADAPTERS[name], service_call)

        with (
            caplog.at_level(logging.DEBUG),
            patch(f"{_RETRY}.asyncio.sleep", new=AsyncMock()),
        ):
            answer = await delegate.set_hvac_mode(thermostat, ENTITY_ID, "heat")

        assert answer is False
        assert service_call.await_count == FULL_ATTEMPTS
        assert any(
            r.levelno >= logging.WARNING and r.name in _LOGGERS for r in caplog.records
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", sorted(MODE_ADAPTERS))
    async def test_an_accepted_mode_change_answers_true(self, name):
        """The answer tells a written mode from a refused one."""
        service_call = AsyncMock(return_value=None)
        thermostat = _thermostat_on(MODE_ADAPTERS[name], service_call)

        answer = await delegate.set_hvac_mode(thermostat, ENTITY_ID, "heat")

        assert answer is True
        service_call.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_offset_written_before_a_refused_mode_counts_as_written(self):
        """The offset went out; the mode re-assertion after it is its own write.

        Reporting the offset as failed would send it again and again, each
        time followed by the same refused mode write.
        """
        calls = []

        async def service_call(domain, service, data, **kwargs):
            calls.append((domain, service))
            if domain == "climate":
                raise _unreachable()

        thermostat = _thermostat_on(generic, service_call)
        thermostat.hass.states.get = MagicMock(return_value=None)
        trv = thermostat.real_trvs[ENTITY_ID]
        trv.local_temperature_calibration_entity = CALIBRATION_ENTITY
        trv.last_hvac_mode = "heat"

        with (
            patch("asyncio.sleep", new=AsyncMock()),
            patch(f"{_RETRY}.asyncio.sleep", new=AsyncMock()),
        ):
            answer = await delegate.set_offset(thermostat, ENTITY_ID, 1.0)

        assert answer is True
        assert calls.count(("number", "set_value")) == 1
        assert trv.last_calibration_requested == 1.0
