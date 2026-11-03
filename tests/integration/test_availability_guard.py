"""TRVs that lose power with the heating system, through the running entity.

Wall thermostats fed by a heat pump or a boiler go dark when it is switched
off. The availability guard tells Better Thermostat when that is expected:
while it is off, a TRV that is away raises no repair issue and no warning.
Once it turns on, the TRVs get the grace period to come back, and one that
is still away after it is reported as any other outage is.
"""

from datetime import timedelta
import logging
import time
from unittest.mock import patch

from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.fsm.control_mode import LADDER_TICK_S
from custom_components.better_thermostat.utils.const import (
    DEFAULT_AVAILABILITY_GUARD_GRACE_SECONDS,
)
from custom_components.better_thermostat.utils.helpers import (
    entry_issue_id,
    entry_settings,
)

from .conftest import (
    CRITICAL_GRACE,
    DOMAIN,
    click_through_the_options,
    form_default,
    make_entry,
    profile_id,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import GROUP_OF_THREE

GUARD_ID = "input_boolean.heat_pump_powered"

# A startup grace window that is over by the time the first check runs, so
# the only thing that holds a report back is the availability guard.
NO_STARTUP_GRACE = timedelta(seconds=0)

pytestmark = pytest.mark.parametrize(
    "trv_group", [GROUP_OF_THREE], indirect=True, ids=profile_id
)


def bt_issues(hass) -> list[str]:
    """Return the repair issues Better Thermostat currently holds open."""
    return sorted(
        issue_id for (domain, issue_id) in ir.async_get(hass).issues if domain == DOMAIN
    )


def warnings_naming(caplog, entity_id: str) -> list[str]:
    """Return the warnings logged so far that name ``entity_id``."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING and entity_id in record.getMessage()
    ]


class TimeMachine:
    """Moves the thermostat's clock and Home Assistant's timers on together.

    The thermostat reads the time off its own clock, which is swapped for a
    fake one that starts at the present, and its periodic ticks run on Home
    Assistant's timers, which fire when the matching moment is announced.
    """

    def __init__(self, hass, bt) -> None:
        self.hass = hass
        self.start = dt_util.utcnow()
        self.elapsed = 0.0
        self.clock = FakeClock(monotonic_value=time.monotonic(), now_value=self.start)
        bt.clock = self.clock

    async def let_pass(self, seconds: float) -> None:
        """Move both on by ``seconds`` and run the timers that fall due."""
        self.clock.advance(seconds)
        self.elapsed += seconds
        async_fire_time_changed(
            self.hass, self.start + timedelta(seconds=self.elapsed + 1)
        )
        await self.hass.async_block_till_done()


async def test_a_head_without_power_is_reported_only_when_the_guard_expects_power(
    hass, trv_group, caplog
):
    """The guard decides whether a head that is away is a fault.

    Off, the head is expected to be dark. On, it is expected back within the
    grace period, and reported once that has passed. Off again, the report it
    raised is withdrawn rather than left standing for the whole time the
    heating system stays off.

    The grace period runs from the moment the guard turned on, which the
    fake clock cannot place to the millisecond, so the report is expected
    within one ladder tick after the period has passed.
    """
    hass.states.async_set(GUARD_ID, "off")
    set_room_sensor(hass, 19.5)
    dark_head = trv_group[1]
    dark_head.set_available(False)
    entry = make_entry(trv_group.scenario, availability_guard_entity_id=GUARD_ID)
    issue_id = entry_issue_id(entry.entry_id, "missing_entity", dark_head.entity_id)

    with patch(CRITICAL_GRACE, NO_STARTUP_GRACE), caplog.at_level(logging.INFO):
        await setup_entry(hass, entry)
        bt = await wait_for_startup(hass, entry)
        time_machine = TimeMachine(hass, bt)
        await time_machine.let_pass(LADDER_TICK_S)

        assert bt_issues(hass) == []
        assert bt.devices_errors == []
        assert warnings_naming(caplog, dark_head.entity_id) == []

        hass.states.async_set(GUARD_ID, "on")
        await hass.async_block_till_done()
        await time_machine.let_pass(
            DEFAULT_AVAILABILITY_GUARD_GRACE_SECONDS - LADDER_TICK_S
        )
        assert bt_issues(hass) == []

        await time_machine.let_pass(2 * LADDER_TICK_S)
        assert await wait_for(hass, lambda: bt_issues(hass) == [issue_id])
        assert bt.devices_errors == [dark_head.entity_id]
        assert warnings_naming(caplog, dark_head.entity_id) == [
            f"better_thermostat {bt.device_name}: Critical entity "
            f"{dark_head.entity_id} is unavailable"
        ]

        hass.states.async_set(GUARD_ID, "off")
        assert await wait_for(hass, lambda: bt_issues(hass) == [])
        assert bt.devices_errors == []


async def test_a_guard_that_stops_answering_lets_the_outage_be_reported(
    hass, trv_group
):
    """A guard that says nothing cannot vouch for an outage."""
    hass.states.async_set(GUARD_ID, "off")
    set_room_sensor(hass, 19.5)
    dark_head = trv_group[1]
    dark_head.set_available(False)
    entry = make_entry(trv_group.scenario, availability_guard_entity_id=GUARD_ID)

    with patch(CRITICAL_GRACE, NO_STARTUP_GRACE):
        await setup_entry(hass, entry)
        bt = await wait_for_startup(hass, entry)
        await TimeMachine(hass, bt).let_pass(LADDER_TICK_S)
        assert bt_issues(hass) == []

        hass.states.async_set(GUARD_ID, "unavailable")
        assert await wait_for(
            hass,
            lambda: (
                bt_issues(hass)
                == [
                    entry_issue_id(
                        entry.entry_id, "missing_entity", dark_head.entity_id
                    )
                ]
            ),
        )


async def test_the_options_keep_the_guard_and_its_grace_period(hass, trv_group):
    """Opening the settings and saving them changes neither.

    The grace period is off the default, so a form that offered the default
    in its place could not pass for one that kept the stored value.
    """
    hass.states.async_set(GUARD_ID, "on")
    set_room_sensor(hass, 19.5)
    entry = make_entry(
        trv_group.scenario,
        availability_guard_entity_id=GUARD_ID,
        availability_guard_grace_seconds=1800,
    )
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)

    user_form, *_ = await click_through_the_options(hass, entry)

    assert form_default(user_form, "availability_guard") == GUARD_ID
    assert form_default(user_form, "availability_guard_grace_seconds") == {
        "hours": 0,
        "minutes": 30,
        "seconds": 0,
    }
    settings = entry_settings(entry)
    assert settings["availability_guard"] == GUARD_ID
    assert settings["availability_guard_grace_seconds"] == 1800
    await wait_for_startup(hass, entry)
    bt = entry.runtime_data.climate
    assert bt.availability_guard_entity_id == GUARD_ID
    assert bt.availability_guard_grace_seconds == 1800
