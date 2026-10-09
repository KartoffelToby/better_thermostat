"""Default model quirks passthrough for unknown devices.

These helpers implement safe no-op defaults for devices that do not
require specific quirks.
"""

from __future__ import annotations

import logging

from homeassistant.components.lock import LockState
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.helpers import entity_registry as er

from custom_components.better_thermostat.model_fixes.types import (
    InitialTweakQuirk,
    MaintenanceIntervalQuirk,
    ModelFixHost,
    ModelQuirks,
    UnknownStateQuirk,
)

from ..utils.advanced_flags import as_bool
from ..utils.const import CONF_CHILD_LOCK
from ..utils.helpers import find_device_entity

_LOGGER = logging.getLogger(__name__)

VALVE_MAINTENANCE_INTERVAL_HOURS = 168  # Default: 7 days


def trv_state_unknown_as_available(self: ModelFixHost, entity_id: str) -> bool:
    """Answer whether the TRV is operating while its state reads ``unknown``.

    Parameters
    ----------
    self : ModelFixHost
        Host providing Home Assistant access and the per-TRV records.
        Unused by the default policy.
    entity_id : str
        Entity ID of the TRV being judged. Unused by the default policy.

    Returns
    -------
    bool
        False: an entity that says nothing about its device leaves the
        device unaccounted for.
    """
    return False


def fix_local_calibration(
    self: ModelFixHost, entity_id: str, calibration_offset: float
) -> float:
    """Return the given local calibration offset unchanged."""
    return calibration_offset


def fix_target_temperature_calibration(
    self: ModelFixHost, entity_id: str, temperature: float
) -> float:
    """Return the given target temperature unchanged."""
    return temperature


async def override_set_hvac_mode(
    self: ModelFixHost, entity_id: str, hvac_mode: str
) -> bool:
    """Do not override HVAC mode by default."""
    return False


async def override_set_temperature(
    self: ModelFixHost, entity_id: str, temperature: float
) -> bool:
    """Do not override set temperature by default."""
    return False


