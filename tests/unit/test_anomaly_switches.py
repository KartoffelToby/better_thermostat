"""The anomaly detection and suppression switches and the thermostat behind them."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.core.fsm.anomaly import (
    AnomalyState,
    FaultPhase,
    FaultSlot,
)
from custom_components.better_thermostat.core.fsm.lifecycle import (
    LifecyclePhase,
    LifecycleState,
)
from custom_components.better_thermostat.switch import (
    BetterThermostatAnomalyDetectionSwitch,
    BetterThermostatAnomalySuppressionSwitch,
)
from custom_components.better_thermostat.utils.anomaly_evidence import (
    AnomalyEvidenceTracker,
)
from tests.factories import ThermostatStandIn, make_state

ACTIVE_FINDING = AnomalyState(
    rapid_loss=FaultSlot(phase=FaultPhase.ACTIVE, active_since=100.0)
)


def _thermostat(
    *, detection: bool = False, suppression: bool = False, starting: bool = False
) -> tuple[BetterThermostat, MagicMock]:
    """Return a thermostat shell and the mock its state is published through."""
    bt = object.__new__(BetterThermostat)
    lifecycle = (
        {"lifecycle": LifecycleState(phase=LifecyclePhase.INITIALISING)}
        if starting
        else {}
    )
    bt.kernel_state = make_state(
        anomaly_detection=detection,
        anomaly_suppression=suppression,
        anomaly=ACTIVE_FINDING if detection else AnomalyState(),
        **lifecycle,
    )
    bt.anomaly_evidence = AnomalyEvidenceTracker()
    bt._anomaly_followup_task = None
    bt._anomaly_followup_due = None
    bt.control_queue_task = asyncio.Queue(maxsize=1)
    bt.config = MagicMock(device_name="Test BT")
    bt.hass = MagicMock()
    publish = MagicMock()
    bt.async_write_ha_state = publish
    return bt, publish


def test_enabling_detection_keeps_the_regions_and_asks_for_a_cycle() -> None:
    bt, publish = _thermostat()

    bt.set_anomaly_detection(True)

    assert bt.kernel_state.anomaly_detection is True
    assert bt.control_queue_task.qsize() == 1
    publish.assert_called_once()


@pytest.mark.asyncio
async def test_disabling_detection_forgets_the_findings_and_their_timer() -> None:
    bt, publish = _thermostat(detection=True)
    timer = asyncio.get_running_loop().create_future()
    bt._anomaly_followup_task = timer  # type: ignore[assignment]
    bt._anomaly_followup_due = 500.0
    bt.anomaly_evidence.reset = MagicMock()

    with patch(
        "custom_components.better_thermostat.climate.sync_anomaly_repairs_and_actions"
    ) as sync:
        bt.set_anomaly_detection(False)

    assert bt.kernel_state.anomaly == AnomalyState()
    assert bt.kernel_state.anomaly_detection is False
    assert timer.cancelled()
    assert (bt._anomaly_followup_task, bt._anomaly_followup_due) == (None, None)
    bt.anomaly_evidence.reset.assert_called_once()
    sync.assert_called_once()
    publish.assert_called_once()


@pytest.mark.asyncio
async def test_disabling_detection_leaves_a_finished_timer_alone() -> None:
    bt, _ = _thermostat(detection=True)
    timer = asyncio.get_running_loop().create_future()
    timer.set_result(None)
    bt._anomaly_followup_task = timer  # type: ignore[assignment]

    with patch(
        "custom_components.better_thermostat.climate.sync_anomaly_repairs_and_actions"
    ):
        bt.set_anomaly_detection(False)

    assert bt._anomaly_followup_task is None
    assert not timer.cancelled()


def test_setting_detection_it_already_has_changes_nothing() -> None:
    bt, publish = _thermostat(detection=True)

    bt.set_anomaly_detection(True)

    publish.assert_not_called()
    assert bt.control_queue_task.empty()


def test_a_setting_restored_during_startup_waits_for_its_cycle() -> None:
    """Startup runs the first cycle itself; a restore asks for none."""
    bt, _ = _thermostat(starting=True)
    # Entity types hass as always set; a shell before registration has none.
    vars(bt)["hass"] = None

    bt.set_anomaly_detection(True)
    bt.set_anomaly_suppression(True)

    assert bt.kernel_state.anomaly_detection is True
    assert bt.kernel_state.anomaly_suppression is True
    assert bt.control_queue_task.empty()


@pytest.mark.parametrize("enabled", [True, False])
def test_suppression_follows_the_switch(enabled: bool) -> None:
    bt, publish = _thermostat(suppression=not enabled)

    bt.set_anomaly_suppression(enabled)

    assert bt.kernel_state.anomaly_suppression is enabled
    assert bt.control_queue_task.qsize() == 1
    publish.assert_called_once()


def test_setting_suppression_it_already_has_changes_nothing() -> None:
    bt, publish = _thermostat(suppression=True)

    bt.set_anomaly_suppression(True)

    publish.assert_not_called()


@pytest.mark.parametrize(
    ("switch_class", "setter"),
    [
        (BetterThermostatAnomalyDetectionSwitch, "set_anomaly_detection"),
        (BetterThermostatAnomalySuppressionSwitch, "set_anomaly_suppression"),
    ],
)
@pytest.mark.parametrize("turn_on", [True, False])
@pytest.mark.asyncio
async def test_each_switch_hands_its_position_to_the_thermostat(
    switch_class: type, setter: str, turn_on: bool
) -> None:
    bt_climate = ThermostatStandIn()
    bt_climate.unique_id = "bt_1"
    switch = switch_class(bt_climate)
    switch.async_write_ha_state = MagicMock()

    if turn_on:
        await switch.async_turn_on()
    else:
        await switch.async_turn_off()

    getattr(bt_climate, setter).assert_called_once_with(turn_on)
    switch.async_write_ha_state.assert_called_once()
