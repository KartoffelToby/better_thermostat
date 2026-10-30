"""A head off the air is looked in on again when its retry window comes round.

The retry waits on Home Assistant's timer, so Home Assistant's clock decides
when it runs: it runs once that clock passes the window and not before.
"""

import asyncio
from datetime import timedelta
from unittest.mock import patch

from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.better_thermostat.utils import controlling

from .conftest import (
    WRITE_BUDGET,
    build_devices,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import GROUP_OF_THREE
from .write_hold import poll_until

RETRY_TASK = "bt_reachability_retry_"


@pytest.fixture
def retry_requests():
    """Record the control cycles the reachability retry asks for."""
    requests: list[str] = []
    original = controlling.request_control_cycle

    def recording(self, *args, **kwargs):
        task = asyncio.current_task()
        if task is not None and task.get_name().startswith(RETRY_TASK):
            requests.append(task.get_name())
        return original(self, *args, **kwargs)

    with patch.object(controlling, "request_control_cycle", recording):
        yield requests


async def test_the_retry_runs_once_the_clock_passes_its_window(hass, retry_requests):
    """Half the window asks for nothing; past the window, a cycle is asked for."""
    heads = await build_devices(hass, *GROUP_OF_THREE.profiles)
    set_room_sensor(hass, 19.5)
    entry = make_entry(GROUP_OF_THREE)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    gone = heads[1]
    trv = bt.real_trvs[gone.entity_id]

    with patch(WRITE_BUDGET, 0.0):
        gone.set_available(False)
        await hass.async_block_till_done()
        set_room_sensor(hass, 19.7)
        assert await wait_for(hass, lambda: trv.reachability_retry_pending)

        region = bt.kernel_state.reachability[gone.entity_id]
        assert region.retry_at is not None
        window = region.retry_at - bt.clock.monotonic()
        assert window > 1.0

        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=window / 2))
        await hass.async_block_till_done()
        assert retry_requests == []
        assert trv.reachability_retry_pending

        # Polled rather than waited out with async_block_till_done: a timer
        # job that went wrong would leave that wait hanging instead of failing.
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=window + 1))
        assert await poll_until(hass, lambda: retry_requests != [], 3.0)
