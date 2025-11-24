from homeassistant.helpers.importlib import async_import_module

import logging
import asyncio
import functools
import random
from ..utils.retry import async_retry
from custom_components.better_thermostat.utils.helpers import round_by_step

_LOGGER = logging.getLogger(__name__)


async def load_adapter(self, integration, entity_id, get_name=False):
    """Load adapter."""
    if get_name:
        self.device_name = "-"

    if integration == "generic_thermostat":
        integration = "generic"

    try:
        self.adapter = await async_import_module(
            self.hass, "custom_components.better_thermostat.adapters." + integration
        )
        _LOGGER.debug(
            "better_thermostat %s: uses adapter %s for trv %s",
            self.device_name,
            integration,
            entity_id,
        )
    except Exception:
        self.adapter = await async_import_module(
            self.hass, "custom_components.better_thermostat.adapters.generic"
        )
        _LOGGER.info(
            "better_thermostat %s: integration: %s isn't native supported, feel free to open an issue, fallback adapter %s",
            self.device_name,
            integration,
            "generic",
        )
        pass

    if get_name:
        return integration
    return self.adapter


@async_retry(retries=5)
async def init(self, entity_id):
    """Init adapter."""
    return await self.real_trvs[entity_id]["adapter"].init(self, entity_id)


@async_retry(retries=5)
async def get_info(self, entity_id):
    return await self.real_trvs[entity_id]["adapter"].get_info(self, entity_id)


@async_retry(retries=5)
async def get_current_offset(self, entity_id):
    """Get current offset."""
    return await self.real_trvs[entity_id]["adapter"].get_current_offset(
        self, entity_id
    )


@async_retry(retries=5)
async def get_offset_step(self, entity_id):
    """get offset setps."""
    return await self.real_trvs[entity_id]["adapter"].get_offset_step(self, entity_id)


@async_retry(retries=5)
async def get_min_offset(self, entity_id):
    """Get min offset."""
    return await self.real_trvs[entity_id]["adapter"].get_min_offset(self, entity_id)


@async_retry(retries=5)
async def get_max_offset(self, entity_id):
    """Get max offset."""
    return await self.real_trvs[entity_id]["adapter"].get_max_offset(self, entity_id)


@async_retry(retries=5)
async def set_temperature(self, entity_id, temperature):
    """Set new target temperature.

    Round to device step if known and clamp to min/max before delegating.
    Also updates last_temperature to the (potentially) rounded value for consistency.
    """
    # Normalize input to float early
    try:
        t = float(temperature)
    except (TypeError, ValueError):
        t = 0.0
    try:
        # Step precedence: per-TRV (usually from config) > global config > device attribute > default 0.5
        per_trv_step = self.real_trvs.get(entity_id, {}).get("target_temp_step")
        global_cfg_step = getattr(self, "bt_target_temp_step", None)
        if global_cfg_step in (0, 0.0):
            global_cfg_step = None
        device_step = (
            self.hass.states.get(entity_id).attributes.get("target_temp_step")
            if self.hass.states.get(entity_id)
            else None
        )
        step = per_trv_step or global_cfg_step or device_step or 0.5
        rounded = round_by_step(float(t), float(step))
    except Exception:  # noqa: BLE001
        rounded = float(t)

    # Clamp to device min/max if available
    t_min_raw = self.real_trvs.get(entity_id, {}).get("min_temp")
    t_max_raw = self.real_trvs.get(entity_id, {}).get("max_temp")
    t_min = None
    t_max = None
    try:
        if t_min_raw is not None:
            t_min = float(t_min_raw)
        if t_max_raw is not None:
            t_max = float(t_max_raw)
    except (TypeError, ValueError):
        t_min = None
        t_max = None
    if isinstance(t_min, (int, float)) and isinstance(t_max, (int, float)):
        low = float(t_min)
        high = float(t_max)
        rv = float(rounded) if isinstance(rounded, (int, float)) else float(t)
        if rv < low:
            rounded = low
        elif rv > high:
            rounded = high
        else:
            rounded = rv

    if rounded != t:
        _LOGGER.debug(
            "better_thermostat %s: delegate.set_temperature rounded %s -> %s (step=%s)",
            getattr(self, "device_name", "unknown"),
            t,
            rounded,
            step if "step" in locals() else None,
        )
    # Keep last_temperature in sync with the actually sent value
    try:
        self.real_trvs[entity_id]["last_temperature"] = rounded
    except Exception:  # noqa: BLE001
        pass

    return await self.real_trvs[entity_id]["adapter"].set_temperature(
        self, entity_id, rounded
    )


@async_retry(retries=5)
async def set_hvac_mode(self, entity_id, hvac_mode):
    """Set new target hvac mode."""
    return await self.real_trvs[entity_id]["adapter"].set_hvac_mode(
        self, entity_id, hvac_mode
    )


async def set_offset(self, entity_id, offset):
    """Set new target offset."""
    @async_retry(retries=5)
    async def inner():
        return await self.real_trvs[entity_id]["adapter"].set_offset(
            self, entity_id, offset
        )
    try:
        return await inner()
    except Exception:
        return None


@async_retry(retries=5)
async def set_valve(self, entity_id, valve):
    """Set new target valve.

    Prefers adapter/number entity if available; otherwise, falls back to model quirks
    override_set_valve. Records last_valve_percent and last_valve_method accordingly.
    Returns True on handled write, False otherwise.
    """
    try:
        target_pct = int(valve)
    except Exception:  # noqa: BLE001
        target_pct = valve
    try:
        valve_entity = (self.real_trvs.get(entity_id, {}) or {}).get(
            "valve_position_entity"
        )
        if valve_entity:
            await self.real_trvs[entity_id]["adapter"].set_valve(
                self, entity_id, target_pct
            )
            try:
                self.real_trvs[entity_id]["last_valve_percent"] = int(target_pct)
                self.real_trvs[entity_id]["last_valve_method"] = "adapter"
            except Exception:  # noqa: BLE001
                pass
            return True
        # Fallback: quirks override
        try:
            from custom_components.better_thermostat.model_fixes.model_quirks import (
                override_set_valve as _override_set_valve,
            )
        except Exception:  # noqa: BLE001
            _override_set_valve = None
        if _override_set_valve is not None:
            ok = await _override_set_valve(self, entity_id, target_pct)
            if ok:
                try:
                    self.real_trvs[entity_id]["last_valve_percent"] = int(target_pct)
                    self.real_trvs[entity_id]["last_valve_method"] = "override"
                except Exception:  # noqa: BLE001
                    pass
            return bool(ok)
    except Exception:  # noqa: BLE001
        _LOGGER.debug(
            "better_thermostat %s: delegate.set_valve failed for %s",
            getattr(self, "device_name", "unknown"),
            entity_id,
        )
    return False