async def initial_tweak(self: ModelFixHost, entity_id: str) -> None:
    """Run initial tweaks for the device."""
    entity_registry = er.async_get(self.hass)
    reg_entity = entity_registry.async_get(entity_id)

    if reg_entity is not None and reg_entity.device_id is not None:
        device_id = reg_entity.device_id

        def find_entity(domains: list[str], keywords: list[str]) -> str | None:
            return find_device_entity(entity_registry, device_id, domains, keywords)

        # 1. Local calibration -> 0
        cal_entity = find_entity(
            ["number"],
            ["local_temperature_calibration", "local_calibration", "calibration"],
        )
        if cal_entity:
            try:
                _LOGGER.debug(
                    "better_thermostat %s: Resetting local calibration for %s to 0",
                    self.device_name,
                    cal_entity,
                )
                await self.hass.services.async_call(
                    "number",
                    "set_value",
                    {"entity_id": cal_entity, "value": 0},
                    blocking=True,
                    context=self.context,
                )
            except Exception as e:  # noqa: BLE001 - a device failure arrives as any exception type
                _LOGGER.warning(
                    "better_thermostat %s: Failed to reset calibration for %s: %s",
                    self.device_name,
                    cal_entity,
                    e,
                )

        # 2. Child lock sync setting
        stored_child_lock = self.real_trvs[entity_id].advanced.get(CONF_CHILD_LOCK)
        if stored_child_lock is not None:
            child_lock_setting = as_bool(stored_child_lock)
            # Look for switch (Z2M) or lock
            cl_entity = find_entity(
                ["switch", "lock"], ["child_lock", "child lock", "lock"]
            )
            if cl_entity:
                target_state = STATE_ON if child_lock_setting else STATE_OFF
                domain = cl_entity.split(".")[0]

                try:
                    if domain == "switch":
                        cur = self.hass.states.get(cl_entity)
                        if cur and cur.state != target_state:
                            _LOGGER.debug(
                                "better_thermostat %s: Setting child lock (switch) for %s to %s",
                                self.device_name,
                                cl_entity,
                                target_state,
                            )
                            service = "turn_on" if child_lock_setting else "turn_off"
                            await self.hass.services.async_call(
                                "switch",
                                service,
                                {"entity_id": cl_entity},
                                blocking=True,
                                context=self.context,
                            )
                    elif domain == "lock":
                        target_lock = (
                            LockState.LOCKED
                            if child_lock_setting
                            else LockState.UNLOCKED
                        )
                        cur = self.hass.states.get(cl_entity)
                        if cur and cur.state != target_lock.value:
                            _LOGGER.debug(
                                "better_thermostat %s: Setting child lock (lock) for %s to %s",
                                self.device_name,
                                cl_entity,
                                target_lock,
                            )
                            service = "lock" if child_lock_setting else "unlock"
                            await self.hass.services.async_call(
                                "lock",
                                service,
                                {"entity_id": cl_entity},
                                blocking=True,
                                context=self.context,
                            )
                except Exception as e:  # noqa: BLE001 - a device failure arrives as any exception type
                    _LOGGER.warning(
                        "better_thermostat %s: Failed to set child lock for %s: %s",
                        self.device_name,
                        cl_entity,
                        e,
                    )

        # 3. Away / Window detection -> Off
        # Window detection disable the interal trv window detection, its handled by better_thermostat
        win_entity = find_entity(
            ["switch"],
            ["window_detection", "window_open", "window open", "open_window"],
        )
        if win_entity:
            try:
                cur = self.hass.states.get(win_entity)
                if cur and cur.state != STATE_OFF:
                    _LOGGER.debug(
                        "better_thermostat %s: Disabling window detection for %s",
                        self.device_name,
                        win_entity,
                    )
                    await self.hass.services.async_call(
                        "switch",
                        "turn_off",
                        {"entity_id": win_entity},
                        blocking=True,
                        context=self.context,
                    )
            except Exception as e:  # noqa: BLE001 - a device failure arrives as any exception type
                _LOGGER.warning(
                    "better_thermostat %s: Failed to disable window detection for %s: %s",
                    self.device_name,
                    win_entity,
                    e,
                )

        # Away mode -> Off
        # Disable the away mode on the device if available
        away_entity = find_entity(
            ["switch"], ["away_mode", "away mode", "holiday_mode", "holiday"]
        )
        if away_entity:
            try:
                cur = self.hass.states.get(away_entity)
                if cur and cur.state != STATE_OFF:
                    _LOGGER.debug(
                        "better_thermostat %s: Disabling away mode for %s",
                        self.device_name,
                        away_entity,
                    )
                    await self.hass.services.async_call(
                        "switch",
                        "turn_off",
                        {"entity_id": away_entity},
                        blocking=True,
                        context=self.context,
                    )
            except Exception as e:  # noqa: BLE001 - a device failure arrives as any exception type
                _LOGGER.warning(
                    "better_thermostat %s: Failed to disable away mode for %s: %s",
                    self.device_name,
                    away_entity,
                    e,
                )


class _Surface:
    """Quirk surface of the module, bound below to each Protocol it implements."""

    fix_local_calibration = staticmethod(fix_local_calibration)
    fix_target_temperature_calibration = staticmethod(
        fix_target_temperature_calibration
    )
    override_set_hvac_mode = staticmethod(override_set_hvac_mode)
    override_set_temperature = staticmethod(override_set_temperature)
    VALVE_MAINTENANCE_INTERVAL_HOURS = VALVE_MAINTENANCE_INTERVAL_HOURS
    initial_tweak = staticmethod(initial_tweak)
    trv_state_unknown_as_available = staticmethod(trv_state_unknown_as_available)


_MODEL_QUIRKS: ModelQuirks = _Surface()
_INITIAL_TWEAK_QUIRK: InitialTweakQuirk = _Surface()
_MAINTENANCE_INTERVAL_QUIRK: MaintenanceIntervalQuirk = _Surface()
_UNKNOWN_STATE_QUIRK: UnknownStateQuirk = _Surface()
