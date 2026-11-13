"""Home Assistant Repairs, Logbook, and maintenance actions for anomaly transitions.

The pure anomaly FSM in :mod:`core.fsm.anomaly` owns fault detection, dwell,
recovery, and liveness. This shell module observes transitions between the
pre-cycle and post-cycle :class:`~core.decide.KernelState` and performs the
side effects:

- Creating and deleting Home Assistant Repairs issues for hardware faults
  (``heating_supply_failure``, ``trv_no_heat_response``, ``valve_leak_detected``).
- Recording Logbook entries on transition into visibility and on return to clear.
- Requesting an immediate one-shot valve maintenance cycle via
  :func:`~core.fsm.maintenance.request_now` when a TRV with valve maintenance
  enabled transitions into ``VALVE_STUCK_OPEN``.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import inspect
import logging
from typing import TYPE_CHECKING, Final

from homeassistant.helpers import issue_registry as ir

from custom_components.better_thermostat.core.fsm.maintenance import (
    MaintenanceDue,
    request_now as maintenance_request_now,
)
from custom_components.better_thermostat.utils.const import DOMAIN
from custom_components.better_thermostat.utils.helpers import (
    async_fire_logbook_entry,
    device_released,
    entry_issue_id,
)
from custom_components.better_thermostat.utils.valve_maintenance import (
    collect_maintenance_trvs,
)

if TYPE_CHECKING:
    from custom_components.better_thermostat.climate import BetterThermostat
    from custom_components.better_thermostat.core.decide import KernelState
    from custom_components.better_thermostat.core.snapshot import WorldSnapshot

_LOGGER = logging.getLogger(__name__)

ANOMALY_LEARN_MORE_URL: Final[str] = (
    "https://better-thermostat.org/faq/anomaly-detection"
)


def _room_no_heat_visible(state: KernelState) -> bool:
    """Return whether room-wide ``NO_HEAT_DELIVERY`` is visible in ``state``."""
    return state.anomaly_detection and state.anomaly.no_heat_delivery.is_visible


def _room_rapid_loss_visible(state: KernelState) -> bool:
    """Return whether room-wide ``RAPID_HEAT_LOSS`` is visible in ``state``."""
    return state.anomaly_detection and state.anomaly.rapid_loss.is_visible


def _room_parasitic_loss_visible(state: KernelState) -> bool:
    """Return whether room-wide ``SUSTAINED_PARASITIC_LOSS`` is visible in ``state``."""
    return state.anomaly_detection and state.anomaly.parasitic_loss.is_visible


def _trv_no_heat_visible(state: KernelState, entity_id: str) -> bool:
    """Return whether per-TRV ``NO_HEAT_DELIVERY`` is visible for ``entity_id``."""
    if not state.anomaly_detection or state.anomaly.no_heat_delivery.is_visible:
        return False
    slot = state.anomaly.trvs.get(entity_id)
    return slot is not None and slot.no_heat_delivery.is_visible


def _trv_stuck_open_visible(state: KernelState, entity_id: str) -> bool:
    """Return whether per-TRV ``VALVE_STUCK_OPEN`` is visible for ``entity_id``."""
    if not state.anomaly_detection:
        return False
    slot = state.anomaly.trvs.get(entity_id)
    return slot is not None and slot.valve_stuck_open.is_visible


def _schedule_logbook_entry(self: BetterThermostat, key: str, default_msg: str) -> None:
    """Schedule a non-blocking logbook entry when an event loop is running."""
    if self.hass is None or self.task_manager.closed:
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    coro = async_fire_logbook_entry(self, key, default_msg)
    task = self.task_manager.create_task(
        coro, name=f"bt_anomaly_logbook_{self.device_name}_{key}"
    )
    if not isinstance(task, asyncio.Task):
        coro.close()


def _sync_room_repairs_and_logbook(
    self: BetterThermostat,
    pre_state: KernelState,
    post_state: KernelState,
    config_entry_id: str | None,
) -> None:
    """Synchronize room-level repair issues and logbook entries."""
    was_no_heat = _room_no_heat_visible(pre_state)
    now_no_heat = _room_no_heat_visible(post_state)
    if not was_no_heat and now_no_heat:
        _LOGGER.warning(
            "better_thermostat %s: heating supply failure detected "
            "(all TRVs calling for heat without room temperature response)",
            self.device_name,
        )
        if self.hass is not None and config_entry_id is not None:
            ir.async_create_issue(
                hass=self.hass,
                domain=DOMAIN,
                issue_id=entry_issue_id(config_entry_id, "heating_supply_failure"),
                is_fixable=False,
                is_persistent=False,
                learn_more_url=ANOMALY_LEARN_MORE_URL,
                severity=ir.IssueSeverity.WARNING,
                translation_key="heating_supply_failure",
                translation_placeholders={"name": self.device_name},
            )
        _schedule_logbook_entry(
            self, "anomaly_heating_supply_failure", "detected a heating supply failure"
        )
    elif was_no_heat and not now_no_heat:
        _LOGGER.info(
            "better_thermostat %s: heating supply failure resolved", self.device_name
        )
        if self.hass is not None and config_entry_id is not None:
            ir.async_delete_issue(
                self.hass,
                DOMAIN,
                entry_issue_id(config_entry_id, "heating_supply_failure"),
            )
        _schedule_logbook_entry(
            self,
            "anomaly_heating_supply_failure_resolved",
            "heating supply failure resolved",
        )

    was_rapid = _room_rapid_loss_visible(pre_state)
    now_rapid = _room_rapid_loss_visible(post_state)
    if not was_rapid and now_rapid:
        _LOGGER.info(
            "better_thermostat %s: rapid heat loss detected (suspected open window)",
            self.device_name,
        )
        _schedule_logbook_entry(
            self,
            "anomaly_rapid_heat_loss",
            "detected rapid heat loss (suspected open window)",
        )
    elif was_rapid and not now_rapid:
        _LOGGER.info("better_thermostat %s: rapid heat loss ended", self.device_name)
        _schedule_logbook_entry(
            self, "anomaly_rapid_heat_loss_resolved", "rapid heat loss ended"
        )

    was_parasitic = _room_parasitic_loss_visible(pre_state)
    now_parasitic = _room_parasitic_loss_visible(post_state)
    if not was_parasitic and now_parasitic:
        _LOGGER.info(
            "better_thermostat %s: sustained elevated heat loss detected",
            self.device_name,
        )
        _schedule_logbook_entry(
            self,
            "anomaly_sustained_parasitic_loss",
            "detected sustained elevated heat loss",
        )
    elif was_parasitic and not now_parasitic:
        _LOGGER.info(
            "better_thermostat %s: sustained elevated heat loss ended", self.device_name
        )
        _schedule_logbook_entry(
            self,
            "anomaly_sustained_parasitic_loss_resolved",
            "sustained elevated heat loss ended",
        )


def _sync_trv_repairs_and_actions(
    self: BetterThermostat,
    snapshot: WorldSnapshot | None,
    pre_state: KernelState,
    post_state: KernelState,
    config_entry_id: str | None,
) -> None:
    """Synchronize per-TRV repair issues, logbook entries, and valve maintenance."""
    all_trvs = sorted(
        set(pre_state.anomaly.trvs) | set(post_state.anomaly.trvs) | set(self.real_trvs)
    )
    maintenance_enabled = set(collect_maintenance_trvs(self.real_trvs))
    trvs_for_maintenance: list[str] = []

    for entity_id in all_trvs:
        released = device_released(self, entity_id)
        was_no_heat = _trv_no_heat_visible(pre_state, entity_id)
        now_no_heat = _trv_no_heat_visible(post_state, entity_id) and not released
        if not was_no_heat and now_no_heat:
            _LOGGER.warning(
                "better_thermostat %s: TRV %s shows no heating response",
                self.device_name,
                entity_id,
            )
            if self.hass is not None and config_entry_id is not None:
                ir.async_create_issue(
                    hass=self.hass,
                    domain=DOMAIN,
                    issue_id=entry_issue_id(
                        config_entry_id, "trv_no_heat_response", entity_id
                    ),
                    is_fixable=False,
                    is_persistent=False,
                    learn_more_url=ANOMALY_LEARN_MORE_URL,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="trv_no_heat_response",
                    translation_placeholders={
                        "name": self.device_name,
                        "entity": entity_id,
                    },
                )
            _schedule_logbook_entry(
                self,
                "anomaly_trv_no_heat_response",
                f"detected no heating response from {entity_id}",
            )
        elif was_no_heat and not now_no_heat:
            _LOGGER.info(
                "better_thermostat %s: TRV %s heating response restored",
                self.device_name,
                entity_id,
            )
            if self.hass is not None and config_entry_id is not None:
                ir.async_delete_issue(
                    self.hass,
                    DOMAIN,
                    entry_issue_id(config_entry_id, "trv_no_heat_response", entity_id),
                )
            _schedule_logbook_entry(
                self,
                "anomaly_trv_no_heat_response_resolved",
                f"heating response from {entity_id} restored",
            )

        was_stuck = _trv_stuck_open_visible(pre_state, entity_id)
        now_stuck = _trv_stuck_open_visible(post_state, entity_id) and not released
        if not was_stuck and now_stuck:
            _LOGGER.error(
                "better_thermostat %s: TRV %s valve appears stuck open or leaking",
                self.device_name,
                entity_id,
            )
            if self.hass is not None and config_entry_id is not None:
                ir.async_create_issue(
                    hass=self.hass,
                    domain=DOMAIN,
                    issue_id=entry_issue_id(
                        config_entry_id, "valve_leak_detected", entity_id
                    ),
                    is_fixable=False,
                    is_persistent=False,
                    learn_more_url=ANOMALY_LEARN_MORE_URL,
                    severity=ir.IssueSeverity.ERROR,
                    translation_key="valve_leak_detected",
                    translation_placeholders={
                        "name": self.device_name,
                        "entity": entity_id,
                    },
                )
            _schedule_logbook_entry(
                self,
                "anomaly_valve_leak_detected",
                f"detected a leaking or stuck-open valve on {entity_id}",
            )
            if entity_id in maintenance_enabled:
                trvs_for_maintenance.append(entity_id)
        elif was_stuck and not now_stuck:
            _LOGGER.info(
                "better_thermostat %s: TRV %s valve leak resolved",
                self.device_name,
                entity_id,
            )
            if self.hass is not None and config_entry_id is not None:
                ir.async_delete_issue(
                    self.hass,
                    DOMAIN,
                    entry_issue_id(config_entry_id, "valve_leak_detected", entity_id),
                )
            _schedule_logbook_entry(
                self,
                "anomaly_valve_leak_detected_resolved",
                f"valve leak on {entity_id} resolved",
            )

    if trvs_for_maintenance:
        prev_maint = self.kernel_state.maintenance
        now_dt = snapshot.now if snapshot is not None else self.clock.now()
        now_mono = (
            snapshot.now_monotonic if snapshot is not None else self.clock.monotonic()
        )
        next_maint = maintenance_request_now(prev_maint, now_dt, now_mono)
        self.kernel_state = replace(self.kernel_state, maintenance=next_maint)
        if isinstance(next_maint, MaintenanceDue) and not isinstance(
            prev_maint, MaintenanceDue
        ):
            coro = self._run_valve_maintenance(trvs_for_maintenance)
            if inspect.iscoroutine(coro):
                task = self._spawn_owned(
                    coro, name=f"bt_anomaly_valve_maintenance_{self.device_name}"
                )
                if not isinstance(task, asyncio.Task):
                    coro.close()


def sync_anomaly_repairs_and_actions(
    self: BetterThermostat,
    snapshot: WorldSnapshot | None,
    pre_state: KernelState,
    post_state: KernelState,
) -> None:
    """Apply repair-issue, logbook, and maintenance side effects for anomaly transitions.

    Parameters
    ----------
    self : BetterThermostat
        The climate entity instance.
    snapshot : WorldSnapshot | None
        The current cycle's observation snapshot, or ``None`` when called
        outside a control cycle.
    pre_state : KernelState
        The kernel state before :func:`~core.decide.decide`.
    post_state : KernelState
        The kernel state returned by :func:`~core.decide.decide`.
    """
    if not pre_state.anomaly_detection and not post_state.anomaly_detection:
        return
    raw_entry_id = getattr(self, "_config_entry_id", None)
    config_entry_id = raw_entry_id if isinstance(raw_entry_id, str) else None
    _sync_room_repairs_and_logbook(self, pre_state, post_state, config_entry_id)
    _sync_trv_repairs_and_actions(
        self, snapshot, pre_state, post_state, config_entry_id
    )
