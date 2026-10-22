"""Better Thermostat Switch Platform."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import TYPE_CHECKING, Any, override

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_OFF, STATE_ON, EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
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
    announce_learned_state,
    current_trv_name,
    last_available_state,
    remove_unclaimed_registry_entries,
)
from .events.contact import set_window_detection
from .tracked_entities import SwitchKind, SwitchRef
from .utils.advanced_flags import advanced_flag
from .utils.calibration.pid import (
    DEFAULT_PID_AUTO_TUNE,
    build_pid_key,
    build_pid_loop_key,
    freeze_pid_gains,
    pid_auto_tune,
    pid_loop_state,
    resolve_unique_id,
)
from .utils.const import CONF_CHILD_LOCK, DOMAIN, CalibrationMode
from .utils.helpers import (
    async_normalize_bt_entity_ids,
    configured_calibration_mode,
    device_released,
    find_child_lock_entity,
)

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

    switches: list[SwitchEntity] = [
        BetterThermostatReleaseDevicesSwitch(bt_climate),
        BetterThermostatPauseLearningSwitch(bt_climate),
    ]
    switch_unique_ids: dict[str, SwitchRef] = {}
    has_multiple_trvs = len(bt_climate.real_trvs) > 1
    for trv_entity_id, trv in bt_climate.real_trvs.items():
        if configured_calibration_mode(trv.advanced) == CalibrationMode.PID_CALIBRATION:
            switches.append(
                BetterThermostatPIDAutoTuneSwitch(
                    bt_climate, trv_entity_id, has_multiple_trvs
                )
            )
            switch_unique_ids[
                BetterThermostatPIDAutoTuneSwitch.build_unique_id(
                    bt_climate.unique_id, trv_entity_id
                )
            ] = SwitchRef(trv_entity_id, SwitchKind.PID_AUTO_TUNE)

        switches.append(
            BetterThermostatChildLockSwitch(
                bt_climate, trv_entity_id, has_multiple_trvs
            )
        )
        switch_unique_ids[
            BetterThermostatChildLockSwitch.build_unique_id(
                bt_climate.unique_id, trv_entity_id
            )
        ] = SwitchRef(trv_entity_id, SwitchKind.CHILD_LOCK)

        switches.append(
            BetterThermostatReleaseTrvSwitch(
                bt_climate, trv_entity_id, has_multiple_trvs
            )
        )
        switch_unique_ids[
            BetterThermostatReleaseTrvSwitch.build_unique_id(
                bt_climate.unique_id, trv_entity_id
            )
        ] = SwitchRef(trv_entity_id, SwitchKind.RELEASE_TRV)

    if bt_climate.has_contact_sensor:
        switches.append(BetterThermostatWindowDetectionSwitch(bt_climate))

    # Track created switch entities for cleanup
    entry.runtime_data.tracked_entities.switches = switch_unique_ids

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


class BetterThermostatReleaseDevicesSwitch(FollowsThermostat, SwitchEntity):
    """Switch that hands the room's devices to the user while it is on.

    The thermostat holds the setting and publishes it as the
    ``devices_released`` attribute, which it restores after a restart before
    its startup writes to any device; the switch shows and sets it.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "release_devices"

    def __init__(self, bt_climate: BetterThermostat) -> None:
        """Initialize the switch."""
        self._bt_climate = bt_climate
        self._attr_unique_id = f"{bt_climate.unique_id}_release_devices"

    @override
    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added."""
        await super().async_added_to_hass()
        self._follow_thermostat()

    @property
    @override
    def device_info(self) -> DeviceInfo:
        """Return the device info."""
        return self._bt_climate.device_info

    @property
    @override
    def is_on(self) -> bool:
        """Return whether the devices are released."""
        return self._bt_climate.devices_released

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Release the devices."""
        await self._bt_climate.async_set_devices_released(True)
        self.async_write_ha_state()

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Take the devices back."""
        await self._bt_climate.async_set_devices_released(False)
        self.async_write_ha_state()


class BetterThermostatReleaseTrvSwitch(FollowsThermostat, TrvNamedEntity, SwitchEntity):
    """Switch that hands one TRV to the user while it is on.

    The thermostat holds the setting and publishes it in the
    ``released_trvs`` attribute, which it restores after a restart before
    its startup writes to any device; the switch shows and sets it.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    @staticmethod
    def build_unique_id(bt_unique_id: str, trv_entity_id: str) -> str:
        """Return the unique id of the switch that releases ``trv_entity_id``."""
        return f"{bt_unique_id}_{trv_entity_id}_release"

    def __init__(
        self,
        bt_climate: BetterThermostat,
        trv_entity_id: str,
        show_trv_name: bool = True,
    ) -> None:
        """Initialize the switch."""
        self._bt_climate = bt_climate
        self._trv_entity_id = trv_entity_id
        self._attr_unique_id = BetterThermostatReleaseTrvSwitch.build_unique_id(
            bt_climate.unique_id, trv_entity_id
        )
        if show_trv_name:
            self._attr_translation_key = "release_trv"
            self._attr_translation_placeholders = {
                "trv_name": current_trv_name(bt_climate.hass, trv_entity_id)
            }
        else:
            self._attr_translation_key = "release_trv_no_trv"

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
    def is_on(self) -> bool:
        """Return whether the TRV is released on its own."""
        return self._trv_entity_id in self._bt_climate.released_trvs

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Release the TRV."""
        await self._bt_climate.async_set_trv_released(self._trv_entity_id, True)
        self.async_write_ha_state()

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Take the TRV back."""
        await self._bt_climate.async_set_trv_released(self._trv_entity_id, False)
        self.async_write_ha_state()


