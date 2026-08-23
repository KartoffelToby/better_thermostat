"""Shared builders for unit-test fixtures.

The canonical home of the recurring mock shapes: kernel inputs
(``make_snapshot``/``make_state``), the entity mock for the control path
(``make_bt``) and the one for the reported state attributes
(``make_state_attributes_bt``). Tests import from here instead of
re-declaring the MagicMock shape per file.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import MagicMock

from homeassistant.components.climate.const import HVACAction, HVACMode

from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.decide import (
    KernelState,
    running_kernel_state,
)
from custom_components.better_thermostat.core.snapshot import (
    HvacMode as CoreHvacMode,
    TrvReported,
    WorldSnapshot,
)
from custom_components.better_thermostat.trv import Trv

DEFAULT_TRV_ID = "climate.trv"


def make_state(**overrides) -> KernelState:
    """Return a post-startup KernelState; overridable per test.

    Parameters
    ----------
    **overrides
        Field values applied via ``dataclasses.replace``.

    Returns
    -------
    KernelState
        A running kernel state with the requested overrides.
    """
    return replace(running_kernel_state(), **overrides)


def make_snapshot(**overrides) -> WorldSnapshot:
    """Return a heating-mode snapshot with two TRVs; overridable per test.

    Parameters
    ----------
    **overrides
        Field values that replace the snapshot defaults.

    Returns
    -------
    WorldSnapshot
        A heating-mode snapshot with the requested overrides.
    """
    defaults = {
        "now": datetime(2026, 1, 2, 8, 30, tzinfo=UTC),
        "now_monotonic": 1000.0,
        "target_temp": 21.0,
        "hvac_mode": CoreHvacMode.HEAT,
        "room_temp": 19.5,
        "call_for_heat": True,
        "tolerance": 0.3,
        "trvs": {
            "climate.trv1": TrvReported(entity_id="climate.trv1"),
            "climate.trv2": TrvReported(entity_id="climate.trv2"),
        },
    }
    defaults.update(overrides)
    return WorldSnapshot(**defaults)


def make_trv(entity_id: str = DEFAULT_TRV_ID, **fields) -> Trv:
    """Return a Trv with identity model quirks; overridable per test.

    Parameters
    ----------
    entity_id : str
        Entity id for the built TRV.
    **fields
        Field values that replace the TRV defaults.

    Returns
    -------
    Trv
        A TRV with identity calibration quirks and the requested fields.
    """
    quirks = MagicMock()
    quirks.fix_local_calibration.side_effect = lambda _self, _eid, offset: float(offset)
    quirks.fix_target_temperature_calibration.side_effect = (
        lambda _self, _eid, temperature: float(temperature)
    )
    defaults = {
        "advanced": {},
        "current_temperature": 21.0,
        "last_calibration": 0.0,
        "local_calibration_step": 0.1,
        "local_calibration_min": -5.0,
        "local_calibration_max": 5.0,
        "target_temp_step": 0.1,
        "min_temp": 5.0,
        "max_temp": 30.0,
        "model_quirks": quirks,
    }
    defaults.update(fields)
    return Trv.from_legacy_dict(entity_id, defaults)


def make_bt(
    *,
    trv_ids: tuple[str, ...] = (DEFAULT_TRV_ID,),
    hvac_action=HVACAction.IDLE,
    cur_temp: float | None = 20.0,
    bt_target_temp: float | None = 21.0,
    tolerance: float = 0.3,
    **trv_fields,
) -> MagicMock:
    """Return the recurring entity mock: clock, kernel regions, queues, TRVs.

    Parameters
    ----------
    trv_ids : tuple of str
        Entity ids for the TRVs to build on the mock.
    hvac_action : HVACAction
        Initial HVAC action reported by the mock.
    cur_temp : float | None
        Current room temperature.
    bt_target_temp : float | None
        Target temperature.
    tolerance : float
        Control tolerance band.
    **trv_fields
        Forwarded into every TRV built for ``trv_ids``.

    Returns
    -------
    MagicMock
        The entity mock with clock, kernel regions, queues and TRVs.
    """
    bt = MagicMock()
    bt.name = "better_thermostat"
    bt.device_name = "Test BT"
    bt.tolerance = tolerance
    bt.hvac_action = hvac_action
    bt.cur_temp = cur_temp
    bt.bt_target_temp = bt_target_temp
    bt.outdoor_sensor = None
    bt.weather_entity = None
    bt.bt_hvac_mode = HVACMode.HEAT
    bt.window_open = False
    bt.call_for_heat = True
    bt.ignore_states = False
    bt.clock = FakeClock()
    bt.kernel_state = running_kernel_state()
    bt.control_queue_task = asyncio.Queue(maxsize=1)
    bt.window_queue_task = asyncio.Queue(maxsize=1)
    bt.real_trvs = {
        entity_id: make_trv(entity_id, **trv_fields) for entity_id in trv_ids
    }
    return bt


def make_state_attributes_bt(**overrides) -> MagicMock:
    """Return the entity mock ``extra_state_attributes`` can be read from.

    The property JSON-encodes several of the values it reads, so the
    collections it serialises have to be real containers rather than
    MagicMock children.

    Parameters
    ----------
    **overrides
        Attribute values applied on top of the defaults.

    Returns
    -------
    MagicMock
        The entity mock with every attribute the property touches.
    """
    bt = MagicMock()
    bt.window_open = False
    bt.call_for_heat = True
    bt.last_change = datetime(2026, 5, 18, tzinfo=UTC)
    bt._saved_temperature = None
    bt._preset_temperature = None
    bt._current_humidity = None
    bt.humidity_sensor_entity_id = None
    bt.last_main_hvac_mode = HVACMode.HEAT
    bt.off_temperature = None
    bt.tolerance = 0.5
    bt.bt_target_temp_step = 0.5
    bt.heating_power = 0.1
    bt.heat_loss_rate = 0.0
    bt.devices_errors = []
    bt.devices_states = {}
    bt.cur_temp_filtered = 20.5
    bt.degraded_mode = False
    bt.unavailable_sensors = []
    bt.real_trvs = {}
    bt.heating_cycles = []
    bt.loss_cycles = []
    bt.last_heating_power_stats = {}
    bt.last_heat_loss_stats = {}
    bt.next_valve_maintenance = None
    bt._preset_cool_temperatures = {}
    for name, value in overrides.items():
        setattr(bt, name, value)
    return bt
