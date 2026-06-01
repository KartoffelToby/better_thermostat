"""Pure-logic helpers for periodic TRV valve maintenance.

All functions are side-effect-free (aside from the async callbacks they
receive) and can be tested without Home Assistant.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from random import randint

from homeassistant.components.climate.const import HVACMode
from homeassistant.core import State
from homeassistant.util import dt as dt_util

from .const import CONF_VALVE_MAINTENANCE, CalibrationType

_LOGGER = logging.getLogger(__name__)

# Type alias for the nested TRV config dicts used throughout the codebase.
TrvMap = dict[str, dict[str, object]]

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MaintenanceTrvInfo:
    """Snapshot of a single TRV needed during valve maintenance."""

    entity_id: str
    cur_mode: str
    cur_temp: float | None
    use_direct_valve: bool
    max_temp: float
    min_temp: float


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _get_advanced(info: dict[str, object]) -> dict[str, object]:
    """Safely extract the ``advanced`` sub-dict from a TRV config entry."""
    adv = info.get("advanced")
    return adv if isinstance(adv, dict) else {}


def collect_maintenance_trvs(real_trvs: TrvMap) -> list[str]:
    """Return entity-ids of TRVs that have valve maintenance enabled."""
    result: list[str] = []
    for trv_id, info in real_trvs.items():
        adv = _get_advanced(info)
        if bool(adv.get(CONF_VALVE_MAINTENANCE, False)):
            result.append(trv_id)
    return result


def compute_next_maintenance(
    real_trvs: TrvMap, trv_ids: list[str], *, now: datetime | None = None
) -> datetime:
    """Compute the next maintenance datetime based on TRV quirks.

    Uses the *minimum* interval across all enabled TRVs and adds ~7 %
    random jitter.
    """
    if now is None:
        now = dt_util.now()

    min_interval_hours = 168  # default 7 days
    for trv_id in trv_ids:
        quirks = (real_trvs.get(trv_id, {}) or {}).get("model_quirks")
        interval = int(getattr(quirks, "VALVE_MAINTENANCE_INTERVAL_HOURS", 168))
        min_interval_hours = min(min_interval_hours, interval)

    variance = max(1, int(min_interval_hours * 0.07))
    return now + timedelta(hours=min_interval_hours + randint(0, variance))


def compute_initial_maintenance(
    real_trvs: TrvMap, trv_ids: list[str], *, now: datetime | None = None
) -> datetime:
    """Compute the *first* maintenance datetime after startup.

    Randomises within ``[1 h, min(5 d, interval)]`` so that multiple
    BT instances don't all fire at once.
    """
    if now is None:
        now = dt_util.now()

    min_interval_hours = 168
    for trv_id in trv_ids:
        quirks = (real_trvs.get(trv_id, {}) or {}).get("model_quirks")
        interval = int(getattr(quirks, "VALVE_MAINTENANCE_INTERVAL_HOURS", 168))
        min_interval_hours = min(min_interval_hours, interval)

    max_delay_hours = min(24 * 5, min_interval_hours)
    delay_hours = randint(1, max(2, max_delay_hours))
    return now + timedelta(hours=delay_hours)


# ---------------------------------------------------------------------------
# Snapshot builder
# ---------------------------------------------------------------------------


def build_trv_snapshots(
    real_trvs: TrvMap,
    trv_ids: list[str],
    get_state: Callable[[str], State | None],
    device_name: str,
) -> list[MaintenanceTrvInfo]:
    """Build per-TRV snapshots needed for the maintenance cycle.

    *get_state* should be ``hass.states.get``.  TRVs whose HA state is
    ``None`` are silently skipped (logged at debug level).
    """
    infos: list[MaintenanceTrvInfo] = []
    for trv_id in trv_ids:
        trv_state = get_state(trv_id)
        if trv_state is None:
            _LOGGER.debug(
                "better_thermostat %s: maintenance skip %s (state None)",
                device_name,
                trv_id,
            )
            continue

        trv_data = real_trvs.get(trv_id, {}) or {}
        valve_entity = trv_data.get("valve_position_entity")
        quirks = trv_data.get("model_quirks")
        support_valve = bool(valve_entity) or bool(
            getattr(quirks, "override_set_valve", None)
        )
        adv = _get_advanced(trv_data)
        cal_type = adv.get("calibration")
        use_direct = bool(
            support_valve and cal_type == CalibrationType.DIRECT_VALVE_BASED
        )

        raw_max = trv_data.get("max_temp", 30)
        raw_min = trv_data.get("min_temp", 5)
        infos.append(
            MaintenanceTrvInfo(
                entity_id=trv_id,
                cur_mode=trv_state.state,
                cur_temp=trv_state.attributes.get("temperature"),
                use_direct_valve=use_direct,
                max_temp=float(raw_max) if isinstance(raw_max, (int, float)) else 30.0,
                min_temp=float(raw_min) if isinstance(raw_min, (int, float)) else 5.0,
            )
        )
    return infos


# ---------------------------------------------------------------------------
# Async step helpers
# ---------------------------------------------------------------------------

SetValveFn = Callable[[str, int], Awaitable[bool]]
SetTemperatureFn = Callable[[str, float], Awaitable[None]]
SetHvacModeFn = Callable[[str, str], Awaitable[None]]


async def _set_valve_pct(trv_id: str, pct: int, set_valve_fn: SetValveFn) -> bool:
    """Set valve percentage via callback."""
    try:
        return bool(await set_valve_fn(trv_id, int(pct)))
    except Exception:
        return False


async def open_step(
    info: MaintenanceTrvInfo,
    *,
    set_valve_fn: SetValveFn,
    set_temperature_fn: SetTemperatureFn,
) -> None:
    """Open a TRV valve fully."""
    if info.use_direct_valve:
        await _set_valve_pct(info.entity_id, 100, set_valve_fn)
        return
    # Temp-extremes fallback: only when TRV is not OFF (OFF TRVs ignore temp changes)
    if info.cur_mode != HVACMode.OFF:
        await set_temperature_fn(info.entity_id, info.max_temp)


async def close_step(
    info: MaintenanceTrvInfo,
    *,
    set_valve_fn: SetValveFn,
    set_temperature_fn: SetTemperatureFn,
) -> None:
    """Close a TRV valve fully."""
    if info.use_direct_valve:
        await _set_valve_pct(info.entity_id, 0, set_valve_fn)
        return
    # Temp-extremes fallback: only when TRV is not OFF (OFF TRVs ignore temp changes)
    if info.cur_mode != HVACMode.OFF:
        await set_temperature_fn(info.entity_id, info.min_temp)


async def restore_one(
    info: MaintenanceTrvInfo,
    *,
    set_temperature_fn: SetTemperatureFn,
    set_hvac_mode_fn: SetHvacModeFn,
) -> None:
    """Restore a TRV to its pre-maintenance state."""
    if info.cur_temp is not None:
        try:
            await set_temperature_fn(info.entity_id, info.cur_temp)
        except Exception:
            pass
    try:
        await set_hvac_mode_fn(info.entity_id, info.cur_mode)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


async def run_valve_maintenance(
    infos: list[MaintenanceTrvInfo],
    *,
    set_valve_fn: SetValveFn,
    set_temperature_fn: SetTemperatureFn,
    set_hvac_mode_fn: SetHvacModeFn,
    device_name: str,
    cycle_sleep: float = 30,
) -> None:
    """Execute 2 x open/close cycles on all TRVs, then restore state.

    This is the pure async orchestrator.  State mutations on
    ``self`` (ignore_states, in_maintenance, control_queue) stay in
    ``climate.py``'s wrapper.
    """
    _LOGGER.info(
        "better_thermostat %s: starting valve maintenance for %d TRV(s)",
        device_name,
        len(infos),
    )

    # Execute in synchronized steps across all TRVs (much faster than sequential).
    # Open all → wait → close all → wait (repeat twice).
    for i in range(2):
        _LOGGER.debug(
            "better_thermostat %s: valve maintenance cycle %d/2 starting for %d TRV(s)",
            device_name,
            i + 1,
            len(infos),
        )
        await asyncio.gather(
            *(
                open_step(
                    info,
                    set_valve_fn=set_valve_fn,
                    set_temperature_fn=set_temperature_fn,
                )
                for info in infos
            ),
            return_exceptions=True,
        )
        await asyncio.sleep(cycle_sleep)
        await asyncio.gather(
            *(
                close_step(
                    info,
                    set_valve_fn=set_valve_fn,
                    set_temperature_fn=set_temperature_fn,
                )
                for info in infos
            ),
            return_exceptions=True,
        )
        await asyncio.sleep(cycle_sleep)

    # Restore
    await asyncio.gather(
        *(
            restore_one(
                info,
                set_temperature_fn=set_temperature_fn,
                set_hvac_mode_fn=set_hvac_mode_fn,
            )
            for info in infos
        ),
        return_exceptions=True,
    )

    _LOGGER.info("better_thermostat %s: valve maintenance finished", device_name)
