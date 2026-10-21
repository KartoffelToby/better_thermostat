"""The room-level release of the devices on the thermostat and its switch.

While the devices are released the room learns nothing from them, and
taking them back resumes control with one cycle.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.climate.const import HVACAction
import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.switch import (
    BetterThermostatReleaseDevicesSwitch,
)
from custom_components.better_thermostat.utils.thermal_learning import (
    HeatingPowerTracker,
    HeatLossTracker,
)
from tests.factories import ThermostatStandIn

_CLIMATE = "custom_components.better_thermostat.climate"


def _learning_room(*, released: bool) -> ThermostatStandIn:
    """A room in the middle of a heating stretch, its trackers spied on."""
    bt = ThermostatStandIn()
    bt.device_name = "Test BT"
    bt.devices_released = released
    bt.room_temperature = 20.0
    bt.heat_target_temperature = 21.0
    bt.contact_open = False
    bt.clock = FakeClock()
    bt._compute_hvac_action = MagicMock(return_value=HVACAction.HEATING)
    bt._get_outdoor_temperature = MagicMock(return_value=None)
    bt._heating_tracker = MagicMock(wraps=HeatingPowerTracker())
    bt._loss_tracker = MagicMock(wraps=HeatLossTracker())
    return bt


@pytest.mark.parametrize(
    ("released", "updates"), [pytest.param(True, 0, id="released"), (False, 1)]
)
@pytest.mark.asyncio
async def test_the_learners_take_nothing_from_a_released_room(released, updates):
    """Heating power and heat loss learn only from a room the control drives."""
    bt = _learning_room(released=released)

    await BetterThermostat.calculate_heating_power(bt)
    await BetterThermostat.calculate_heat_loss(bt)

    assert bt._heating_tracker.update.call_count == updates
    assert bt._loss_tracker.update.call_count == updates


def _releasable_room() -> ThermostatStandIn:
    bt = _learning_room(released=False)
    bt.startup_running = False
    bt._initialize_arrived_trvs = AsyncMock()
    bt.control_queue_task = MagicMock()
    return bt


@pytest.mark.asyncio
async def test_releasing_gives_up_the_stretches_the_learners_follow():
    """What the room does from the release on is not the control's doing."""
    bt = _releasable_room()

    with patch(f"{_CLIMATE}.check_critical_entities", AsyncMock()) as critical:
        await BetterThermostat.async_set_devices_released(bt, True)

    assert bt.devices_released is True
    bt._heating_tracker.abort_cycle.assert_called_once()
    bt._loss_tracker.abort_cycle.assert_called_once()
    critical.assert_awaited_once_with(bt)
    bt.control_queue_task.put_nowait.assert_not_called()
    bt._initialize_arrived_trvs.assert_not_awaited()


@pytest.mark.asyncio
async def test_taking_the_devices_back_resumes_with_one_cycle():
    """The devices are taken back with one control cycle and nothing else.

    A TRV startup left uninitialised is set up first, and the outage checks
    run again; the learners have nothing to give up.
    """
    bt = _releasable_room()
    bt.devices_released = True

    with patch(f"{_CLIMATE}.check_critical_entities", AsyncMock()) as critical:
        await BetterThermostat.async_set_devices_released(bt, False)

    assert bt.devices_released is False
    bt._initialize_arrived_trvs.assert_awaited_once()
    critical.assert_awaited_once_with(bt)
    bt.control_queue_task.put_nowait.assert_called_once_with(bt)
    bt._heating_tracker.abort_cycle.assert_not_called()


@pytest.mark.parametrize("released", [True, False])
@pytest.mark.asyncio
async def test_setting_the_release_it_already_has_changes_nothing(released):
    """A repeated switch command neither aborts learning nor queues a cycle."""
    bt = _releasable_room()
    bt.devices_released = released

    with patch(f"{_CLIMATE}.check_critical_entities", AsyncMock()) as critical:
        await BetterThermostat.async_set_devices_released(bt, released)

    critical.assert_not_awaited()
    bt.control_queue_task.put_nowait.assert_not_called()
    bt._heating_tracker.abort_cycle.assert_not_called()


@pytest.mark.asyncio
async def test_taking_the_devices_back_during_startup_leaves_them_to_it():
    """A startup still running initialises and controls the devices itself."""
    bt = _releasable_room()
    bt.devices_released = True
    bt.startup_running = True

    with patch(f"{_CLIMATE}.check_critical_entities", AsyncMock()):
        await BetterThermostat.async_set_devices_released(bt, False)

    bt._initialize_arrived_trvs.assert_not_awaited()
    bt.control_queue_task.put_nowait.assert_not_called()


def _release_switch(
    *, released: bool
) -> tuple[BetterThermostatReleaseDevicesSwitch, AsyncMock]:
    """Return the switch of a thermostat and the setter it calls."""
    bt_climate = ThermostatStandIn()
    bt_climate.unique_id = "bt_1"
    bt_climate.devices_released = released
    setter = AsyncMock()
    bt_climate.async_set_devices_released = setter
    switch = BetterThermostatReleaseDevicesSwitch(bt_climate)
    switch.async_write_ha_state = MagicMock()
    return switch, setter


@pytest.mark.parametrize("released", [True, False])
def test_the_switch_shows_the_thermostats_release(released):
    """The thermostat holds the setting; the switch shows it."""
    switch, _ = _release_switch(released=released)

    assert switch.is_on is released
    assert switch.unique_id == "bt_1_release_devices"


@pytest.mark.parametrize("turn_on", [True, False])
@pytest.mark.asyncio
async def test_the_switch_sets_the_thermostats_release(turn_on):
    """Turning the switch on releases the devices, turning it off takes them back."""
    switch, setter = _release_switch(released=not turn_on)

    if turn_on:
        await switch.async_turn_on()
    else:
        await switch.async_turn_off()

    setter.assert_awaited_once_with(turn_on)
