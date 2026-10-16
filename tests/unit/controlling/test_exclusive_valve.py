"""Exclusive direct control resolves logical safety gates before one valve write."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.components.climate.const import PRESET_BOOST, HVACMode
from homeassistant.core import State
import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.const import (
    CalibrationMode,
    CalibrationType,
)
from custom_components.better_thermostat.utils.controlling import control_trv

CTRL = "custom_components.better_thermostat.utils.controlling"
ALGORITHMS = [
    CalibrationMode.MPC_CALIBRATION,
    CalibrationMode.MPC_V2_CALIBRATION,
    CalibrationMode.PID_CALIBRATION,
    CalibrationMode.TPI_CALIBRATION,
    CalibrationMode.HEATING_POWER_CALIBRATION,
]


def thermostat(mode=CalibrationMode.MPC_CALIBRATION):
    write = AsyncMock(return_value=True)
    trv = Trv(
        entity_id="climate.shelly",
        integration="shelly",
        direct_valve_exclusive=True,
        adapter=SimpleNamespace(set_valve=write),
        hvac_modes=[HVACMode.HEAT],
        min_temp=4,
        max_temp=30,
        advanced={
            "calibration": CalibrationType.DIRECT_VALVE_BASED,
            "calibration_mode": mode,
            "no_off_system_mode": True,
        },
    )
    bt = SimpleNamespace(
        real_trvs={trv.entity_id: trv},
        _temp_lock=asyncio.Lock(),
        task_manager=SimpleNamespace(create_task=AsyncMock()),
        bt_hvac_mode=HVACMode.HEAT,
        contact_open=False,
        call_for_heat=True,
        cur_temp=19,
        bt_target_temp=22,
        preset_mode=None,
        device_name="Studio",
    )
    return bt, trv, write


@pytest.mark.parametrize("algorithm", ALGORITHMS)
@pytest.mark.parametrize(
    "mode, contact, demand, boost, output, maximum, expected",
    [
        ("heat", False, True, False, 37, 100, 37),
        ("off", False, True, False, 37, 100, 0),
        ("heat", True, True, False, 37, 100, 0),
        ("heat", False, False, False, 37, 100, 0),
        ("heat", True, True, True, 37, 100, 0),
        ("heat", False, False, True, 37, 100, 0),
        ("off", False, True, True, 37, 100, 0),
        ("heat", False, True, True, 37, 63, 63),
        ("heat", False, True, False, 90, 63, 63),
        ("heat", False, True, False, -10, 100, 0),
        ("heat", False, True, False, 110, 100, 100),
        ("heat", False, True, False, None, 100, 0),
        ("heat", False, True, False, float("nan"), 100, 0),
        ("heat", False, True, False, float("inf"), 100, 0),
        ("heat", False, True, False, "bad", 100, 0),
    ],
)
async def test_one_final_write(
    algorithm, mode, contact, demand, boost, output, maximum, expected
):
    bt, trv, write = thermostat(algorithm)
    bt.bt_hvac_mode, bt.contact_open, bt.call_for_heat = mode, contact, demand
    bt.preset_mode = PRESET_BOOST if boost else None
    trv.valve_max_opening = maximum
    # Poison both fallback channels with previous-cycle demand.
    trv.calibration_balance = trv.balance = {"valve_percent": 99, "apply_valve": True}

    def calculate(*args):
        if output is not None:
            trv.calibration_balance = {"valve_percent": output, "apply_valve": True}

    with (
        patch(f"{CTRL}.calculate_calibration_setpoint", side_effect=calculate),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        assert await control_trv(bt, trv.entity_id)
    write.assert_awaited_once_with(bt, trv.entity_id, expected)
    assert trv.last_valve_percent == expected
    assert trv.last_temperature is None
    assert trv.last_hvac_mode is None
    assert trv.last_calibration is None
    assert trv.ignore_trv_states is False
    assert not bt._temp_lock.locked()
    bt.task_manager.create_task.assert_not_called()


async def test_calculation_exception_closes_and_failure_is_retried_next_cycle():
    bt, trv, write = thermostat()
    write.side_effect = [False, True]
    with (
        patch(f"{CTRL}.calculate_calibration_setpoint", side_effect=RuntimeError),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        assert await control_trv(bt, trv.entity_id)
        assert trv.last_valve_percent is None
        assert trv.ignore_trv_states is False
        assert await control_trv(bt, trv.entity_id)
    assert write.await_count == 2
    assert trv.last_valve_percent == 0


async def test_cancelled_write_releases_guards():
    bt, trv, write = thermostat()
    bt.bt_hvac_mode = "off"
    write.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await control_trv(bt, trv.entity_id)
    assert not trv.ignore_trv_states
    assert not bt._temp_lock.locked()


def maintenance_thermostat():
    bt, trv, write = thermostat()
    trv.valve_position_entity = "number.valve"
    bt.in_maintenance = False
    bt.ignore_states = False
    bt.control_queue_task = asyncio.Queue()
    state = State(trv.entity_id, "off", {"temperature": 22, "hvac_modes": ["heat"]})
    bt.hass = SimpleNamespace(states=SimpleNamespace(get=lambda _: state))
    bt.hass.config = SimpleNamespace(units=SimpleNamespace(temperature_unit="°C"))
    trv.adapter.set_temperature = AsyncMock()
    trv.adapter.set_hvac_mode = AsyncMock()
    return bt, trv, write


async def test_maintenance_does_not_restore_physical_climate():
    bt, trv, write = maintenance_thermostat()
    with patch("asyncio.sleep", new=AsyncMock()):
        await BetterThermostat._run_valve_maintenance(bt, [trv.entity_id])
    assert [call.args[2] for call in write.await_args_list] == [100, 0, 100, 0]
    trv.adapter.set_temperature.assert_not_awaited()
    trv.adapter.set_hvac_mode.assert_not_awaited()


async def test_maintenance_prevents_a_waiting_control_write():
    bt, trv, write = thermostat()
    bt.in_maintenance = True
    assert await control_trv(bt, trv.entity_id)
    write.assert_not_awaited()


async def test_non_shelly_control_dispatch_during_maintenance_is_unchanged():
    bt, trv, write = maintenance_thermostat()
    trv.integration = "mqtt"
    bt.in_maintenance = True
    # Ordinary devices still reach their existing state-conversion path.
    with (
        patch(f"{CTRL}.convert_outbound_states", return_value=None) as convert,
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        assert await control_trv(bt, trv.entity_id) is False
    convert.assert_called_once()
    write.assert_not_awaited()


async def test_shared_cooler_cannot_bypass_exclusive_valve_ownership():
    from custom_components.better_thermostat.utils.controlling import control_cooler

    bt, trv, _ = thermostat()
    bt.cooler_entity_id = trv.entity_id
    bt.last_cooler_mode_decided = HVACMode.COOL
    # This thermostat has no climate-service interface: the guard must stand
    # down before both service calls and their success caches are reached.
    await control_cooler(bt)
    assert bt.last_cooler_mode_decided == HVACMode.OFF
