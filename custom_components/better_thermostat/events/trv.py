"""TRV event handlers and helpers for better_thermostat.

This module contains the various Home Assistant TRV event handlers and
helper functions used by the Better Thermostat integration to read and
convert thermostat states and prepare outbound payloads.
"""

from datetime import datetime
import logging
from custom_components.better_thermostat.utils.const import CONF_HOMEMATICIP

from homeassistant.components.climate.const import HVACMode
from homeassistant.core import State, callback
from custom_components.better_thermostat.utils.helpers import (
    convert_to_float,
    mode_remap,
)
from custom_components.better_thermostat.adapters.delegate import get_current_offset
from custom_components.better_thermostat.utils.helpers import get_device_model
from custom_components.better_thermostat.model_fixes.model_quirks import (
    load_model_quirks,
)

from custom_components.better_thermostat.utils.const import (
    CalibrationType,
    CalibrationMode,
)

from custom_components.better_thermostat.calibration import (
    calculate_calibration_local,
    calculate_calibration_setpoint,
)

_LOGGER = logging.getLogger(__name__)


@callback
async def trigger_trv_change(self, event):
    """Trigger a change in the trv state."""
    if self.startup_running:
        return
    if self.control_queue_task is None:
        return
    if self.bt_target_temp is None or self.cur_temp is None or self.tolerance is None:
        return
    if self.bt_update_lock:
        return
    _main_change = False
    old_state = event.data.get("old_state")
    new_state = event.data.get("new_state")
    entity_id = event.data.get("entity_id")

    if None in (new_state, old_state, new_state.attributes):
        _LOGGER.debug(
            "better_thermostat %s: TRV %s update contained not all necessary data for processing, skipping",
            self.device_name,
            entity_id,
        )
        return

    if not isinstance(new_state, State) or not isinstance(old_state, State):
        _LOGGER.debug(
            "better_thermostat %s: TRV %s update contained not a State, skipping",
            self.device_name,
            entity_id,
        )
        return
    # set context HACK TO FIND OUT IF AN EVENT WAS SEND BY BT

    # Check if the update is coming from the code
    if self.context == event.context:
        return

    # _LOGGER.debug(f"better_thermostat {self.device_name}: TRV {entity_id} update received")

    _org_trv_state = self.hass.states.get(entity_id)
    child_lock = self.real_trvs[entity_id]["advanced"].get("child_lock")

    # Dynamische Modell-Erkennung: nur einmalig (z. B. beim Start) – nicht bei jedem Event
    try:
        prev_model = self.real_trvs.get(entity_id, {}).get("model")
        if not prev_model:
            if _org_trv_state is not None and isinstance(
                _org_trv_state.attributes, dict
            ):
                # Nur prüfen, wenn Hinweise vorhanden sind
                if (
                    "model_id" in _org_trv_state.attributes
                    or "device" in _org_trv_state.attributes
                ):
                    detected = await get_device_model(self, entity_id)
                    if isinstance(detected, str) and detected:
                        if prev_model != detected:
                            _LOGGER.info(
                                "better_thermostat %s: TRV %s model changed: %s -> %s; reloading quirks",
                                self.device_name,
                                entity_id,
                                prev_model,
                                detected,
                            )
                            quirks = await load_model_quirks(self, detected, entity_id)
                            self.real_trvs[entity_id]["model"] = detected
                            self.real_trvs[entity_id]["model_quirks"] = quirks
    except Exception as e:
        _LOGGER.debug(
            "better_thermostat %s: dynamic model detection failed for %s: %s",
            self.device_name,
            entity_id,
            e,
        )

    _new_current_temp = convert_to_float(
        str(_org_trv_state.attributes.get("current_temperature", None)),
        self.device_name,
        "TRV_current_temp",
    )

    _time_diff = 5
    try:
        for trv in self.all_trvs:
            if trv["advanced"][CONF_HOMEMATICIP]:
                _time_diff = 600
    except KeyError:
        pass
    if (
        _new_current_temp is not None
        and self.real_trvs[entity_id]["current_temperature"] != _new_current_temp
        and (
            (datetime.now() - self.last_internal_sensor_change).total_seconds()
            > _time_diff
            or (
                self.real_trvs[entity_id]["calibration_received"] is False
                and self.real_trvs[entity_id]["calibration"] != 1
            )
        )
    ):
        _old_temp = self.real_trvs[entity_id]["current_temperature"]
        self.real_trvs[entity_id]["current_temperature"] = _new_current_temp
        _LOGGER.debug(
            "better_thermostat %s: TRV %s sends new internal temperature from %s to %s",
            self.device_name,
            entity_id,
            _old_temp,
            _new_current_temp,
        )
        self.last_internal_sensor_change = datetime.now()
        _main_change = True

        # async def in controlling? (left as note)
        if self.real_trvs[entity_id]["calibration_received"] is False:
            self.real_trvs[entity_id]["calibration_received"] = True
            _LOGGER.debug(
                "better_thermostat %s: calibration accepted by TRV %s",
                self.device_name,
                entity_id,
            )
            _main_change = False
            if self.real_trvs[entity_id]["calibration"] == 0:
                self.real_trvs[entity_id][
                    "last_calibration"
                ] = await get_current_offset(self, entity_id)

    if self.ignore_states:
        return

    try:
        mapped_state = convert_inbound_states(self, entity_id, _org_trv_state)
    except TypeError:
        _LOGGER.debug(
            "better_thermostat %s: remapping TRV %s state failed, skipping",
            self.device_name,
            entity_id,
        )
        return

    # hvac_action bedingungslos in den Cache schreiben (immer aktuell halten)
    try:
        hvac_action_attr = _org_trv_state.attributes.get("hvac_action")
        if hvac_action_attr is None:
            hvac_action_attr = _org_trv_state.attributes.get("action")
        if hvac_action_attr is not None:
            val = str(hvac_action_attr).strip().lower()
            prev = self.real_trvs[entity_id].get("hvac_action")
            self.real_trvs[entity_id]["hvac_action"] = val
            if prev != val:
                _main_change = True
                _LOGGER.debug(
                    "better_thermostat %s: TRV %s hvac_action changed: %s -> %s",
                    self.device_name,
                    entity_id,
                    prev,
                    val,
                )
    except Exception:
        pass

    if mapped_state in (HVACMode.OFF, HVACMode.HEAT, HVACMode.HEAT_COOL):
        if (
            self.real_trvs[entity_id]["hvac_mode"] != _org_trv_state.state
            and not child_lock
        ):
            _old = self.real_trvs[entity_id]["hvac_mode"]
            _LOGGER.debug(
                "better_thermostat %s: TRV %s decoded TRV mode changed from %s to %s - converted %s",
                self.device_name,
                entity_id,
                _old,
                _org_trv_state.state,
                new_state.state,
            )
            self.real_trvs[entity_id]["hvac_mode"] = _org_trv_state.state
            _main_change = True
            if (
                child_lock is False
                and self.real_trvs[entity_id]["system_mode_received"] is True
                and self.real_trvs[entity_id]["last_hvac_mode"] != _org_trv_state.state
            ):
                self.bt_hvac_mode = mapped_state

    # Hinweis: Kein Caching von hvac_action mehr – BT liest direkt vom TRV-State in climate.py

    _main_key = "temperature"
    if "temperature" not in old_state.attributes:
        _main_key = "target_temp_low"

    _old_heating_setpoint = convert_to_float(
        str(old_state.attributes.get(_main_key, None)),
        self.device_name,
        "trigger_trv_change()",
    )
    _new_heating_setpoint = convert_to_float(
        str(new_state.attributes.get(_main_key, None)),
        self.device_name,
        "trigger_trv_change()",
    )
    if (
        _new_heating_setpoint is not None
        and _old_heating_setpoint is not None
        and self.bt_hvac_mode is not HVACMode.OFF
    ):
        _LOGGER.debug(
            "better_thermostat %s: trigger_trv_change / _old_heating_setpoint: %s - _new_heating_setpoint: %s - _last_temperature: %s",
            self.device_name,
            _old_heating_setpoint,
            _new_heating_setpoint,
            self.real_trvs[entity_id]["last_temperature"],
        )
        if (
            _new_heating_setpoint < self.bt_min_temp
            or self.bt_max_temp < _new_heating_setpoint
        ):
            _LOGGER.warning(
                "better_thermostat %s: New TRV %s setpoint outside of range, overwriting it",
                self.device_name,
                entity_id,
            )

            if _new_heating_setpoint < self.bt_min_temp:
                _new_heating_setpoint = self.bt_min_temp
            else:
                _new_heating_setpoint = self.bt_max_temp

        if (
            self.bt_target_temp != _new_heating_setpoint
            and _old_heating_setpoint != _new_heating_setpoint
            and self.real_trvs[entity_id]["last_temperature"] != _new_heating_setpoint
            and not child_lock
            and self.real_trvs[entity_id]["target_temp_received"] is True
            and self.real_trvs[entity_id]["system_mode_received"] is True
            and self.real_trvs[entity_id]["hvac_mode"] is not HVACMode.OFF
            and self.window_open is False
        ):
            _calibration_type = self.real_trvs[entity_id]["advanced"].get("calibration")
            if _calibration_type == CalibrationType.TARGET_TEMP_BASED:
                _LOGGER.debug(
                    "better_thermostat %s: TRV %s target temp change ignored because of calibration type %s",
                    self.device_name,
                    entity_id,
                    _calibration_type,
                )
            else:
                _LOGGER.debug(
                    "better_thermostat %s: TRV %s decoded TRV target temp changed from %s to %s",
                    self.device_name,
                    entity_id,
                    self.bt_target_temp,
                    _new_heating_setpoint,
                )
                self.bt_target_temp = _new_heating_setpoint
                if self.cooler_entity_id is not None:
                    if self.bt_target_temp <= self.bt_target_cooltemp:
                        self.bt_target_cooltemp = (
                            self.bt_target_temp - self.bt_target_temp_step
                        )
                    if self.bt_target_temp >= self.bt_target_cooltemp:
                        self.bt_target_cooltemp = (
                            self.bt_target_temp - self.bt_target_temp_step
                        )

                _main_change = True

        if self.real_trvs[entity_id]["advanced"].get("no_off_system_mode", False):
            if _new_heating_setpoint == self.real_trvs[entity_id]["min_temp"]:
                # Only set OFF if window is NOT open - min_temp during window
                # open was set by BT, not by user turning off heating
                if not self.window_open:
                    self.bt_hvac_mode = HVACMode.OFF
            else:
                self.bt_hvac_mode = HVACMode.HEAT
            _main_change = True

    if _main_change is True:
        self.async_write_ha_state()
        return await self.control_queue_task.put(self)

    self.async_write_ha_state()
    return