class BetterThermostatPauseLearningSwitch(FollowsThermostat, SwitchEntity):
    """Switch that pauses every learner of the thermostat.

    While it is on, nothing the thermostat learns from the room changes and
    regulation runs on what was learned before. The pause lives on the
    thermostat, which publishes it as ``learning_paused`` and restores it
    from its own saved state before its first control cycle, so the switch
    holds no state of its own and needs no restore.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "pause_learning"

    def __init__(self, bt_climate: BetterThermostat) -> None:
        """Initialize the switch."""
        self._bt_climate = bt_climate
        self._attr_unique_id = f"{bt_climate.unique_id}_pause_learning"

    @override
    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added."""
        await super().async_added_to_hass()
        self._follow_thermostat()

    @property
    @override
    def device_info(self) -> DeviceInfo:
        """Return the device info."""
        return self._bt_climate.device_info

    @property
    @override
    def is_on(self) -> bool:
        """Return whether learning is paused."""
        return self._bt_climate.learning_paused

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Pause learning."""
        self._bt_climate.set_learning_paused(True)
        self.async_write_ha_state()

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Resume learning."""
        self._bt_climate.set_learning_paused(False)
        self.async_write_ha_state()


class BetterThermostatPIDAutoTuneSwitch(
    FollowsThermostat, TrvNamedEntity, SwitchEntity, RestoreEntity
):
    """Switch for PID Auto Tune."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    @staticmethod
    def build_unique_id(bt_unique_id: str, trv_entity_id: str) -> str:
        """Return the unique id of the PID auto-tune switch of ``trv_entity_id``."""
        return f"{bt_unique_id}_{trv_entity_id}_pid_auto_tune"

    def __init__(
        self,
        bt_climate: BetterThermostat,
        trv_entity_id: str,
        show_trv_name: bool = True,
    ) -> None:
        """Initialize the switch."""
        self._bt_climate = bt_climate
        self._trv_entity_id = trv_entity_id
        self._attr_unique_id = BetterThermostatPIDAutoTuneSwitch.build_unique_id(
            bt_climate.unique_id, trv_entity_id
        )

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
        state_mgr = self._bt_climate.state_mgr
        if not self._bt_climate.state_loaded:
            return DEFAULT_PID_AUTO_TUNE
        states = state_mgr.state.pid
        bucket_key = build_pid_key(self._bt_climate, self._trv_entity_id)
        return pid_auto_tune(
            pid_loop_state(
                states,
                build_pid_loop_key(self._bt_climate, self._trv_entity_id),
                bucket_key,
            ),
            states.get(bucket_key),
        )

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        self._update_state(True)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        self._update_state(False)

    def _update_state(self, state: bool) -> None:
        """Update the state."""
        state_mgr = self._bt_climate.state_mgr
        if not self._bt_climate.state_loaded:
            _LOGGER.debug(
                "Cannot set PID auto-tune for %s: state manager not ready",
                self._trv_entity_id,
            )
            return

        # The flag sits on the TRV's loop entry, so it holds at every target.
        # Turning auto-tuning off keeps the gains in use at the current
        # target for all targets.
        loop_key = build_pid_loop_key(self._bt_climate, self._trv_entity_id)
        bucket_key = build_pid_key(self._bt_climate, self._trv_entity_id)
        loop = pid_loop_state(state_mgr.state.pid, loop_key, bucket_key)
        if state:
            loop.auto_tune = True
        else:
            freeze_pid_gains(loop, state_mgr.state.pid.get(bucket_key))
        state_mgr.set_pid(loop_key, loop)

        self._bt_climate.schedule_save_state()
        self.async_write_ha_state()
        # The gain numbers show the gains this may have frozen.
        announce_learned_state(self.hass, resolve_unique_id(self._bt_climate))


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
        "switch",
        DOMAIN,
        BetterThermostatChildLockSwitch.build_unique_id(bt_unique_id, trv_entity_id),
    )
    if entity_id is None:
        return None
    stored = restore_state.async_get(hass).last_states.get(entity_id)
    if stored is None:
        return None
    extra = stored.extra_data.as_dict() if stored.extra_data else None
    saved = last_available_state(stored.state, extra)
    return _switch_state_wins(saved.state if saved else None, extra, configured)


def restored_window_detection(hass: HomeAssistant, bt_unique_id: str) -> bool | None:
    """Return the window detection the thermostat's switch restores to.

    ``None`` when the switch has no saved state. Read before the switch
    itself is set up, so the thermostat's first control cycle already runs
    with the restored setting.
    """
    entity_id = er.async_get(hass).async_get_entity_id(
        "switch", DOMAIN, f"{bt_unique_id}_window_detection"
    )
    if entity_id is None:
        return None
    stored = restore_state.async_get(hass).last_states.get(entity_id)
    if stored is None:
        return None
    extra = stored.extra_data.as_dict() if stored.extra_data else None
    saved = last_available_state(stored.state, extra)
    if saved is None or saved.state not in (STATE_ON, STATE_OFF):
        return None
    return saved.state == STATE_ON


class BetterThermostatWindowDetectionSwitch(
    FollowsThermostat, SwitchEntity, RestoresLastAvailableState
):
    """Switch that lets the window and door sensors turn the heating off.

    The setting lives in the thermostat's kernel state. The thermostat
    restores it from this switch's saved state before its startup; the
    switch restores it once more when it is added, for a saved state the
    thermostat could not find by the switch's entity id.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "window_detection"

    def __init__(self, bt_climate: BetterThermostat) -> None:
        """Initialize the switch."""
        self._bt_climate = bt_climate
        self._attr_unique_id = f"{bt_climate.unique_id}_window_detection"

    @override
    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added."""
        await super().async_added_to_hass()
        self._follow_thermostat()
        last_state = await self.async_get_last_available_state()
        if last_state is not None and last_state.state in (STATE_ON, STATE_OFF):
            await set_window_detection(self._bt_climate, last_state.state == STATE_ON)

    @property
    @override
    def device_info(self) -> DeviceInfo:
        """Return the device info."""
        return self._bt_climate.device_info

    @property
    @override
    def is_on(self) -> bool:
        """Return whether open windows and doors turn the heating off."""
        return self._bt_climate.kernel_state.window_detection

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Let open windows and doors turn the heating off again."""
        await set_window_detection(self._bt_climate, True)
        self.async_write_ha_state()

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Keep heating whatever the window and door sensors report."""
        await set_window_detection(self._bt_climate, False)
        self.async_write_ha_state()


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

    @staticmethod
    def build_unique_id(bt_unique_id: str, trv_entity_id: str) -> str:
        """Return the unique id of the child lock switch of ``trv_entity_id``."""
        return f"{bt_unique_id}_{trv_entity_id}_child_lock"

    def __init__(
        self,
        bt_climate: BetterThermostat,
        trv_entity_id: str,
        show_trv_name: bool = True,
    ) -> None:
        """Initialize the switch."""
        self._bt_climate = bt_climate
        self._trv_entity_id = trv_entity_id
        self._attr_unique_id = BetterThermostatChildLockSwitch.build_unique_id(
            bt_climate.unique_id, trv_entity_id
        )
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
                return advanced_flag(trv_config.get("advanced", {}), CONF_CHILD_LOCK)
        return False

    @override
    def _extra_restore_data(self) -> dict[str, object]:
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
        held = advanced_flag(trv.advanced, CONF_CHILD_LOCK)
        trv.advanced[CONF_CHILD_LOCK] = restored
        # Released devices are not written to. The restored lock is the
        # setting from here on; the initialisation that follows taking them
        # back syncs it where the TRV's model quirk does.
        if restored != held and not device_released(
            self._bt_climate, self._trv_entity_id
        ):
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
        return advanced_flag(trv.advanced, CONF_CHILD_LOCK)

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        self._refuse_while_released()
        self._update_state(True)
        await self._set_child_lock(True)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        self._refuse_while_released()
        self._update_state(False)
        await self._set_child_lock(False)

    def _refuse_while_released(self) -> None:
        """Refuse a change of the lock while the device is released.

        The lock is written to the device, and a released device is the
        user's, with the room's devices or on its own. A change accepted
        without the write would leave the switch showing a lock the device
        does not have.
        """
        if self._bt_climate.devices_released:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="devices_released",
                translation_placeholders={"device_name": self._bt_climate.device_name},
            )
        if self._trv_entity_id in self._bt_climate.released_trvs:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="trv_released",
                translation_placeholders={
                    "device_name": self._bt_climate.device_name,
                    "entity": self._trv_entity_id,
                },
            )

    def _update_state(self, state: bool) -> None:
        """Update the state."""
        trv = self._bt_climate.real_trvs.get(self._trv_entity_id)
        if trv is None:
            return
        trv.advanced[CONF_CHILD_LOCK] = state
        self.async_write_ha_state()

    async def _set_child_lock(self, state: bool, *, force: bool = False) -> None:
        """Set the child lock on the real device.

        With ``force`` the command is sent even when the device already
        reports the target state.
        """
        entity_registry = er.async_get(self._bt_climate.hass)
        reg_entity = entity_registry.async_get(self._trv_entity_id)

        if reg_entity is None or reg_entity.device_id is None:
            return

        device_id = reg_entity.device_id

        cl_entity = find_child_lock_entity(entity_registry, device_id)

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
                else:
                    # find_child_lock_entity answers only from the two domains
                    # it searches, so the entity is a lock here.
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
