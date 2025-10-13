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
from custom_components.better_thermostat.balance import (
    compute_balance,
    BalanceInput,
    BalanceParams,
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
    _main_change = False
    old_state = event.data.get("old_state")
    new_state = event.data.get("new_state")
    entity_id = event.data.get("entity_id")

    if None in (new_state, old_state, new_state.attributes):
        _LOGGER.debug(
            f"better_thermostat {self.device_name}: TRV {entity_id} update contained not all necessary data for processing, skipping"
        )
        return

    if not isinstance(new_state, State) or not isinstance(old_state, State):
        _LOGGER.debug(
            f"better_thermostat {self.device_name}: TRV {entity_id} update contained not a State, skipping"
        )
        return
    # set context HACK TO FIND OUT IF AN EVENT WAS SEND BY BT

    # Check if the update is coming from the code
    if self.context == event.context:
        return

    # _LOGGER.debug(f"better_thermostat {self.device_name}: TRV {entity_id} update received")

    _org_trv_state = self.hass.states.get(entity_id)
    child_lock = self.real_trvs[entity_id]["advanced"].get("child_lock")

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
            f"better_thermostat {self.device_name}: TRV {entity_id} sends new internal temperature from {_old_temp} to {_new_current_temp}"
        )
        self.last_internal_sensor_change = datetime.now()
        _main_change = True

        # TODO: async def in controlling?
        if self.real_trvs[entity_id]["calibration_received"] is False:
            self.real_trvs[entity_id]["calibration_received"] = True
            _LOGGER.debug(
                f"better_thermostat {self.device_name}: calibration accepted by TRV {entity_id}"
            )
            _main_change = False
            if self.real_trvs[entity_id]["calibration"] == 0:
                self.real_trvs[entity_id]["last_calibration"] = (
                    await get_current_offset(self, entity_id)
                )

    if self.ignore_states:
        return

    try:
        mapped_state = convert_inbound_states(self, entity_id, _org_trv_state)
    except TypeError:
        _LOGGER.debug(
            f"better_thermostat {self.device_name}: remapping TRV {entity_id} state failed, skipping"
        )
        return

    if mapped_state in (HVACMode.OFF, HVACMode.HEAT, HVACMode.HEAT_COOL):
        if (
            self.real_trvs[entity_id]["hvac_mode"] != _org_trv_state.state
            and not child_lock
        ):
            _old = self.real_trvs[entity_id]["hvac_mode"]
            _LOGGER.debug(
                f"better_thermostat {self.device_name}: TRV {entity_id} decoded TRV mode changed from {_old} to {_org_trv_state.state} - converted {new_state.state}"
            )
            self.real_trvs[entity_id]["hvac_mode"] = _org_trv_state.state
            _main_change = True
            if (
                child_lock is False
                and self.real_trvs[entity_id]["system_mode_received"] is True
                and self.real_trvs[entity_id]["last_hvac_mode"] != _org_trv_state.state
            ):
                self.bt_hvac_mode = mapped_state

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
            f"better_thermostat {self.device_name}: trigger_trv_change test / _old_heating_setpoint: {_old_heating_setpoint} - _new_heating_setpoint: {_new_heating_setpoint} - _last_temperature: {self.real_trvs[entity_id]['last_temperature']}"
        )
        if (
            _new_heating_setpoint < self.bt_min_temp
            or self.bt_max_temp < _new_heating_setpoint
        ):
            _LOGGER.warning(
                f"better_thermostat {self.device_name}: New TRV {entity_id} setpoint outside of range, overwriting it"
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
            _LOGGER.debug(
                f"better_thermostat {self.device_name}: TRV {entity_id} decoded TRV target temp changed from {self.bt_target_temp} to {_new_heating_setpoint}"
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
    """Convert hvac mode in a thermostat state from HA
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


def _apply_hydraulic_balance(
    self,
    entity_id: str,
    hvac_mode,
    current_setpoint,
    calibration_type,
    calibration_mode,
    precheck_applies: bool | None = None,
):
    """Apply decentralized hydraulic balance if enabled via calibration mode.

    Returns the potentially updated setpoint. Also writes debug info to
    self.real_trvs[entity_id]["balance"].
    """
    try:
        min_t = self.real_trvs[entity_id].get("min_temp") or self.bt_min_temp
        max_t = self.real_trvs[entity_id].get("max_temp") or self.bt_max_temp
        cond_has_cur = self.cur_temp is not None
        cond_has_target = self.bt_target_temp is not None
        cond_hvac_ok = hvac_mode is not None and hvac_mode != HVACMode.OFF
        cond_window_closed = self.window_open is False
        cond_not_min_temp_off = (
            current_setpoint is None or current_setpoint > (min_t + 0.05)
        )
        cond_enabled = calibration_mode == CalibrationMode.HYDRAULIC_BALANCE

        apply_balance = (
            cond_has_cur
            and cond_has_target
            and cond_hvac_ok
            and cond_window_closed
            and cond_not_min_temp_off
            and cond_enabled
        )
        # If caller provided an early precheck, keep it as a sanity requirement
        if precheck_applies is not None:
            apply_balance = apply_balance and precheck_applies

        _LOGGER.debug(
            (
                "better_thermostat %s: balance pre-check for %s: apply=%s | "
                "inputs target=%.2f current=%.2f tol=%.2f slope=%s hvac_mode=%s "
                "window_open=%s min_t=%.2f max_t=%.2f initial_setpoint=%s | "
                "conds has_cur=%s has_target=%s hvac_ok=%s window_closed=%s "
                "not_min_off=%s enabled=%s"
            ),
            self.device_name,
            entity_id,
            apply_balance,
            (self.bt_target_temp if self.bt_target_temp is not None else float("nan")),
            (self.cur_temp if self.cur_temp is not None else float("nan")),
            float(getattr(self, "tolerance", 0.0) or 0.0),
            getattr(self, "temp_slope", None),
            hvac_mode,
            self.window_open,
            min_t,
            max_t,
            current_setpoint,
            cond_has_cur,
            cond_has_target,
            cond_hvac_ok,
            cond_window_closed,
            cond_not_min_temp_off,
            cond_enabled,
        )

        if not apply_balance:
            _LOGGER.debug(
                (
                    "better_thermostat %s: balance NOT applied for %s (conds) -> "
                    "has_cur=%s has_target=%s hvac_ok=%s window_closed=%s "
                    "not_min_off=%s enabled=%s"
                ),
                self.device_name,
                entity_id,
                cond_has_cur,
                cond_has_target,
                cond_hvac_ok,
                cond_window_closed,
                cond_not_min_temp_off,
                cond_enabled,
            )
            return current_setpoint

        bal = compute_balance(
            BalanceInput(
                key=f"{self._unique_id}:{entity_id}",
                target_temp_C=self.bt_target_temp,
                current_temp_C=self.cur_temp,
                tolerance_K=float(getattr(self, "tolerance", 0.0) or 0.0),
                temp_slope_K_per_min=getattr(self, "temp_slope", None),
                window_open=self.window_open,
                heating_allowed=True,
            )
        )
        # Schedule a debounced persistence save (if the entity supports it)
        try:
            if hasattr(self, "_schedule_save_balance_state"):
                self._schedule_save_balance_state()
        except Exception:
            pass
        _LOGGER.debug(
            (
                "better_thermostat %s: balance result for %s: valve=%.1f%% "
                "flow_cap_K=%s setpoint_eff=%s sonoff_min=%s%% sonoff_max=%s%%"
            ),
            self.device_name,
            entity_id,
            (bal.valve_percent if bal.valve_percent is not None else float("nan")),
            bal.flow_cap_K,
            bal.setpoint_eff_C,
            bal.sonoff_min_open_pct,
            bal.sonoff_max_open_pct,
        )

        # Save debug
        self.real_trvs[entity_id]["balance"] = {
            "valve_percent": bal.valve_percent,
            "flow_cap_K": bal.flow_cap_K,
            "setpoint_eff_C": bal.setpoint_eff_C,
            "sonoff_min_open_pct": bal.sonoff_min_open_pct,
            "sonoff_max_open_pct": bal.sonoff_max_open_pct,
        }

        # Only in HYDRAULIC_BALANCE mode we adjust setpoint.
        # Use a symmetric adjustment around the calibration/base setpoint:
        # - If demand present (current < target, delta_T > 0), increase setpoint by flow_cap_K
        # - If overshoot (current >= target, delta_T <= 0), decrease setpoint by flow_cap_K
        if calibration_mode == CalibrationMode.HYDRAULIC_BALANCE:
            try:
                base_sp = current_setpoint if current_setpoint is not None else self.bt_target_temp
                if base_sp is None:
                    return current_setpoint
                delta_T = (self.bt_target_temp - self.cur_temp) if (
                    self.bt_target_temp is not None and self.cur_temp is not None) else 0.0
                # Combine valve-derived cap with demand-based magnitude from ΔT
                bp = BalanceParams()
                demand_gain = 0.6  # K/K scaling; tuneable
                demand_mag = min(bp.cap_max_K, abs(delta_T) * demand_gain)
                magnitude = max(bal.flow_cap_K or 0.0, demand_mag)
                proposed = base_sp + (magnitude if delta_T > 0.0 else -magnitude)
                new_sp = max(min_t, min(max_t, proposed))
                _LOGGER.debug(
                    (
                        "better_thermostat %s: balance applied setpoint for %s (symmetric): "
                        "base=%s delta_T=%.3f flow_cap_K=%.3f demand_mag=%.3f -> proposed=%s clamped=%s within [%.2f, %.2f]"
                    ),
                    self.device_name,
                    entity_id,
                    base_sp,
                    delta_T,
                    (bal.flow_cap_K or 0.0),
                    demand_mag,
                    proposed,
                    new_sp,
                    min_t,
                    max_t,
                )
                return new_sp
            except TypeError:
                return current_setpoint

        return current_setpoint
    except Exception as e:
        _LOGGER.debug(
            "better_thermostat %s: balance compute failed for %s: %s",
            self.device_name,
            entity_id,
            e,
        )
        return current_setpoint


def convert_outbound_states(self, entity_id, hvac_mode) -> dict | None:
    """Creates the new outbound thermostat state.
    Parameters
    ----------
    self :
            self instance of better_thermostat
    hvac_mode :
            the HA mode to convert to
    Returns
    -------
    dict
            A dictionary containing the new outbound thermostat state containing the following keys:
                    temperature: float
                    local_temperature: float
                    local_temperature_calibration: float
                    system_mode: string
    None
            In case of an error.
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
            _new_heating_setpoint = self.bt_target_temp
            _new_local_calibration = calculate_calibration_local(self, entity_id)

            if _new_local_calibration is None:
                return None

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
                    f"better_thermostat {self.device_name}: device config expects no system mode, while the device has one. Device system mode will be ignored"
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
                HVACMode.OFF not in _system_modes
                or self.real_trvs[entity_id]["advanced"].get("no_off_system_mode")
            ):
                _min_temp = self.real_trvs[entity_id]["min_temp"]
                _LOGGER.debug(
                    f"better_thermostat {self.device_name}: sending {_min_temp}°C to the TRV because this device has no system mode off and heater should be off"
                )
                _new_heating_setpoint = _min_temp
                hvac_mode = None

            # Early balance precondition (simple check, full check/logging in helper)
            _balance_precheck = (
                _calibration_mode == CalibrationMode.HYDRAULIC_BALANCE
                and self.cur_temp is not None
                and self.bt_target_temp is not None
                and hvac_mode is not None
                and hvac_mode != HVACMode.OFF
                and self.window_open is False
            )

    # --- Hydraulic balance (decentralized): percentage & setpoint throttling ---
        _new_heating_setpoint = _apply_hydraulic_balance(
            self,
            entity_id,
            hvac_mode,
            _new_heating_setpoint,
            _calibration_type,
            _calibration_mode,
            _balance_precheck,
        )

        return {
            "temperature": _new_heating_setpoint,
            "local_temperature": self.real_trvs[entity_id]["current_temperature"],
            "system_mode": hvac_mode,
            "local_temperature_calibration": _new_local_calibration,
        }
    except Exception as e:
        _LOGGER.error(e)
        return None
