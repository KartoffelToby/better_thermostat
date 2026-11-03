"""The unique ids of Better Thermostat's entities, spelled out.

A unique id is the key of an entity's registry entry: a changed spelling
orphans the entry, with its entity_id, its name and its history. Every id is
written out here character by character, and the tracking keys the stale-entity
cleanup records are checked to be the ids the entities carry.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from homeassistant.helpers.entity import Entity
import pytest

from custom_components.better_thermostat import (
    BetterThermostatData,
    number as number_platform,
    switch as switch_platform,
)
from custom_components.better_thermostat.number import (
    BetterThermostatPIDNumber,
    BetterThermostatPresetCoolNumber,
    BetterThermostatPresetNumber,
)
from custom_components.better_thermostat.sensor import (
    BetterThermostatExternalTemp1hEMASensor,
    BetterThermostatExternalTempSensor,
    BetterThermostatHeatingPowerSensor,
    BetterThermostatHeatLossSensor,
    BetterThermostatMpcGainSensor,
    BetterThermostatMpcKaSensor,
    BetterThermostatMpcLossSensor,
    BetterThermostatMpcV2CouplingSensor,
    BetterThermostatMpcV2DisturbanceSensor,
    BetterThermostatMpcV2RoomTimeConstantSensor,
    BetterThermostatMpcV2VirtualTempSensor,
    BetterThermostatPidErrorSensor,
    BetterThermostatPidOutputSensor,
    BetterThermostatTempSlopeSensor,
    BetterThermostatVirtualTempSensor,
    _cleanup_pid_number_entities,
    _cleanup_pid_switch_entities,
    _cleanup_preset_number_entities,
)
from custom_components.better_thermostat.switch import (
    BetterThermostatChildLockSwitch,
    BetterThermostatPIDAutoTuneSwitch,
    BetterThermostatReleaseTrvSwitch,
)
from custom_components.better_thermostat.tracked_entities import TrackedEntities
from custom_components.better_thermostat.utils.const import (
    CONF_CALIBRATION_MODE,
    CalibrationMode,
)
from custom_components.better_thermostat.utils.entry_schema import (
    TrvAdvanced,
    TrvSettings,
)
from tests.factories import ThermostatStandIn, make_entity_registry, trv_record

_TRV = "climate.trv_living"


def _bt_climate() -> ThermostatStandIn:
    bt = ThermostatStandIn()
    bt.unique_id = "bt_1"
    bt.cooler_entity_id = None
    bt.device_info = dict[str, str]()
    return bt


@pytest.mark.parametrize(
    ("entity", "expected"),
    [
        (lambda bt: BetterThermostatPresetNumber(bt, "eco"), "bt_1_preset_eco"),
        (
            lambda bt: BetterThermostatPresetCoolNumber(bt, "eco"),
            "bt_1_preset_eco_cool",
        ),
        (
            lambda bt: BetterThermostatPIDNumber(bt, _TRV, "ki", False),
            "bt_1_climate.trv_living_pid_ki",
        ),
        (
            lambda bt: BetterThermostatPIDAutoTuneSwitch(bt, _TRV, False),
            "bt_1_climate.trv_living_pid_auto_tune",
        ),
        (
            lambda bt: BetterThermostatChildLockSwitch(bt, _TRV, False),
            "bt_1_climate.trv_living_child_lock",
        ),
        (
            lambda bt: BetterThermostatReleaseTrvSwitch(bt, _TRV, False),
            "bt_1_climate.trv_living_release",
        ),
        (BetterThermostatExternalTempSensor, "bt_1_external_temp_ema"),
        (BetterThermostatExternalTemp1hEMASensor, "bt_1_external_temp_ema_1h"),
        (BetterThermostatTempSlopeSensor, "bt_1_temp_slope"),
        (BetterThermostatHeatingPowerSensor, "bt_1_heating_power"),
        (BetterThermostatHeatLossSensor, "bt_1_heat_loss"),
        (BetterThermostatVirtualTempSensor, "bt_1_virtual_temp"),
        (BetterThermostatMpcGainSensor, "bt_1_mpc_gain"),
        (BetterThermostatMpcLossSensor, "bt_1_mpc_loss"),
        (BetterThermostatMpcKaSensor, "bt_1_mpc_ka"),
        (BetterThermostatMpcV2VirtualTempSensor, "bt_1_mpc_v2_virtual_temp"),
        (BetterThermostatMpcV2CouplingSensor, "bt_1_mpc_v2_coupling"),
        (BetterThermostatMpcV2DisturbanceSensor, "bt_1_mpc_v2_disturbance"),
        (BetterThermostatMpcV2RoomTimeConstantSensor, "bt_1_mpc_v2_room_time_constant"),
        (BetterThermostatPidOutputSensor, "bt_1_pid_output"),
        (BetterThermostatPidErrorSensor, "bt_1_pid_error"),
    ],
)
def test_entity_unique_id_is_spelled_as_registered(entity, expected):
    """Each entity carries the unique id its registry entry is stored under."""
    assert entity(_bt_climate()).unique_id == expected


@pytest.mark.asyncio
async def test_tracked_number_and_switch_keys_are_the_entities_unique_ids():
    """The cleanup records each number and switch under the id it carries.

    A tracking key that differs from the entity's id would make the cleanup
    look up, and leave behind, a registry entry that does not exist.
    """
    bt = _bt_climate()
    bt.real_trvs = {
        _TRV: trv_record(
            entity_id=_TRV,
            advanced={CONF_CALIBRATION_MODE: CalibrationMode.PID_CALIBRATION},
        )
    }
    tracked = TrackedEntities()
    registry = make_entity_registry()
    registry.async_get_entity_id = MagicMock(return_value=None)

    await _cleanup_preset_number_entities(MagicMock(), registry, tracked, bt, {"eco"})
    await _cleanup_pid_number_entities(MagicMock(), registry, tracked, bt)
    await _cleanup_pid_switch_entities(MagicMock(), registry, tracked, bt)

    assert set(tracked.preset_numbers) == {
        BetterThermostatPresetNumber(bt, "eco").unique_id
    }
    assert set(tracked.pid_numbers) == {
        BetterThermostatPIDNumber(bt, _TRV, gain, False).unique_id
        for gain in ("kp", "ki", "kd")
    }
    assert set(tracked.switches) == {
        switch(bt, _TRV, False).unique_id
        for switch in (
            BetterThermostatPIDAutoTuneSwitch,
            BetterThermostatChildLockSwitch,
            BetterThermostatReleaseTrvSwitch,
        )
    }


def _pid_thermostat() -> ThermostatStandIn:
    bt = _bt_climate()
    bt.device_name = "Test BT"
    bt.preset_modes = ["none", "eco"]
    bt.cooler_entity_id = "climate.cooler"
    bt.outdoor_sensor_entity_id = None
    bt.weather_entity_id = None
    bt.has_contact_sensor = False
    advanced: TrvAdvanced = {"calibration_mode": CalibrationMode.PID_CALIBRATION}
    bt.real_trvs = {_TRV: trv_record(entity_id=_TRV, advanced=advanced)}
    bt.all_trvs = [TrvSettings(trv=_TRV, integration="generic", advanced=advanced)]
    return bt


async def _set_up(platform, bt: ThermostatStandIn) -> tuple[MagicMock, list[Entity]]:
    entry = MagicMock()
    entry.runtime_data = BetterThermostatData(climate=bt)
    add_entities = MagicMock()
    module = platform.__name__
    with (
        patch(f"{module}.er.async_get"),
        patch(f"{module}.remove_unclaimed_registry_entries"),
        patch(f"{module}.async_normalize_bt_entity_ids"),
    ):
        await platform.async_setup_entry(MagicMock(), entry, add_entities)
    (entities,) = add_entities.call_args.args
    return entry, entities


@pytest.mark.asyncio
async def test_number_setup_tracks_each_number_under_its_unique_id():
    """Setup records the preset and PID numbers under the ids they carry."""
    entry, numbers = await _set_up(number_platform, _pid_thermostat())

    tracked = entry.runtime_data.tracked_entities
    preset_ids = {
        n.unique_id for n in numbers if isinstance(n, BetterThermostatPresetNumber)
    }
    pid_ids = {n.unique_id for n in numbers if isinstance(n, BetterThermostatPIDNumber)}
    assert preset_ids == {"bt_1_preset_eco", "bt_1_preset_eco_cool"}
    assert set(tracked.preset_numbers) == preset_ids
    assert len(pid_ids) == 3
    assert set(tracked.pid_numbers) == pid_ids


@pytest.mark.asyncio
async def test_switch_setup_tracks_each_trv_switch_under_its_unique_id():
    """Setup records the per-TRV switches under the ids they carry."""
    entry, switches = await _set_up(switch_platform, _pid_thermostat())

    trv_switch_ids = {
        s.unique_id
        for s in switches
        if isinstance(
            s,
            BetterThermostatPIDAutoTuneSwitch
            | BetterThermostatChildLockSwitch
            | BetterThermostatReleaseTrvSwitch,
        )
    }
    assert len(trv_switch_ids) == 3
    assert set(entry.runtime_data.tracked_entities.switches) == trv_switch_ids
