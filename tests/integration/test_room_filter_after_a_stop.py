"""The filtered room temperature after a restart describes the room as it is.

The filter's moving average and slope are saved with the rest of the runtime
state, and Home Assistant may stay down for seconds or for hours before they
are read again. A filter that resumes as if no time had passed holds the
temperature the room had at the stop, and the controllers that read it
heat or idle against a room that is no longer there until the average has
caught up, five to fifteen minutes later.
"""

from homeassistant.const import (
    EVENT_HOMEASSISTANT_FINAL_WRITE,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import CoreState, State
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import mock_restore_cache

from custom_components.better_thermostat.utils.state_manager import CURRENT_VERSION

from .conftest import (
    BT_ENTITY,
    DOMAIN,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for_startup,
)

ROOM_NOW = 17.0
ROOM_AT_STOP = 21.5
SLOPE_AT_STOP = 0.05


def _store_key(entry) -> str:
    return f"{DOMAIN}_{entry.entry_id}_state"


def _saved_filters(entry, filters) -> dict[str, object]:
    """Return a store payload that holds ``filters`` and nothing learned."""
    key = _store_key(entry)
    return {
        "version": CURRENT_VERSION,
        "minor_version": 1,
        "key": key,
        "data": {
            "version": CURRENT_VERSION,
            "mpc": {},
            "pid": {},
            "tpi": {},
            "thermal": {},
            "filters": filters,
        },
    }


async def _start_after_a_stop(hass, hass_storage, fake_trv, stopped_seconds):
    """Start a thermostat whose filter was saved ``stopped_seconds`` ago.

    ``None`` saves the filter the way a store without its time carries it.
    """
    set_room_sensor(hass, ROOM_NOW)
    entry = make_entry(fake_trv.profile)
    filters: dict[str, object] = {
        "external_temp_ema": ROOM_AT_STOP,
        "temp_slope": SLOPE_AT_STOP,
    }
    if stopped_seconds is not None:
        filters["room_temperature_ema_recorded_at"] = (
            dt_util.utcnow().timestamp() - stopped_seconds
        )
    hass_storage[_store_key(entry)] = _saved_filters(entry, filters)
    await setup_entry(hass, entry)
    return entry, await wait_for_startup(hass, entry)


async def test_a_long_stop_hands_the_filter_to_the_live_reading(
    hass, hass_storage, fake_trv
):
    """After hours down, the filter starts on the room's current temperature."""
    _, bt = await _start_after_a_stop(hass, hass_storage, fake_trv, 2 * 3600)

    assert bt.room_temperature_filtered == ROOM_NOW
    assert bt.temperature_slope is None


async def test_a_short_restart_keeps_the_filter_running(hass, hass_storage, fake_trv):
    """A restart within the filter's time constant resumes the saved filter.

    Thirty seconds down move the average a tenth of the way to the current
    reading, and the slope still describes the room.
    """
    _, bt = await _start_after_a_stop(hass, hass_storage, fake_trv, 30)

    filtered = bt.room_temperature_filtered
    assert filtered is not None
    assert ROOM_NOW < filtered < ROOM_AT_STOP
    assert filtered > ROOM_AT_STOP - 0.5 * (ROOM_AT_STOP - ROOM_NOW)
    assert bt.temperature_slope == SLOPE_AT_STOP


async def test_a_filter_saved_without_its_time_starts_on_the_live_reading(
    hass, hass_storage, fake_trv
):
    """A filter of unknown age is not resumed; the live reading seeds it."""
    _, bt = await _start_after_a_stop(hass, hass_storage, fake_trv, None)

    assert bt.room_temperature_filtered == ROOM_NOW
    assert bt.temperature_slope is None


async def test_a_filter_published_by_1_9_does_not_replace_the_live_reading(
    hass, fake_trv
):
    """The filter a 1.9 state carries has no age, so it is not taken over."""
    mock_restore_cache(
        hass,
        [
            State(
                BT_ENTITY,
                "heat",
                {
                    "temperature": 21.0,
                    "external_temp_ema": ROOM_AT_STOP,
                    "temp_slope_K_min": SLOPE_AT_STOP,
                },
            )
        ],
    )
    set_room_sensor(hass, ROOM_NOW)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)

    assert bt.room_temperature_filtered == ROOM_NOW
    assert bt.temperature_slope is None


async def test_the_saved_filter_carries_the_time_it_was_last_updated(
    hass, hass_storage, fake_trv
):
    """The filter is saved together with the wall-clock time of its last update."""
    set_room_sensor(hass, ROOM_NOW)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    started = dt_util.utcnow().timestamp()

    bt.schedule_save_state()
    hass.set_state(CoreState.stopping)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    hass.set_state(CoreState.final_write)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()
    hass.set_state(CoreState.running)

    filters = hass_storage[_store_key(entry)]["data"]["filters"]
    assert filters["external_temp_ema"] == ROOM_NOW
    recorded_at = filters["room_temperature_ema_recorded_at"]
    assert started - 60 < recorded_at <= dt_util.utcnow().timestamp()
