"""Startup grace window for degraded-mode annunciation.

_finalize_startup arms the degraded-mode grace deadline on the lifecycle
region together with the INITIALISING -> STARTING transition, so the very
first control cycle (which runs a lifecycle tick) does not promote the
region straight to RUNNING. While the grace window is active,
check_and_update_degraded_mode defers the degraded-mode warning and the
Home Assistant repair issue; both fire once the window has elapsed.
"""

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.decide import KernelState
from custom_components.better_thermostat.core.fsm.lifecycle import (
    LifecyclePhase,
    tick as lifecycle_tick,
)
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.watcher import (
    STARTUP_DEGRADED_GRACE_PERIOD,
    check_and_update_degraded_mode,
)

_CLIMATE = "custom_components.better_thermostat.climate"
SENSOR_ID = "sensor.room_temp"
TRV_ID = "climate.test_trv"


def _startup_bt():
    """Build a minimal BetterThermostat mock for _finalize_startup."""
    mock = MagicMock()
    mock.device_name = "Test BT"
    mock.is_removed = False
    mock.kernel_state = KernelState()
    mock.clock = FakeClock()
    mock.real_trvs = {TRV_ID: Trv(entity_id=TRV_ID, advanced={})}
    mock.entity_ids = [TRV_ID]
    mock.all_trvs = None
    mock.all_entities = []
    mock.sensor_entity_id = SENSOR_ID
    mock.humidity_sensor_entity_id = None
    mock.window_id = None
    mock.door_id = None
    mock.cooler_entity_id = None
    mock.outdoor_sensor = None
    mock.weather_entity = None
    mock.unavailable_sensors = []
    mock._degraded_warning_emitted = False
    mock._degraded_grace_until = None
    mock._async_unsub_state_changed = None
    mock._trigger_time = AsyncMock()
    mock._trigger_check_weather = AsyncMock()
    mock._startup_control_trvs = AsyncMock()
    mock.async_update_ha_state = AsyncMock()
    mock.hass = MagicMock()
    return mock


async def _run_finalize_startup(bt, *, patch_degraded_check=True):
    """Run _finalize_startup with the external hooks patched."""
    patches = [
        patch(f"{_CLIMATE}.await_critical_entities", AsyncMock()),
        patch(f"{_CLIMATE}.check_critical_entities", AsyncMock(return_value=True)),
        patch(f"{_CLIMATE}.await_optional_sensors", AsyncMock()),
        patch(f"{_CLIMATE}.async_track_time_interval", MagicMock()),
        patch(f"{_CLIMATE}.async_track_state_change_event", MagicMock()),
        patch(f"{_CLIMATE}.async_track_time_change", MagicMock()),
        patch(f"{_CLIMATE}.asyncio.sleep", AsyncMock()),
    ]
    if patch_degraded_check:
        patches.append(patch(f"{_CLIMATE}.check_and_update_degraded_mode", AsyncMock()))
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        await BetterThermostat._finalize_startup(bt)


@pytest.mark.asyncio
async def test_grace_is_armed_with_the_starting_transition():
    """The lifecycle carries the grace deadline as soon as STARTING begins."""
    bt = _startup_bt()
    start = bt.clock.now()
    await _run_finalize_startup(bt)

    assert bt.kernel_state.lifecycle.phase == LifecyclePhase.STARTING
    assert bt._degraded_grace_until == start + STARTUP_DEGRADED_GRACE_PERIOD
    assert bt.kernel_state.lifecycle.grace_until == bt._degraded_grace_until
    assert bt.kernel_state.lifecycle.in_grace(bt.clock.now()) is True


@pytest.mark.asyncio
async def test_grace_survives_a_lifecycle_tick_until_it_elapses():
    """A tick keeps STARTING during the window and promotes RUNNING after."""
    bt = _startup_bt()
    await _run_finalize_startup(bt)

    ticked = lifecycle_tick(bt.kernel_state.lifecycle, bt.clock.now())
    assert ticked.phase == LifecyclePhase.STARTING
    assert ticked.in_grace(bt.clock.now()) is True

    bt.clock.advance(STARTUP_DEGRADED_GRACE_PERIOD.total_seconds() + 1)
    assert ticked.in_grace(bt.clock.now()) is False
    assert lifecycle_tick(ticked, bt.clock.now()).phase == LifecyclePhase.RUNNING


@pytest.mark.asyncio
async def test_post_grace_recheck_uses_the_deadline_armed_at_startup_finish():
    """The degraded recheck task is scheduled with the armed deadline."""
    bt = _startup_bt()
    await _run_finalize_startup(bt)

    recheck_calls = [
        call
        for call in bt._post_grace_recheck.call_args_list
        if call.args[0] == bt._degraded_grace_until
    ]
    assert bt._degraded_grace_until is not None
    assert recheck_calls, "no post-grace recheck scheduled with the armed deadline"


@pytest.mark.asyncio
async def test_degraded_sensor_stays_silent_during_grace_and_warns_after(caplog):
    """An unavailable sensor defers warning and repair issue until grace ends."""
    bt = _startup_bt()
    # Every entity reads as unavailable, so the degraded check sees the room
    # sensor missing while the TRV offers no usable internal temperature.
    bt.hass.states.get.return_value = None

    with patch(
        "custom_components.better_thermostat.utils.watcher.ir.async_create_issue"
    ) as create_issue:
        await _run_finalize_startup(bt, patch_degraded_check=False)

        assert bt.kernel_state.control_mode.degraded is True
        assert not any("Entering degraded mode" in r.message for r in caplog.records)
        create_issue.assert_not_called()
        assert bt._degraded_warning_emitted is False

        bt.clock.advance(STARTUP_DEGRADED_GRACE_PERIOD.total_seconds() + 1)
        with patch(
            "custom_components.better_thermostat.utils.helpers.async_fire_logbook_entry",
            AsyncMock(),
        ):
            await check_and_update_degraded_mode(bt)

        assert any("Entering degraded mode" in r.message for r in caplog.records)
        create_issue.assert_called_once()
        assert bt._degraded_warning_emitted is True
