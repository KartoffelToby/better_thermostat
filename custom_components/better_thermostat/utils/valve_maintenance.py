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

from ..trv import Trv
from .const import CONF_VALVE_MAINTENANCE, CalibrationType
from .helpers import device_offers_mode

_LOGGER = logging.getLogger(__name__)

# Type alias for the per-entity Trv registry (``real_trvs``).
TrvMap = dict[str, Trv]

# Data types


@dataclass(frozen=True)
class MaintenanceTrvInfo:
    """Snapshot of a single TRV needed during valve maintenance."""

    entity_id: str
    cur_mode: str
    cur_temp: float | None
    use_direct_valve: bool
    max_temp: float
    min_temp: float
    wake_mode: str | None = None
    """Device-native mode to switch into for the exercise, or ``None``.

    Only set for a TRV that is ``off`` and driven through temperature
    extremes: such a device ignores setpoint writes, so the cycle would
    move nothing. Valve-driven TRVs are written through a number entity
    that works while the device is off, so they are never woken.
    """


# Modes a TRV can be woken into for the exercise, most preferred first.
_WAKE_MODE_PREFERENCE: tuple[str, ...] = (
    HVACMode.HEAT,
    HVACMode.AUTO,
    HVACMode.HEAT_COOL,
)


# Pure helpers


def pick_wake_mode(
    cur_mode: str, use_direct_valve: bool, hvac_modes: object
) -> str | None:
    """Return the mode to exercise an ``off`` TRV in, or ``None``.

    Parameters
    ----------
    cur_mode : str
        The TRV's current device-native HVAC mode.
    use_direct_valve : bool
        Whether the TRV is driven by writing a valve percentage.
    hvac_modes : object
        The modes the TRV reports, as read from its state attributes.
        Any spelling: ``HVACMode`` members, plain strings or
        ``"HVACMode.HEAT"``.

    Returns
    -------
    str | None
        The first supported mode from ``_WAKE_MODE_PREFERENCE``, or
        ``None`` when the TRV needs no wake or offers no usable mode.
    """
    if use_direct_valve or cur_mode != HVACMode.OFF:
        return None
    if not isinstance(hvac_modes, (list, tuple, set)):
        return None
    for candidate in _WAKE_MODE_PREFERENCE:
        if device_offers_mode(hvac_modes, candidate):
            return candidate
    return None


def mode_needs_restoring(
    info: MaintenanceTrvInfo, get_state: Callable[[str], State | None]
) -> bool:
    """Whether the TRV's HVAC mode has to be written back after the run.

    Parameters
    ----------
    info : MaintenanceTrvInfo
        The snapshot the run started from, carrying the mode to restore and
        the wake mode the run switched into, if any.
    get_state : Callable[[str], State | None]
        ``hass.states.get``. A TRV that reports no state is taken to need
        the restore: nothing then says the mode is where it belongs.

    Returns
    -------
    bool
        True when the run woke the TRV, or when the mode it reports is no
        longer the one the snapshot was taken in.
    """
    if info.wake_mode is not None:
        return True
    state = get_state(info.entity_id)
    if state is None:
        return True
    return state.state != info.cur_mode


def _get_advanced(info: Trv) -> dict[str, object]:
    """Safely extract the ``advanced`` dict from a Trv entry."""
    adv = info.advanced
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
        _trv = real_trvs.get(trv_id)
        quirks = _trv.model_quirks if _trv is not None else None
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
        _trv = real_trvs.get(trv_id)
        quirks = _trv.model_quirks if _trv is not None else None
        interval = int(getattr(quirks, "VALVE_MAINTENANCE_INTERVAL_HOURS", 168))
        min_interval_hours = min(min_interval_hours, interval)

    max_delay_hours = min(24 * 5, min_interval_hours)
    delay_hours = randint(1, max(2, max_delay_hours))
    return now + timedelta(hours=delay_hours)


# Snapshot builder


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

        trv_data = real_trvs.get(trv_id)
        if trv_data is None:
            _LOGGER.debug(
                "better_thermostat %s: maintenance skip %s (not in real_trvs)",
                device_name,
                trv_id,
            )
            continue
        support_valve = trv_data.capabilities().supports_valve_write
        adv = _get_advanced(trv_data)
        cal_type = adv.get("calibration")
        use_direct = bool(
            support_valve and cal_type == CalibrationType.DIRECT_VALVE_BASED
        )

        raw_max = trv_data.max_temp
        raw_min = trv_data.min_temp
        infos.append(
            MaintenanceTrvInfo(
                entity_id=trv_id,
                cur_mode=trv_state.state,
                cur_temp=trv_state.attributes.get("temperature"),
                use_direct_valve=use_direct,
                max_temp=float(raw_max) if isinstance(raw_max, (int, float)) else 30.0,
                min_temp=float(raw_min) if isinstance(raw_min, (int, float)) else 5.0,
                wake_mode=pick_wake_mode(
                    trv_state.state, use_direct, trv_state.attributes.get("hvac_modes")
                ),
            )
        )
    return infos


# Async step helpers

SetValveFn = Callable[[str, int], Awaitable[bool]]
SetTemperatureFn = Callable[[str, float], Awaitable[None]]
SetHvacModeFn = Callable[[str, str], Awaitable[None]]


