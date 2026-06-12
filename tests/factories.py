"""Shared builders for unit-test fixtures.

The canonical home of the recurring mock shapes: kernel inputs
(``make_snapshot``/``make_state``) and the entity mock (``make_bt``).
Tests import from here instead of re-declaring the MagicMock shape per
file.
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
    """Return a post-startup KernelState; overridable per test."""
    return replace(running_kernel_state(), **overrides)


def make_snapshot(**overrides) -> WorldSnapshot:
    """Return a heating-mode snapshot with two TRVs; overridable per test."""
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
    """Return a Trv with identity model quirks; overridable per test."""
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

    Keyword arguments beyond the listed ones are forwarded into every
    TRV built for ``trv_ids``.
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
