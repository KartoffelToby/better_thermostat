"""One-time migration from four separate Store files to unified StateManager.

Before the unified ``StateManager`` was introduced, Better Thermostat
persisted calibration state across four independent HA Store files:

- ``better_thermostat_mpc_states``
- ``better_thermostat_pid_states``
- ``better_thermostat_tpi_states``
- ``better_thermostat_thermal_stats``

This module reads those files (if present), filters entries belonging to
the current config entry, and imports them into the unified store.  It
runs once on startup when the unified store is still empty.

Once enough time has passed for all installations to have migrated, this
entire module can be deleted and its single call site in ``climate.py``
removed.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .state_manager import (
    StateManager,
    ThermalStats,
    deserialize_mpc,
    deserialize_pid,
    deserialize_tpi,
)

_LOGGER = logging.getLogger(__name__)

DOMAIN = "better_thermostat"


def _import_legacy_data(
    state_mgr: StateManager,
    *,
    mpc_data: dict[str, dict[str, Any]] | None = None,
    pid_data: dict[str, dict[str, Any]] | None = None,
    tpi_data: dict[str, dict[str, Any]] | None = None,
    thermal_data: dict[str, Any] | None = None,
) -> None:
    """Deserialize raw dicts from legacy stores into the unified state.

    Parameters
    ----------
    state_mgr:
        The StateManager to populate.
    mpc_data / pid_data / tpi_data:
        Key → raw-dict mappings loaded from the respective legacy stores,
        already filtered to the current entity prefix.
    thermal_data:
        Raw dict for the thermal stats entry of this config entry.
    """
    if mpc_data:
        for key, state_dict in mpc_data.items():
            if isinstance(state_dict, dict):
                state_mgr.set_mpc(key, deserialize_mpc(state_dict))

    if pid_data:
        for key, state_dict in pid_data.items():
            if isinstance(state_dict, dict):
                state_mgr.set_pid(key, deserialize_pid(state_dict))

    if tpi_data:
        for key, state_dict in tpi_data.items():
            if isinstance(state_dict, dict):
                state_mgr.set_tpi(key, deserialize_tpi(state_dict))

    if thermal_data and isinstance(thermal_data, dict):
        heating_power = thermal_data.get("heating_power")
        heat_loss_rate = thermal_data.get("heat_loss_rate")
        state_mgr.thermal = ThermalStats(
            heating_power=(float(heating_power) if heating_power is not None else None),
            heat_loss_rate=(
                float(heat_loss_rate) if heat_loss_rate is not None else None
            ),
        )


def _filter_by_prefix(raw: dict[str, Any], prefix: str) -> dict[str, dict[str, Any]]:
    """Return only entries whose key starts with *prefix* and whose value is a dict."""
    return {
        k: v
        for k, v in raw.items()
        if isinstance(k, str) and k.startswith(prefix) and isinstance(v, dict)
    }


async def migrate_v0_stores(
    hass: HomeAssistant,
    state_mgr: StateManager,
    entity_prefix: str,
    config_entry_id: str,
) -> None:
    """Migrate data from the four legacy Store files into the unified store.

    Skips silently when the unified store already contains data (i.e. the
    migration has already run or the user started fresh).  After a
    successful import the unified store is saved immediately.  The legacy
    files are **not** deleted so that a rollback to the previous version
    remains possible.

    Parameters
    ----------
    hass:
        The Home Assistant instance.
    state_mgr:
        The (already loaded) unified StateManager.
    entity_prefix:
        The entity-scoped key prefix, typically ``f"{unique_id}:"``.
    config_entry_id:
        The config entry ID (used to look up thermal stats).
    """
    # If the unified store already has data, skip migration.
    current_state = state_mgr.state
    if (
        current_state.mpc
        or current_state.pid
        or current_state.tpi
        or current_state.presets
    ):
        return
    if (
        current_state.thermal.heating_power is not None
        or current_state.thermal.heat_loss_rate is not None
    ):
        return

    any_imported = False

    # MPC legacy
    try:
        mpc_store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}_mpc_states")
        mpc_raw = await mpc_store.async_load()
        if isinstance(mpc_raw, dict):
            entity_entries = _filter_by_prefix(mpc_raw, entity_prefix)
            if entity_entries:
                _import_legacy_data(state_mgr, mpc_data=entity_entries)
                any_imported = True
    except Exception:
        pass

    # PID legacy
    try:
        pid_store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}_pid_states")
        pid_raw = await pid_store.async_load()
        if isinstance(pid_raw, dict):
            entity_entries = _filter_by_prefix(pid_raw, entity_prefix)
            if entity_entries:
                _import_legacy_data(state_mgr, pid_data=entity_entries)
                any_imported = True
    except Exception:
        pass

    # TPI legacy
    try:
        tpi_store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}_tpi_states")
        tpi_raw = await tpi_store.async_load()
        if isinstance(tpi_raw, dict):
            entity_entries = _filter_by_prefix(tpi_raw, entity_prefix)
            if entity_entries:
                _import_legacy_data(state_mgr, tpi_data=entity_entries)
                any_imported = True
    except Exception:
        pass

    # Thermal legacy
    try:
        thermal_store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}_thermal_stats")
        thermal_raw = await thermal_store.async_load()
        if isinstance(thermal_raw, dict):
            thermal_entry = thermal_raw.get(str(config_entry_id))
            if isinstance(thermal_entry, dict):
                _import_legacy_data(state_mgr, thermal_data=thermal_entry)
                any_imported = True
    except Exception:
        pass

    if any_imported:
        await state_mgr.save()
        _LOGGER.info(
            "better_thermostat [%s]: migrated v0 stores to unified state",
            config_entry_id,
        )
