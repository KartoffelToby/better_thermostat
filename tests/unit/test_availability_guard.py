"""Whether an unavailable TRV is reported, as the availability guard sees it.

The availability guard is an entity that says whether the TRVs are expected
to have power. Thermostats fed by the heating system go dark when it is
switched off, and that outage is no fault: it raises neither a warning nor a
repair issue. A TRV that is still away once the guard has been on for its
grace period is reported as any other outage is.
"""

from datetime import datetime, timedelta
import logging
from unittest.mock import MagicMock, patch

from homeassistant.core import State
import pytest

from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.watcher import (
    check_critical_entities,
    should_report_unavailable_trvs,
)
from tests.factories import ThermostatStandIn

GUARD_ID = "binary_sensor.heat_pump_powered"
TRV_ID = "climate.wall_thermostat"
GRACE_SECONDS = 600.0
WATCHER_IR = "custom_components.better_thermostat.utils.watcher.ir"


def _guard(state: str, changed_at: datetime) -> State:
    return State(GUARD_ID, state, last_changed=changed_at)


class TestShouldReportUnavailableTrvs:
    """The decision on its own, against the guard's state and the time."""

    def test_a_thermostat_without_a_guard_reports_every_outage(self):
        assert should_report_unavailable_trvs(None, FakeClock().now(), GRACE_SECONDS)

    def test_a_guard_that_is_off_excuses_the_outage(self):
        """No length of time spent off turns an expected outage into a fault."""
        now = FakeClock().now()
        guard = _guard("off", now - timedelta(days=3))

        assert not should_report_unavailable_trvs(guard, now, GRACE_SECONDS)

    def test_a_guard_that_just_turned_on_gives_the_trvs_time_to_return(self):
        now = FakeClock().now()
        guard = _guard("on", now - timedelta(seconds=GRACE_SECONDS - 1))

        assert not should_report_unavailable_trvs(guard, now, GRACE_SECONDS)

    def test_a_guard_on_for_the_whole_grace_period_reports_the_outage(self):
        now = FakeClock().now()
        guard = _guard("on", now - timedelta(seconds=GRACE_SECONDS))

        assert should_report_unavailable_trvs(guard, now, GRACE_SECONDS)

    @pytest.mark.parametrize("state", ["unavailable", "unknown", "standby"])
    def test_a_guard_that_says_neither_on_nor_off_cannot_excuse_an_outage(self, state):
        """A guard that does not answer is no evidence that the power is off."""
        now = FakeClock().now()
        guard = _guard(state, now)

        assert should_report_unavailable_trvs(guard, now, GRACE_SECONDS)


@pytest.fixture
def thermostat() -> ThermostatStandIn:
    """Return a thermostat with one TRV and an availability guard."""
    bt = ThermostatStandIn()
    bt.hass = MagicMock()
    bt.device_name = "Wall BT"
    bt.real_trvs = {TRV_ID: Trv(entity_id=TRV_ID)}
    bt.devices_errors = list[str]()
    bt.devices_states = dict[str, dict[str, str | None]](
        {TRV_ID: {"battery": None, "battery_id": None}}
    )
    bt._next_battery_read = dict[str, float]()
    bt.clock = FakeClock()
    bt.availability_guard_entity_id = GUARD_ID
    bt.availability_guard_grace_seconds = GRACE_SECONDS
    return bt


def _world(bt: ThermostatStandIn, guard: State | None, trv_state: str) -> None:
    """Answer state lookups with ``guard`` and a TRV in ``trv_state``."""
    states = {TRV_ID: State(TRV_ID, trv_state)}
    if guard is not None:
        states[GUARD_ID] = guard
    bt.hass.states.get.side_effect = states.get


class TestCheckCriticalEntitiesWithAGuard:
    """The critical-entity check asks the guard before it reports a TRV."""

    async def test_a_trv_away_while_the_guard_is_off_is_not_reported(
        self, thermostat, caplog
    ):
        _world(thermostat, _guard("off", thermostat.clock.now()), "unavailable")

        with caplog.at_level(logging.DEBUG), patch(WATCHER_IR) as ir:
            await check_critical_entities(thermostat)

        assert thermostat.devices_errors == []
        ir.async_create_issue.assert_not_called()
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    async def test_a_guard_turning_off_withdraws_a_report_already_made(
        self, thermostat
    ):
        """The TRVs can drop out before the guard says the power went off.

        The report they raised in between describes an expected outage, so it
        does not outlast the guard turning off.
        """
        thermostat.devices_errors = [TRV_ID]
        _world(thermostat, _guard("off", thermostat.clock.now()), "unavailable")

        with patch(WATCHER_IR) as ir:
            await check_critical_entities(thermostat)

        assert thermostat.devices_errors == []
        ir.async_create_issue.assert_not_called()
        (call,) = ir.async_delete_issue.call_args_list
        assert call.args[2].endswith(f"missing_entity_{TRV_ID}")

    async def test_a_trv_still_away_after_the_grace_period_is_reported(
        self, thermostat, caplog
    ):
        _world(thermostat, _guard("on", thermostat.clock.now()), "unavailable")

        with patch(WATCHER_IR) as ir:
            await check_critical_entities(thermostat)
            assert thermostat.devices_errors == []
            ir.async_create_issue.assert_not_called()

            thermostat.clock.advance(GRACE_SECONDS)
            await check_critical_entities(thermostat)

        assert thermostat.devices_errors == [TRV_ID]
        ir.async_create_issue.assert_called_once()
        assert ir.async_create_issue.call_args.kwargs["translation_key"] == (
            "missing_entity"
        )
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert [r.getMessage() for r in warnings] == [
            f"better_thermostat Wall BT: Critical entity {TRV_ID} is unavailable"
        ]

    async def test_a_guard_that_does_not_answer_lets_the_outage_be_reported(
        self, thermostat
    ):
        _world(thermostat, _guard("unavailable", thermostat.clock.now()), "unavailable")

        with patch(WATCHER_IR) as ir:
            await check_critical_entities(thermostat)

        assert thermostat.devices_errors == [TRV_ID]
        ir.async_create_issue.assert_called_once()

    async def test_a_configured_guard_that_does_not_exist_reports_the_outage(
        self, thermostat
    ):
        _world(thermostat, None, "unavailable")

        with patch(WATCHER_IR) as ir:
            await check_critical_entities(thermostat)

        assert thermostat.devices_errors == [TRV_ID]
        ir.async_create_issue.assert_called_once()

    async def test_a_trv_that_answers_is_left_alone_whatever_the_guard_says(
        self, thermostat
    ):
        """The guard excuses outages; it does not invent them."""
        _world(thermostat, _guard("off", thermostat.clock.now()), "heat")

        with patch(WATCHER_IR) as ir:
            await check_critical_entities(thermostat)

        assert thermostat.devices_errors == []
        ir.async_create_issue.assert_not_called()
