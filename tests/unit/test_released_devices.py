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
from custom_components.better_thermostat.trv import Trv
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
    bt.contact_sensed_open = False
    bt.clock = FakeClock()
    bt._compute_hvac_action = MagicMock(return_value=HVACAction.HEATING)
    bt._get_outdoor_temperature = MagicMock(return_value=None)
    bt._heating_tracker = MagicMock(wraps=HeatingPowerTracker())
    bt._loss_tracker = MagicMock(wraps=HeatLossTracker())
    return bt


def _releasable_room() -> ThermostatStandIn:
    bt = _learning_room(released=False)
    bt.startup_running = False
    bt._initialize_arrived_trvs = AsyncMock()
    bt._external_temperature_keepalive = AsyncMock()
    bt.control_queue_task = MagicMock()
    return bt


@pytest.mark.parametrize("released", [True, False], ids=["released", "controlled"])
@pytest.mark.asyncio
async def test_the_learners_take_nothing_from_a_released_room(released):
    """Heating power and heat loss learn only from a room the control drives.

    A release holds them as a learning pause does: they still follow the
    room, and the reading teaches them nothing.
    """
    bt = _releasable_room()

    with patch(f"{_CLIMATE}.check_critical_entities", AsyncMock()):
        await BetterThermostat.async_set_devices_released(bt, released)
    await BetterThermostat.calculate_heating_power(bt)
    await BetterThermostat.calculate_heat_loss(bt)

    for tracker in (bt._heating_tracker, bt._loss_tracker):
        assert tracker.update.call_count == 1
        assert tracker.update.call_args.kwargs["learning_paused"] is released
    assert bt.learning_paused is False


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
    bt._initialize_arrived_trvs.assert_not_called()
    bt._external_temperature_keepalive.assert_not_called()


@pytest.mark.asyncio
async def test_taking_the_devices_back_resumes_with_one_cycle():
    """The devices are taken back with one control cycle and nothing else.

    A TRV startup left uninitialised is set up first, the outage checks run
    again and the TRVs that take an external temperature get the room's,
    which an unchanged sensor would not send again before the keepalive;
    the learners have nothing to give up.
    """
    bt = _releasable_room()
    bt.devices_released = True

    with patch(f"{_CLIMATE}.check_critical_entities", AsyncMock()) as critical:
        await BetterThermostat.async_set_devices_released(bt, False)

    assert bt.devices_released is False
    bt._initialize_arrived_trvs.assert_awaited_once()
    bt._external_temperature_keepalive.assert_awaited_once_with()
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

    bt._initialize_arrived_trvs.assert_not_called()
    bt._external_temperature_keepalive.assert_not_called()
    bt.control_queue_task.put_nowait.assert_not_called()


_FIRST_TRV = "climate.first_trv"
_SECOND_TRV = "climate.second_trv"


def _room_setting_up_two_trvs() -> ThermostatStandIn:
    """A room about to initialise two TRVs, its devices not released."""
    bt = ThermostatStandIn()
    bt.device_name = "Test BT"
    bt.devices_released = False
    bt.hass = MagicMock()
    bt.hass.states.get.return_value = None
    bt.real_trvs = {
        entity_id: Trv(entity_id=entity_id, calibration=None)
        for entity_id in (_FIRST_TRV, _SECOND_TRV)
    }
    bt.all_entities = dict.fromkeys(bt.real_trvs)
    bt._configured_temperature_step = None
    bt.bt_target_temperature_step = None
    return bt


@pytest.mark.parametrize("released_in", ["init", "initial_tweak"])
@pytest.mark.asyncio
async def test_a_release_during_setup_stops_the_writes_that_follow(released_in):
    """Setting a TRV up writes to it, and released devices get no writes.

    The release comes in while the first TRV is being set up. No write of
    its setup that comes after the release runs, nor any of the second
    TRV's, and a TRV whose setup was cut short waits to be set up once the
    devices are taken back.
    """
    bt = _room_setting_up_two_trvs()
    steps: list[tuple[str, str]] = []

    def step(name):
        async def run(_bt, entity_id):
            steps.append((name, entity_id))
            if name == released_in:
                bt.devices_released = True

        return run

    with (
        patch(f"{_CLIMATE}.init", autospec=True, side_effect=step("init")),
        patch(
            f"{_CLIMATE}.initial_tweak",
            autospec=True,
            side_effect=step("initial_tweak"),
        ),
    ):
        await BetterThermostat._initialize_trvs(bt)

    assert (
        steps
        == [("init", _FIRST_TRV), ("initial_tweak", _FIRST_TRV)][
            : 1 if released_in == "init" else 2
        ]
    )
    assert bt.real_trvs[_FIRST_TRV].awaiting_initialization is (released_in == "init")
    assert bt.real_trvs[_SECOND_TRV].awaiting_initialization is True


@pytest.mark.asyncio
async def test_a_trv_released_while_it_arrives_waits_for_the_take_back():
    """A TRV set up when it reports again stays out once the release comes in.

    It is set up again, and controlled, once the devices are taken back.
    """
    bt = _room_setting_up_two_trvs()
    bt.is_removed = False
    bt.in_maintenance = False
    bt._trvs_initializing = set()
    for trv in bt.real_trvs.values():
        trv.awaiting_initialization = True

    async def released_meanwhile(_entity_ids):
        bt.devices_released = True
        return set()

    bt._initialize_trvs = AsyncMock(side_effect=released_meanwhile)
    with (
        patch(f"{_CLIMATE}.is_trv_available", return_value=True),
        patch(f"{_CLIMATE}.request_control_cycle") as request,
    ):
        await BetterThermostat._initialize_arrived_trvs(bt)

    bt._initialize_trvs.assert_awaited_once_with([_FIRST_TRV])
    assert all(trv.awaiting_initialization for trv in bt.real_trvs.values())
    request.assert_not_called()


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
