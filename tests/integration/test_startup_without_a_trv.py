"""End-to-end tests: a room whose TRV is off the air when it starts.

A room with several TRVs starts once the startup grace window has closed and
at least one of them is reachable, and a TRV that comes back later is set up
and driven from then on. A room with no reachable TRV keeps waiting.
"""

from datetime import timedelta
from unittest.mock import patch

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    setup_test_component_platform,
)

from .conftest import DOMAIN, SENSOR_ID, TRV_ID, FakeTrvEntity, setup_entry, wait_for

BT_ENTITY = "climate.bt_test"
TRV_ID_2 = "climate.fake_trv_2"
CRITICAL_GRACE = (
    "custom_components.better_thermostat.climate.STARTUP_CRITICAL_GRACE_PERIOD"
)
# A startup grace window that is already over by the time the first check
# runs, for a room that has to start without waiting it out.
NO_GRACE = timedelta(seconds=0)
# Startup polls every 20 s; with the sleeps compressed, this is well past the
# number of polls a room that is going to start needs.
STARTUP_WAIT_S = 3.0


class _SecondFakeTrv(FakeTrvEntity):
    _attr_name = "fake trv 2"


@pytest.fixture
async def two_heads(hass):
    """Register two fake TRVs; the second starts off the air."""
    first = FakeTrvEntity()
    second = _SecondFakeTrv()
    second._attr_available = False
    setup_test_component_platform(hass, CLIMATE_DOMAIN, [first, second])
    assert await async_setup_component(
        hass, CLIMATE_DOMAIN, {CLIMATE_DOMAIN: {"platform": "test"}}
    )
    await hass.async_block_till_done()
    assert hass.states.get(TRV_ID) is not None
    assert hass.states.get(TRV_ID_2).state == "unavailable"
    return first, second


def _make_two_head_entry() -> MockConfigEntry:
    """Build a config entry for a room with both fake TRVs."""
    head = {
        "integration": "generic_thermostat",
        "model": "Generic",
        "advanced": {
            "calibration": "target_temp_based",
            "calibration_mode": "default",
            "no_off_system_mode": False,
        },
    }
    data = {
        "name": "BT Test",
        "thermostat": [{**head, "trv": TRV_ID}, {**head, "trv": TRV_ID_2}],
        "temperature_sensor": SENSOR_ID,
        "model": "Generic",
        "target_temp_step": "0.5",
        "tolerance": 0.3,
        "off_temperature": 5,
    }
    return MockConfigEntry(domain=DOMAIN, version=18, data=data, title="BT Test")


async def _boot(hass, *, grace):
    """Set the two-head room up with ``grace`` as the startup grace window."""
    hass.states.async_set(SENSOR_ID, "18.0", {"unit_of_measurement": "°C"})
    entry = _make_two_head_entry()
    with patch(CRITICAL_GRACE, grace):
        await setup_entry(hass, entry)
        bt = hass.data[DOMAIN][entry.entry_id]["climate"]
        started = await wait_for(
            hass,
            lambda: (
                not bt.startup_running and bt._async_unsub_state_changed is not None
            ),
            timeout_s=STARTUP_WAIT_S,
        )
    return bt, started


def _bring_back(hass, head) -> None:
    """Put ``head`` back on the air."""
    head._attr_available = True
    head.async_write_ha_state()


async def test_a_room_booting_with_a_head_gone_starts_once_the_grace_window_closes(
    hass, two_heads
):
    """The reachable head is driven and the absent head is left alone."""
    first, second = two_heads

    bt, started = await _boot(hass, grace=NO_GRACE)

    assert started
    assert hass.states.get(BT_ENTITY).state == "heat"
    assert await wait_for(hass, lambda: first.set_temperature_calls)
    assert second.set_temperature_calls == []
    assert second.set_hvac_mode_calls == []
    assert bt.real_trvs[TRV_ID_2].awaiting_initialization is True


async def test_a_room_booting_with_a_head_gone_waits_out_the_grace_window(
    hass, two_heads
):
    """Inside the grace window a head still loading at boot is waited for."""
    first, _second = two_heads

    bt, started = await _boot(hass, grace=timedelta(minutes=2))

    assert not started
    assert bt.startup_running
    assert first.set_temperature_calls == []


async def test_a_room_with_every_head_gone_keeps_waiting_after_the_grace_window(
    hass, two_heads
):
    """With no head to drive the room does not start, and starts on the first."""
    first, second = two_heads
    first._attr_available = False
    first.async_write_ha_state()

    bt, started = await _boot(hass, grace=NO_GRACE)

    assert not started
    assert hass.states.get(BT_ENTITY).state == "unavailable"

    _bring_back(hass, first)
    assert await wait_for(
        hass,
        lambda: not bt.startup_running and bt._async_unsub_state_changed is not None,
    )
    assert hass.states.get(BT_ENTITY).state == "heat"
    assert await wait_for(hass, lambda: first.set_temperature_calls)
    assert second.set_temperature_calls == []


async def test_a_head_that_arrives_after_the_room_started_is_initialised_and_driven(
    hass, two_heads
):
    """The late head is set up, joins the room and is commanded to its target."""
    first, second = two_heads
    bt, started = await _boot(hass, grace=NO_GRACE)
    assert started
    assert await wait_for(hass, lambda: first.set_temperature_calls)

    _bring_back(hass, second)

    assert await wait_for(
        hass, lambda: not bt.real_trvs[TRV_ID_2].awaiting_initialization
    )
    assert bt.real_trvs[TRV_ID_2].hvac_modes is not None
    assert bt.real_trvs[TRV_ID_2].target_temp_step == 0.5
    assert await wait_for(hass, lambda: second.set_temperature_calls)
    assert second.set_temperature_calls[-1] == first.set_temperature_calls[-1]