def convert_inbound_states(self, entity_id, state: State) -> str | None:
    """Convert HVAC mode in a thermostat state from Home Assistant.

    Parameters
    ----------
    self :
        self instance of better_thermostat
    state : State
        Inbound thermostat state, which will be modified

    Returns
    -------
    Modified state
    """

    if state is None:
        raise TypeError("convert_inbound_states() received None state, cannot convert")

    if state.attributes is None or state.state is None:
        raise TypeError("convert_inbound_states() received None state, cannot convert")

    remapped_state = mode_remap(self, entity_id, str(state.state), True)

    if remapped_state not in (HVACMode.OFF, HVACMode.HEAT):
        return None
    return remapped_state


def convert_outbound_states(self, entity_id, hvac_mode) -> dict | None:
    """Convert outbound states for TRV control.

    Returns the payload for setting the TRV state.
    """
    _new_local_calibration = None
    _new_heating_setpoint = None

    try:
        _calibration_type = self.real_trvs[entity_id]["advanced"].get("calibration")
        _calibration_mode = self.real_trvs[entity_id]["advanced"].get(
            "calibration_mode"
        )

        if _calibration_type is None:
            _LOGGER.warning(
                "better_thermostat %s: no calibration type found in device config, talking to the TRV using fallback mode",
                self.device_name,
            )
            # Fallback: keine lokale Kalibrierung durchführen, nur Solltemperatur setzen
            _new_heating_setpoint = self.bt_target_temp
            _new_local_calibration = None

        else:
            if _calibration_type == CalibrationType.LOCAL_BASED:
                _new_local_calibration = calculate_calibration_local(self, entity_id)

                _new_heating_setpoint = self.bt_target_temp

            elif _calibration_type == CalibrationType.TARGET_TEMP_BASED:
                if _calibration_mode == CalibrationMode.NO_CALIBRATION:
                    _new_heating_setpoint = self.bt_target_temp
                else:
                    _new_heating_setpoint = calculate_calibration_setpoint(
                        self, entity_id
                    )

            _system_modes = self.real_trvs[entity_id]["hvac_modes"]
            _has_system_mode = _system_modes is not None

            # Handling different devices with or without system mode reported or contained in the device config

            # Normalize without forcing to str to avoid values like "HVACMode.HEAT"
            _orig_mode = hvac_mode
            hvac_mode = mode_remap(self, entity_id, hvac_mode, False)
            _LOGGER.debug(
                "better_thermostat %s: convert_outbound_states(%s) system_mode in=%s out=%s",
                self.device_name,
                entity_id,
                _orig_mode,
                hvac_mode,
            )

            if not _has_system_mode:
                _LOGGER.debug(
                    "better_thermostat %s: device config expects no system mode, while the device has one. Device system mode will be ignored",
                    self.device_name,
                )
                if hvac_mode == HVACMode.OFF:
                    _new_heating_setpoint = self.real_trvs[entity_id]["min_temp"]
                hvac_mode = None
                _LOGGER.debug(
                    "better_thermostat %s: convert_outbound_states(%s) suppressing system_mode for no-off device",
                    self.device_name,
                    entity_id,
                )
            if hvac_mode == HVACMode.OFF and (
                (_system_modes is not None and HVACMode.OFF not in _system_modes)
                or self.real_trvs[entity_id]["advanced"].get("no_off_system_mode")
            ):
                _min_temp = self.real_trvs[entity_id]["min_temp"]
                _LOGGER.debug(
                    "better_thermostat %s: sending %s°C to the TRV because this device has no system mode off and heater should be off",
                    self.device_name,
                    _min_temp,
                )
                _new_heating_setpoint = _min_temp
                hvac_mode = None

        # Build payload; include calibration only if present
        _payload = {
            "temperature": _new_heating_setpoint,
            "local_temperature": self.real_trvs[entity_id]["current_temperature"],
            "system_mode": hvac_mode,
        }
        if _new_local_calibration is not None:
            _payload["local_temperature_calibration"] = _new_local_calibration
        return _payload
    except Exception as e:
        _LOGGER.error(e)
        return None
