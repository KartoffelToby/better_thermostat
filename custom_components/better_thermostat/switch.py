"""Better Thermostat Switch Platform."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import TYPE_CHECKING, Any, override

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_OFF, STATE_ON, EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er, restore_state
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
import voluptuous as vol

from . import BetterThermostatConfigEntry
from .entity import (
    ControlsOneTrv,
    FollowsThermostat,
    RestoresLastAvailableState,
    TrvNamedEntity,
    current_trv_name,
    last_available_state,
    remove_unclaimed_registry_entries,
)

# Import tracking variables from sensor.py
from .sensor import _ACTIVE_SWITCH_ENTITIES
from .utils.calibration.pid import (
    DEFAULT_PID_AUTO_TUNE,
    build_pid_key,
    resolve_unique_id,
)
from .utils.const import CONF_CALIBRATION_MODE, CONF_CHILD_LOCK, DOMAIN, CalibrationMode
from .utils.helpers import async_normalize_bt_entity_ids, find_device_entity

if TYPE_CHECKING:
    from .climate import BetterThermostat

_LOGGER = logging.getLogger(__name__)

# Every entity is pushed and none polls; actions are not limited per platform.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BetterThermostatConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Better Thermostat switches."""
    bt_climate = entry.runtime_data.climate
    if not bt_climate:
        return

    switches: list[SwitchEntity] = []
    switch_unique_ids = {}
    has_multiple_trvs = len(bt_climate.real_trvs) > 1
    for trv_entity_id, trv in bt_climate.real_trvs.items():
        advanced = trv.advanced or {}
        calibration_mode = advanced.get(CONF_CALIBRATION_MODE)

        # Normalize string values to CalibrationMode enum
        try:
            if isinstance(calibration_mode, str):
                calibration_mode = CalibrationMode(calibration_mode)
        except ValueError, TypeError:
            # Invalid or unknown calibration mode, skip PID creation
            calibration_mode = None

        if calibration_mode == CalibrationMode.PID_CALIBRATION:
            pid_switch = BetterThermostatPIDAutoTuneSwitch(
                bt_climate, trv_entity_id, has_multiple_trvs
            )
            switches.append(pid_switch)
            switch_unique_ids[pid_switch._attr_unique_id] = {
                "trv": trv_entity_id,
                "type": "pid_auto_tune",
            }

        child_lock_switch = BetterThermostatChildLockSwitch(
            bt_climate, trv_entity_id, has_multiple_trvs
        )
        switches.append(child_lock_switch)
        switch_unique_ids[child_lock_switch._attr_unique_id] = {
            "trv": trv_entity_id,
            "type": "child_lock",
        }

    # Track created switch entities for cleanup
    _ACTIVE_SWITCH_ENTITIES[entry.entry_id] = switch_unique_ids

    _LOGGER.debug(
        "Better Thermostat %s: Created %d switch entities",
        bt_climate.device_name,
        len(switch_unique_ids),
    )

    remove_unclaimed_registry_entries(
        er.async_get(hass),
        entry,
        Platform.SWITCH,
        (switch.unique_id for switch in switches),
        bt_climate,
    )
    async_normalize_bt_entity_ids(hass, entry, Platform.SWITCH)
    async_add_entities(switches)


