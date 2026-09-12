"""What the periodic ticks do once they fire.

Startup registering a callback on an interval says nothing about the
callback doing anything when the interval elapses; the registration and
the effect are two separate claims and only one of them is about the
user's radiator. The set of registrations is asserted in
``test_climate_startup_registration``; here each tick is called once and
the effect it exists for is asserted.

Three of the six ticks are covered by the file that owns their
behaviour: ``_maintenance_tick`` in ``test_climate_maintenance``,
``_external_temperature_keepalive`` in
``test_external_temperature_keepalive`` and ``_async_update_ema_periodic``
in ``test_climate_ema_periodic``. The remaining three are here.

Every guard these handlers carry is asserted in both directions: the
condition met, and the condition not met. A guard tested only the way
it fires today can be inverted without a test noticing.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.core.decide import KernelState

_CLIMATE = "custom_components.better_thermostat.climate"


@pytest.fixture
def bt():
    """A BetterThermostat stand-in whose entities all read as available."""
    mock = MagicMock()
    mock.device_name = "Test BT"
    mock.kernel_state = KernelState()
    mock.in_maintenance = False
    mock.call_for_heat = True
    mock._last_call_for_heat = True
    mock.async_update_ha_state = AsyncMock()
    mock.hass = MagicMock()
    return mock


# ---------------------------------------------------------------------------
# _trigger_check_weather: the hourly tick
# ---------------------------------------------------------------------------


async def _run_check_weather(bt, event, *, critical_ok=True, flips_call_for_heat=False):
    """Call the hourly tick; return the weather check and the cycle request."""

    async def check_weather(self):
        if flips_call_for_heat:
            self.call_for_heat = not self._last_call_for_heat

    weather = AsyncMock(side_effect=check_weather)
    request = MagicMock()
    with (
        patch(f"{_CLIMATE}.check_and_update_degraded_mode", AsyncMock()),
        patch(
            f"{_CLIMATE}.check_critical_entities", AsyncMock(return_value=critical_ok)
        ),
        patch(f"{_CLIMATE}.check_weather", weather),
        patch(f"{_CLIMATE}.request_control_cycle", request),
    ):
        await BetterThermostat._trigger_check_weather(bt, event)
    return weather, request


@pytest.mark.asyncio
async def test_the_hourly_tick_reads_the_weather(bt):
    """The tick's reason to exist: the forecast is fetched again."""
    weather, _ = await _run_check_weather(bt, None)

    weather.assert_awaited_once_with(bt)


@pytest.mark.asyncio
async def test_an_unavailable_critical_entity_stops_the_hourly_tick(bt):
    """Nothing to act on: the weather is not read either."""
    weather, _ = await _run_check_weather(bt, None, critical_ok=False)

    weather.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_call_for_heat_that_flipped_is_published(bt):
    """Summer mode turning on or off changes what the entity reports."""
    await _run_check_weather(bt, None, flips_call_for_heat=True)

    assert bt._last_call_for_heat == bt.call_for_heat
    bt.async_update_ha_state.assert_awaited_once_with(force_refresh=True)
    bt.async_write_ha_state.assert_called_once()


@pytest.mark.asyncio
async def test_an_unchanged_call_for_heat_publishes_nothing(bt):
    """A forecast that moved without crossing the threshold is not news."""
    await _run_check_weather(bt, None)

    bt.async_update_ha_state.assert_not_awaited()
    bt.async_write_ha_state.assert_not_called()


@pytest.mark.asyncio
async def test_a_flip_on_the_timer_requests_a_control_cycle(bt):
    """The timer passes an event, and the valves have to follow the flip."""
    _, request = await _run_check_weather(bt, MagicMock(), flips_call_for_heat=True)

    request.assert_called_once_with(bt)


@pytest.mark.asyncio
async def test_a_flip_during_startup_requests_no_control_cycle(bt):
    """Startup calls the tick without an event and drives its own cycle."""
    _, request = await _run_check_weather(bt, None, flips_call_for_heat=True)

    request.assert_not_called()


# ---------------------------------------------------------------------------
# _trigger_time: the five-minute control tick
# ---------------------------------------------------------------------------


async def _run_trigger_time(bt, event, *, critical_ok=True):
    """Call the control tick; return the ambient check and the cycle request."""
    ambient = AsyncMock()
    request = MagicMock()
    with (
        patch(f"{_CLIMATE}.check_and_update_degraded_mode", AsyncMock()),
        patch(
            f"{_CLIMATE}.check_critical_entities", AsyncMock(return_value=critical_ok)
        ),
        patch(f"{_CLIMATE}.check_ambient_air_temperature", ambient),
        patch(f"{_CLIMATE}.request_control_cycle", request),
    ):
        await BetterThermostat._trigger_time(bt, event)
    return ambient, request


@pytest.mark.asyncio
async def test_the_control_tick_refreshes_the_outdoor_average(bt):
    """The averaged outdoor temperature is what the tick is for."""
    ambient, _ = await _run_trigger_time(bt, None)

    ambient.assert_awaited_once_with(bt)
    bt.async_write_ha_state.assert_called_once()


@pytest.mark.asyncio
async def test_the_control_tick_requests_a_cycle_when_the_timer_fired_it(bt):
    """On the timer the refreshed average has to reach the valves."""
    _, request = await _run_trigger_time(bt, MagicMock())

    request.assert_called_once_with(bt)


@pytest.mark.asyncio
async def test_the_control_tick_requests_no_cycle_during_startup(bt):
    """Startup calls it without an event and drives its own cycle."""
    _, request = await _run_trigger_time(bt, None)

    request.assert_not_called()


@pytest.mark.asyncio
async def test_a_running_valve_maintenance_skips_the_control_tick(bt):
    """A control cycle mid-exercise would drive the valve off its sweep."""
    bt.in_maintenance = True

    ambient, request = await _run_trigger_time(bt, MagicMock())

    ambient.assert_not_awaited()
    request.assert_not_called()


@pytest.mark.asyncio
async def test_an_unavailable_critical_entity_stops_the_control_tick(bt):
    """No reachable valve means no outdoor refresh and no cycle."""
    ambient, request = await _run_trigger_time(bt, MagicMock(), critical_ok=False)

    ambient.assert_not_awaited()
    request.assert_not_called()


# ---------------------------------------------------------------------------
# _reconcile_tick: the five-minute reconciliation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_reconcile_tick_reaches_the_reconciler(bt):
    """The registered callback is the entity's way into ``reconcile_tick``.

    The reconciler's own decisions are pinned in ``test_reconciler``;
    what this asserts is that the timer's callback gets there at all,
    carrying the moment it fired.
    """
    now = MagicMock()
    reconcile = AsyncMock()
    with patch(f"{_CLIMATE}.reconcile_tick", reconcile):
        await BetterThermostat._reconcile_tick(bt, now)

    reconcile.assert_awaited_once_with(bt, now)