async def _set_valve_pct(trv_id: str, pct: int, set_valve_fn: SetValveFn) -> bool:
    """Set valve percentage via callback."""
    try:
        return bool(await set_valve_fn(trv_id, int(pct)))
    except Exception:
        return False


def _temp_cycle_reaches_valve(info: MaintenanceTrvInfo) -> bool:
    """Whether writing a setpoint moves this TRV's valve.

    An ``off`` TRV ignores setpoint writes, so the cycle only reaches it
    once ``wake_step`` has switched it into ``wake_mode``.
    """
    return info.cur_mode != HVACMode.OFF or info.wake_mode is not None


async def wake_step(
    info: MaintenanceTrvInfo, *, set_hvac_mode_fn: SetHvacModeFn
) -> None:
    """Switch an ``off`` TRV into its exercise mode.

    ``restore_one`` puts ``cur_mode`` back at the end of the run, so the
    TRV returns to ``off`` afterwards.
    """
    if info.wake_mode is None:
        return
    await set_hvac_mode_fn(info.entity_id, info.wake_mode)


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
    if _temp_cycle_reaches_valve(info):
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
    if _temp_cycle_reaches_valve(info):
        await set_temperature_fn(info.entity_id, info.min_temp)


async def restore_one(
    info: MaintenanceTrvInfo,
    *,
    set_temperature_fn: SetTemperatureFn,
    set_hvac_mode_fn: SetHvacModeFn,
    get_state: Callable[[str], State | None],
) -> None:
    """Restore a TRV to its pre-maintenance state.

    The setpoint is always written back; the mode only when it moved.
    A device that reports a single mode implements no setter for it, so
    writing the mode it is already in raises ``NotImplementedError`` inside
    Home Assistant's climate component and buys nothing in exchange.

    Two things move a mode here, and the guard answers to both. The run
    itself moves one only through ``wake_step``, which is gated on
    ``wake_mode``, so that alone says a mode was written and has to go back
    regardless of what the device has published since. Anything else that
    moved it, a valve write a device answers by switching itself on among
    them, shows up as a reported mode that is no longer the one the snapshot
    was taken in.
    """
    if info.cur_temp is not None:
        try:
            await set_temperature_fn(info.entity_id, info.cur_temp)
        except Exception:
            _LOGGER.debug(
                "better_thermostat: restoring the setpoint of %s failed",
                info.entity_id,
                exc_info=True,
            )
    if not mode_needs_restoring(info, get_state):
        _LOGGER.debug(
            "better_thermostat: %s is still in mode %s, leaving it alone",
            info.entity_id,
            info.cur_mode,
        )
        return
    try:
        await set_hvac_mode_fn(info.entity_id, info.cur_mode)
    except Exception:
        _LOGGER.debug(
            "better_thermostat: restoring the HVAC mode of %s failed",
            info.entity_id,
            exc_info=True,
        )


# Main orchestrator


async def run_valve_maintenance(
    infos: list[MaintenanceTrvInfo],
    *,
    set_valve_fn: SetValveFn,
    set_temperature_fn: SetTemperatureFn,
    set_hvac_mode_fn: SetHvacModeFn,
    get_state: Callable[[str], State | None],
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

    # Wake TRVs that are off, otherwise the temperature cycle below moves
    # nothing on them. restore_one puts them back to off at the end.
    wake_results = await asyncio.gather(
        *(wake_step(info, set_hvac_mode_fn=set_hvac_mode_fn) for info in infos),
        return_exceptions=True,
    )

    # Which TRVs the cycle below actually moves. A failed wake leaves a TRV
    # off, so a setpoint write would either move nothing or, on a device that
    # reads a setpoint as "turn on", heat it without the cycle asking for it.
    # A TRV that offered no wake mode at all is unreachable for the same
    # reason. Both are still restored below.
    cycled: list[MaintenanceTrvInfo] = []
    for info, result in zip(infos, wake_results):
        if isinstance(result, BaseException):
            _LOGGER.warning(
                "better_thermostat %s: could not wake %s for maintenance (%s), "
                "skipping its temperature cycle",
                device_name,
                info.entity_id,
                result,
            )
            continue
        if info.use_direct_valve or _temp_cycle_reaches_valve(info):
            cycled.append(info)

    if not cycled:
        # Nothing to open or close, so the four cycle sleeps would be waits
        # around no work at all. Drop straight through to the restore.
        _LOGGER.info(
            "better_thermostat %s: no TRV reachable for the cycle, restoring directly",
            device_name,
        )
    else:
        # Execute in synchronized steps across all TRVs (much faster than
        # sequential). Open all → wait → close all → wait (repeat twice).
        for i in range(2):
            _LOGGER.debug(
                "better_thermostat %s: valve maintenance cycle %d/2 starting "
                "for %d TRV(s)",
                device_name,
                i + 1,
                len(cycled),
            )
            await asyncio.gather(
                *(
                    open_step(
                        info,
                        set_valve_fn=set_valve_fn,
                        set_temperature_fn=set_temperature_fn,
                    )
                    for info in cycled
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
                    for info in cycled
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
                get_state=get_state,
            )
            for info in infos
        ),
        return_exceptions=True,
    )

    _LOGGER.info("better_thermostat %s: valve maintenance finished", device_name)