class BetterThermostatPIDAutoTuneSwitch(
    FollowsThermostat, TrvNamedEntity, SwitchEntity, RestoreEntity
):
    """Switch for PID Auto Tune."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        bt_climate: BetterThermostat,
        trv_entity_id: str,
        show_trv_name: bool = True,
    ) -> None:
        """Initialize the switch."""
        self._bt_climate = bt_climate
        self._trv_entity_id = trv_entity_id
        self._attr_unique_id = f"{bt_climate.unique_id}_{trv_entity_id}_pid_auto_tune"

        if show_trv_name:
            self._attr_translation_key = "pid_auto_tune"
            self._attr_translation_placeholders = {
                "trv_name": current_trv_name(bt_climate.hass, trv_entity_id)
            }
        else:
            self._attr_translation_key = "pid_auto_tune_no_trv"

    @override
    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added."""
        await super().async_added_to_hass()
        self._follow_trv_name()
        self._follow_thermostat()

    @property
    @override
    def device_info(self) -> DeviceInfo:
        """Return the device info."""
        return self._bt_climate.device_info

    @property
    @override
    def is_on(self) -> bool | None:
        """Return true if switch is on."""
        # Try to get the value from the current active PID state
        state_mgr = self._bt_climate.state_mgr
        if state_mgr is not None:
            key = build_pid_key(self._bt_climate, self._trv_entity_id)
            pid_state = state_mgr.state.pid.get(key)
            if pid_state is not None and pid_state.auto_tune is not None:
                return pid_state.auto_tune

        return DEFAULT_PID_AUTO_TUNE

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        self._update_state(True)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        self._update_state(False)

    def _update_state(self, state: bool):
        """Update the state."""
        state_mgr = self._bt_climate.state_mgr
        if state_mgr is None:
            _LOGGER.debug(
                "Cannot set PID auto-tune for %s: state manager not ready",
                self._trv_entity_id,
            )
            return

        # Update persistent PID states (if any exist for this TRV)
        uid = resolve_unique_id(self._bt_climate)
        prefix = f"{uid}:{self._trv_entity_id}:"

        changed = False
        for key, pid_state in state_mgr.state.pid.items():
            if key.startswith(prefix):
                pid_state.auto_tune = state
                changed = True
        if changed:
            state_mgr.mark_dirty()
        else:
            # No bucket for this TRV yet (fresh start or after a PID
            # reset): seed the active bucket so the toggle is not lost.
            key = build_pid_key(self._bt_climate, self._trv_entity_id)
            pid_state = state_mgr.get_pid(key)
            pid_state.auto_tune = state
            state_mgr.set_pid(key, pid_state)

        self._bt_climate.schedule_save_state()
        self.async_write_ha_state()


def _switch_state_wins(
    state: str | None, extra: Mapping[str, object] | None, configured: bool
) -> bool | None:
    """Return the child lock a restored switch state stands for, if it wins.

    The switch and the configured option both set the lock, and the one set
    last wins. The switch records the option next to its state; a recorded
    option that differs from the current one was changed after the switch,
    and then the option holds (``None``).
    """
    if state not in (STATE_ON, STATE_OFF):
        return None
    recorded = (extra or {}).get("configured")
    if recorded is not None and recorded != configured:
        return None
    return state == STATE_ON


def restored_child_lock(
    hass: HomeAssistant, bt_unique_id: str, trv_entity_id: str, configured: bool
) -> bool | None:
    """Return the child lock the TRV's switch restores to, or ``None``.

    Read before the switch itself is set up, so the thermostat's startup sends
    the device the lock the switch will show rather than the option.
    """
    entity_id = er.async_get(hass).async_get_entity_id(
        "switch", DOMAIN, f"{bt_unique_id}_{trv_entity_id}_child_lock"
    )
    if entity_id is None:
        return None
    stored = restore_state.async_get(hass).last_states.get(entity_id)
    if stored is None:
        return None
    extra = stored.extra_data.as_dict() if stored.extra_data else None
    saved = last_available_state(stored.state, extra)
    return _switch_state_wins(saved.state if saved else None, extra, configured)


