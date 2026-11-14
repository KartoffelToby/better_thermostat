"""Unit tests for anomaly repair issues, logbook entries, and maintenance requests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import inspect
from types import MappingProxyType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.helpers import issue_registry as ir
import pytest

from custom_components.better_thermostat.core.fsm.anomaly import (
    AnomalyState,
    FaultPhase,
    FaultSlot,
    TrvFaultState,
)
from custom_components.better_thermostat.core.fsm.maintenance import (
    ANOMALY_MAINTENANCE_COOLDOWN_SECONDS,
    MaintenanceDue,
    MaintenanceIdle,
)
from custom_components.better_thermostat.core.fsm.window import WindowPhase, WindowState
from custom_components.better_thermostat.core.recorder import (
    _json_safe,
    _state_asdict,
    state_from_dict,
)
from custom_components.better_thermostat.utils.anomaly_repairs import (
    _schedule_logbook_entry,
    sync_anomaly_repairs_and_actions,
)
from custom_components.better_thermostat.utils.const import (
    CONF_VALVE_MAINTENANCE,
    DOMAIN,
)
from custom_components.better_thermostat.utils.helpers import entry_issue_id
from tests.factories import (
    DEFAULT_CONFIG_ENTRY_ID,
    DEFAULT_TRV_ID,
    make_bt,
    make_snapshot,
    make_state,
)

ACTIVE_SLOT = FaultSlot(phase=FaultPhase.ACTIVE, since=100.0, active_since=200.0)
CLEAR_SLOT = FaultSlot()


def _configured_bt(hass):
    """Build a thermostat stand-in connected to ``hass`` and a config entry id."""
    bt = make_bt(trv_ids=(DEFAULT_TRV_ID,))
    bt.hass = hass
    bt._unique_id = DEFAULT_CONFIG_ENTRY_ID
    bt.entity_id = "climate.test_bt"
    bt.is_removed = False
    bt._owned_tasks = set()
    return bt


def _open_issues(hass) -> set[str]:
    """Return open Better Thermostat repair issue ids in ``hass``."""
    return {
        issue_id for (domain, issue_id) in ir.async_get(hass).issues if domain == DOMAIN
    }


def test_sync_noop_when_anomaly_detection_disabled(hass):
    """Disabled anomaly detection skips repair synchronization."""
    bt = _configured_bt(hass)
    snapshot = make_snapshot()
    pre_state = make_state(anomaly_detection=False)
    post_state = make_state(
        anomaly_detection=False, anomaly=AnomalyState(no_heat_delivery=ACTIVE_SLOT)
    )
    sync_anomaly_repairs_and_actions(bt, snapshot, pre_state, post_state)
    assert _open_issues(hass) == set()


async def test_room_no_heat_delivery_creates_and_clears_repair_issue(hass):
    """Room-level NO_HEAT_DELIVERY manages heating_supply_failure repair issue."""
    bt = _configured_bt(hass)
    snapshot = make_snapshot()
    clear_state = make_state(anomaly_detection=True)
    active_state = make_state(
        anomaly_detection=True, anomaly=AnomalyState(no_heat_delivery=ACTIVE_SLOT)
    )

    sync_anomaly_repairs_and_actions(bt, snapshot, clear_state, active_state)
    expected_id = entry_issue_id(DEFAULT_CONFIG_ENTRY_ID, "heating_supply_failure")
    assert _open_issues(hass) == {expected_id}

    sync_anomaly_repairs_and_actions(bt, snapshot, active_state, clear_state)
    assert _open_issues(hass) == set()


async def test_rapid_and_parasitic_loss_schedule_logbook_without_repairs(hass):
    """Transient heat-loss anomalies record logbook entries without repair issues."""
    bt = _configured_bt(hass)
    snapshot = make_snapshot()
    clear_state = make_state(anomaly_detection=True)
    active_state = make_state(
        anomaly_detection=True,
        anomaly=AnomalyState(rapid_loss=ACTIVE_SLOT, parasitic_loss=ACTIVE_SLOT),
    )

    with patch(
        "custom_components.better_thermostat.utils.anomaly_repairs.async_fire_logbook_entry",
        new_callable=AsyncMock,
    ) as mock_logbook:
        sync_anomaly_repairs_and_actions(bt, snapshot, clear_state, active_state)
        await hass.async_block_till_done()
        assert mock_logbook.call_count == 2
        assert _open_issues(hass) == set()

        sync_anomaly_repairs_and_actions(bt, snapshot, active_state, clear_state)
        await hass.async_block_till_done()
        assert mock_logbook.call_count == 4
        assert _open_issues(hass) == set()


async def test_trv_no_heat_response_creates_and_clears_repair_issue(hass):
    """Per-TRV NO_HEAT_DELIVERY manages trv_no_heat_response repair issue."""
    bt = _configured_bt(hass)
    snapshot = make_snapshot()
    clear_state = make_state(anomaly_detection=True)
    active_state = make_state(
        anomaly_detection=True,
        anomaly=AnomalyState(
            trvs=MappingProxyType(
                {DEFAULT_TRV_ID: TrvFaultState(no_heat_delivery=ACTIVE_SLOT)}
            )
        ),
    )

    sync_anomaly_repairs_and_actions(bt, snapshot, clear_state, active_state)
    expected_id = entry_issue_id(
        DEFAULT_CONFIG_ENTRY_ID, "trv_no_heat_response", DEFAULT_TRV_ID
    )
    assert _open_issues(hass) == {expected_id}

    # Escalation to room-level NO_HEAT_DELIVERY replaces per-TRV issue with room issue
    room_active_state = make_state(
        anomaly_detection=True,
        anomaly=AnomalyState(
            no_heat_delivery=ACTIVE_SLOT,
            trvs=MappingProxyType(
                {DEFAULT_TRV_ID: TrvFaultState(no_heat_delivery=ACTIVE_SLOT)}
            ),
        ),
    )
    sync_anomaly_repairs_and_actions(bt, snapshot, active_state, room_active_state)
    room_id = entry_issue_id(DEFAULT_CONFIG_ENTRY_ID, "heating_supply_failure")
    assert _open_issues(hass) == {room_id}


async def test_valve_leak_creates_issue_and_requests_maintenance_once_per_cooldown(
    hass,
):
    """VALVE_STUCK_OPEN creates valve_leak_detected and triggers valve maintenance."""
    bt = _configured_bt(hass)
    bt.real_trvs[DEFAULT_TRV_ID].advanced[CONF_VALVE_MAINTENANCE] = True
    bt._run_valve_maintenance = AsyncMock()
    spawned: list[object] = []

    def _capture_spawn(coro, *, name: str):
        spawned.append(name)
        coro.close()

    bt._spawn_owned = MagicMock(side_effect=_capture_spawn)

    now = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
    snapshot = make_snapshot(now=now, now_monotonic=5000.0)
    idle_maint = MaintenanceIdle(next_due=now + timedelta(days=7))
    clear_state = make_state(anomaly_detection=True, maintenance=idle_maint)
    active_state = make_state(
        anomaly_detection=True,
        maintenance=idle_maint,
        anomaly=AnomalyState(
            trvs=MappingProxyType(
                {DEFAULT_TRV_ID: TrvFaultState(valve_stuck_open=ACTIVE_SLOT)}
            )
        ),
    )
    bt.kernel_state = active_state

    sync_anomaly_repairs_and_actions(bt, snapshot, clear_state, active_state)
    expected_id = entry_issue_id(
        DEFAULT_CONFIG_ENTRY_ID, "valve_leak_detected", DEFAULT_TRV_ID
    )
    assert _open_issues(hass) == {expected_id}
    assert isinstance(bt.kernel_state.maintenance, MaintenanceDue)
    assert bt.kernel_state.maintenance.last_anomaly_run_monotonic == pytest.approx(
        5000.0
    )
    assert len(spawned) == 1

    # Clear fault and return maintenance to idle with cooldown timestamp preserved
    post_run_maint = MaintenanceIdle(
        next_due=now + timedelta(days=7), last_anomaly_run_monotonic=5000.0
    )
    cleared_state = replace(clear_state, maintenance=post_run_maint)
    bt.kernel_state = cleared_state
    sync_anomaly_repairs_and_actions(bt, snapshot, active_state, cleared_state)
    assert _open_issues(hass) == set()

    # Re-trigger within cooldown: issue opens, maintenance stays idle
    in_cooldown_snapshot = make_snapshot(
        now=now + timedelta(hours=2),
        now_monotonic=5000.0 + ANOMALY_MAINTENANCE_COOLDOWN_SECONDS - 10.0,
    )
    re_active = replace(active_state, maintenance=post_run_maint)
    bt.kernel_state = re_active
    sync_anomaly_repairs_and_actions(bt, in_cooldown_snapshot, cleared_state, re_active)
    assert _open_issues(hass) == {expected_id}
    assert isinstance(bt.kernel_state.maintenance, MaintenanceIdle)
    assert len(spawned) == 1


@pytest.mark.parametrize(
    ("contact", "window_detection", "armed"),
    [
        ("window", True, False),
        ("door", True, False),
        # With window detection off an open contact shuts no valve, so it
        # does not stand in the way of the exercise either.
        ("window", False, True),
    ],
)
def test_a_leak_found_under_an_open_contact_starts_no_valve_exercise(
    hass, contact, window_detection, armed
):
    """The exercise opens the valve fully, as the scheduled one does."""
    bt = _configured_bt(hass)
    bt.real_trvs[DEFAULT_TRV_ID].advanced[CONF_VALVE_MAINTENANCE] = True
    bt._run_valve_maintenance = MagicMock(return_value=None)
    idle_maint = MaintenanceIdle(next_due=datetime(2026, 1, 17, 12, 0, tzinfo=UTC))
    open_contact = {contact: WindowState(phase=WindowPhase.OPEN)}
    clear_state = make_state(
        anomaly_detection=True,
        maintenance=idle_maint,
        window_detection=window_detection,
        **open_contact,
    )
    active_state = replace(
        clear_state,
        anomaly=AnomalyState(
            trvs={DEFAULT_TRV_ID: TrvFaultState(valve_stuck_open=ACTIVE_SLOT)}
        ),
    )
    bt.kernel_state = active_state

    snapshot = make_snapshot()
    sync_anomaly_repairs_and_actions(bt, snapshot, clear_state, active_state)

    assert isinstance(bt.kernel_state.maintenance, MaintenanceDue) is armed
    assert bt._run_valve_maintenance.called is armed
    # Either way the run is due now; under the open contact the schedule
    # holds it until the contact has closed.
    assert bt.kernel_state.maintenance.next_due == snapshot.now
    # The finding itself is reported either way.
    assert _open_issues(hass) == {
        entry_issue_id(DEFAULT_CONFIG_ENTRY_ID, "valve_leak_detected", DEFAULT_TRV_ID)
    }


def test_recorder_round_trips_last_anomaly_run_monotonic():
    """Flight recorder serializes and restores last_anomaly_run_monotonic."""
    now = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
    state = make_state(
        maintenance=MaintenanceIdle(next_due=now, last_anomaly_run_monotonic=4321.5)
    )
    raw = _json_safe(_state_asdict(state))
    assert isinstance(raw, dict)
    restored = state_from_dict(raw)
    assert restored == state


def test_transitions_without_a_hass_still_schedule_nothing(hass):
    """A detached entity updates no issue and schedules no logbook entry."""
    bt = _configured_bt(hass)
    bt.hass = None
    bt._unique_id = None
    snapshot = make_snapshot()
    clear_state = make_state(anomaly_detection=True)
    active_state = make_state(
        anomaly_detection=True,
        anomaly=AnomalyState(
            no_heat_delivery=ACTIVE_SLOT,
            rapid_loss=ACTIVE_SLOT,
            parasitic_loss=ACTIVE_SLOT,
            trvs=MappingProxyType(
                {
                    DEFAULT_TRV_ID: TrvFaultState(
                        no_heat_delivery=ACTIVE_SLOT, valve_stuck_open=ACTIVE_SLOT
                    )
                }
            ),
        ),
    )

    sync_anomaly_repairs_and_actions(bt, snapshot, clear_state, active_state)
    assert _open_issues(hass) == set()

    detached_pre = make_state(anomaly_detection=False)
    sync_anomaly_repairs_and_actions(bt, snapshot, detached_pre, clear_state)
    sync_anomaly_repairs_and_actions(bt, snapshot, active_state, clear_state)

    trv_active = make_state(
        anomaly_detection=True,
        anomaly=AnomalyState(
            trvs=MappingProxyType(
                {
                    DEFAULT_TRV_ID: TrvFaultState(
                        no_heat_delivery=ACTIVE_SLOT, valve_stuck_open=ACTIVE_SLOT
                    )
                }
            )
        ),
    )
    sync_anomaly_repairs_and_actions(bt, snapshot, clear_state, trv_active)
    sync_anomaly_repairs_and_actions(bt, snapshot, trv_active, clear_state)
    assert _open_issues(hass) == set()


async def test_a_closed_task_manager_drops_the_logbook_coroutine(hass):
    """A closed manager closes the logbook coroutine instead of scheduling it."""

    class _Manager:
        closed = False
        coro: Any = None

        def create_task(self, coro, name=None):
            self.coro = coro

    manager = _Manager()
    bt: Any = SimpleNamespace(
        hass=object(), task_manager=manager, device_name="Test BT"
    )
    _schedule_logbook_entry(bt, "anomaly_test", "test")
    assert manager.coro is not None
    assert inspect.getcoroutinestate(manager.coro) == "CORO_CLOSED"

    manager.closed = True
    with patch(
        "custom_components.better_thermostat.utils.anomaly_repairs.async_fire_logbook_entry"
    ) as mock_fire:
        _schedule_logbook_entry(bt, "anomaly_test", "test")
        mock_fire.assert_not_called()


def test_leak_on_a_trv_without_maintenance_leaves_maintenance_idle(hass):
    """A stuck-open valve only requests maintenance when the TRV enables it."""
    bt = _configured_bt(hass)
    bt.real_trvs[DEFAULT_TRV_ID].advanced[CONF_VALVE_MAINTENANCE] = False
    bt._run_valve_maintenance = MagicMock(return_value=None)
    idle_maint = MaintenanceIdle(next_due=datetime(2026, 1, 10, 12, 0, tzinfo=UTC))
    clear_state = make_state(anomaly_detection=True, maintenance=idle_maint)
    active_state = make_state(
        anomaly_detection=True,
        maintenance=idle_maint,
        anomaly=AnomalyState(
            trvs=MappingProxyType(
                {DEFAULT_TRV_ID: TrvFaultState(valve_stuck_open=ACTIVE_SLOT)}
            )
        ),
    )
    bt.kernel_state = active_state

    sync_anomaly_repairs_and_actions(bt, make_snapshot(), clear_state, active_state)

    assert isinstance(bt.kernel_state.maintenance, MaintenanceIdle)
    bt._run_valve_maintenance.assert_not_called()


async def test_a_non_coroutine_maintenance_run_is_not_spawned(hass):
    """A maintenance runner that returns no coroutine is left alone."""
    bt = _configured_bt(hass)
    bt.real_trvs[DEFAULT_TRV_ID].advanced[CONF_VALVE_MAINTENANCE] = True
    bt._run_valve_maintenance = MagicMock(return_value=None)
    bt._spawn_owned = MagicMock()
    idle_maint = MaintenanceIdle(next_due=datetime(2026, 1, 10, 12, 0, tzinfo=UTC))
    clear_state = make_state(anomaly_detection=True, maintenance=idle_maint)
    active_state = make_state(
        anomaly_detection=True,
        maintenance=idle_maint,
        anomaly=AnomalyState(
            trvs=MappingProxyType(
                {DEFAULT_TRV_ID: TrvFaultState(valve_stuck_open=ACTIVE_SLOT)}
            )
        ),
    )
    bt.kernel_state = active_state

    sync_anomaly_repairs_and_actions(bt, make_snapshot(), clear_state, active_state)

    bt._spawn_owned.assert_not_called()
    await hass.async_block_till_done()


async def test_a_real_maintenance_task_is_spawned(hass):
    """A coroutine maintenance run is spawned and tracked normally."""
    bt = _configured_bt(hass)
    bt.real_trvs[DEFAULT_TRV_ID].advanced[CONF_VALVE_MAINTENANCE] = True
    bt._run_valve_maintenance = AsyncMock()
    bt._spawn_owned = MagicMock(
        side_effect=lambda coro, name: asyncio.create_task(coro)
    )
    idle_maint = MaintenanceIdle(next_due=datetime(2026, 1, 10, 12, 0, tzinfo=UTC))
    clear_state = make_state(anomaly_detection=True, maintenance=idle_maint)
    active_state = make_state(
        anomaly_detection=True,
        maintenance=idle_maint,
        anomaly=AnomalyState(
            trvs=MappingProxyType(
                {DEFAULT_TRV_ID: TrvFaultState(valve_stuck_open=ACTIVE_SLOT)}
            )
        ),
    )
    bt.kernel_state = active_state

    sync_anomaly_repairs_and_actions(bt, make_snapshot(), clear_state, active_state)

    assert bt._spawn_owned.call_count == 1
    await hass.async_block_till_done()