class BetterThermostatChildLockSwitch(
    ControlsOneTrv,
    FollowsThermostat,
    TrvNamedEntity,
    SwitchEntity,
    RestoresLastAvailableState,
):
    """Switch for Child Lock."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        bt_climate: BetterThermostat,
        trv_entity_id: str,
        show_trv_name: bool = True,
    ) -> None:
        """Initialize the switch."""
        self._bt_climate = bt_climate
        self._trv_entity_id = trv_entity_id
        self._attr_unique_id = f"{bt_climate.unique_id}_{trv_entity_id}_child_lock"
        if show_trv_name:
            self._attr_translation_key = "child_lock"
            self._attr_translation_placeholders = {
                "trv_name": current_trv_name(bt_climate.hass, trv_entity_id)
            }
        else:
            self._attr_translation_key = "child_lock_no_trv"

    @override
    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added."""
        await super().async_added_to_hass()
        self._follow_trv_name()
        self._follow_trv_availability()
        self._follow_thermostat()
        await self._restore_child_lock()

    def _configured_child_lock(self) -> bool:
        """Return the child-lock option the config entry holds for the TRV."""
        for trv_config in self._bt_climate.all_trvs or []:
            if trv_config.get("trv") == self._trv_entity_id:
                return bool((trv_config.get("advanced") or {}).get(CONF_CHILD_LOCK))
        return False

    @override
    def _extra_restore_data(self) -> dict[str, Any]:
        """Record the configured option next to the switch state.

        A restore compares it with the option then configured; a difference
        means the option was set in the options flow after the switch.
        """
        return {
            **super()._extra_restore_data(),
            "configured": self._configured_child_lock(),
        }

    async def _restore_child_lock(self) -> None:
        """Put the switch state from before a reload or restart back on the TRV.

        The switch and the options flow both set the lock, and the one set
        last wins: the restored state, unless the option has changed since.
        The thermostat takes the restored state over before its startup
        sends the lock to the device; should the TRV still hold another one,
        the restored state is sent after it.
        """
        trv = self._bt_climate.real_trvs.get(self._trv_entity_id)
        last_state = await self.async_get_last_available_state()
        if trv is None or last_state is None:
            return
        last_extra = await self.async_get_last_extra_data()
        restored = _switch_state_wins(
            last_state.state,
            last_extra.as_dict() if last_extra else None,
            self._configured_child_lock(),
        )
        if restored is None:
            return
        held = bool(trv.advanced.get(CONF_CHILD_LOCK))
        trv.advanced[CONF_CHILD_LOCK] = restored
        if restored != held:
            # The device may still report the state from before a command
            # the startup sent, so the restored one is sent regardless.
            await self._set_child_lock(restored, force=True)

    @property
    @override
    def device_info(self) -> DeviceInfo:
        """Return the device info."""
        return self._bt_climate.device_info

    @property
    @override
    def is_on(self) -> bool | None:
        """Return true if switch is on."""
        trv = self._bt_climate.real_trvs.get(self._trv_entity_id)
        if trv is None:
            return False
        return (trv.advanced or {}).get("child_lock", False)

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        self._update_state(True)
        await self._set_child_lock(True)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        self._update_state(False)
        await self._set_child_lock(False)

    def _update_state(self, state: bool):
        """Update the state."""
        trv = self._bt_climate.real_trvs.get(self._trv_entity_id)
        if trv is None:
            return
        trv.advanced["child_lock"] = state
        self.async_write_ha_state()

    async def _set_child_lock(self, state: bool, *, force: bool = False):
        """Set the child lock on the real device.

        With ``force`` the command is sent even when the device already
        reports the target state.
        """
        entity_registry = er.async_get(self._bt_climate.hass)
        reg_entity = entity_registry.async_get(self._trv_entity_id)

        if reg_entity is None or reg_entity.device_id is None:
            return

        device_id = reg_entity.device_id

        # Look for switch (Z2M) or lock. Prefer child-lock-specific names and
        # only fall back to a bare "lock" match, so a device exposing several
        # lock entities does not select the wrong one.
        cl_entity = find_device_entity(
            entity_registry, device_id, ["switch", "lock"], ["child_lock", "child lock"]
        ) or find_device_entity(
            entity_registry, device_id, ["switch", "lock"], ["lock"]
        )

        if cl_entity:
            target_state = STATE_ON if state else STATE_OFF
            domain = cl_entity.split(".")[0]

            try:
                if domain == "switch":
                    cur = self._bt_climate.hass.states.get(cl_entity)
                    if cur and (force or cur.state != target_state):
                        _LOGGER.debug(
                            "Better Thermostat Child Lock: Setting child lock (switch) for %s to %s",
                            cl_entity,
                            target_state,
                        )
                        service = "turn_on" if state else "turn_off"
                        await self._bt_climate.hass.services.async_call(
                            "switch", service, {"entity_id": cl_entity}
                        )
                elif domain == "lock":
                    target_lock = "locked" if state else "unlocked"
                    cur = self._bt_climate.hass.states.get(cl_entity)
                    if cur and (force or cur.state != target_lock):
                        _LOGGER.debug(
                            "Better Thermostat Child Lock: Setting child lock (lock) for %s to %s",
                            cl_entity,
                            target_lock,
                        )
                        service = "lock" if state else "unlock"
                        await self._bt_climate.hass.services.async_call(
                            "lock", service, {"entity_id": cl_entity}
                        )
            # Without ``blocking`` only a call Home Assistant refuses outright
            # raises here; a device failure is logged by the task it runs in.
            except (HomeAssistantError, vol.Invalid) as e:
                _LOGGER.warning(
                    "Better Thermostat Child Lock: Failed to set child lock for %s: %s",
                    cl_entity,
                    e,
                )
