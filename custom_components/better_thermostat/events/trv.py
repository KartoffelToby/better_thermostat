from datetime import datetime
import logging
from typing import Any, Dict, cast
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
from custom_components.better_thermostat.utils.helpers import get_device_model
from custom_components.better_thermostat.model_fixes.model_quirks import (
    load_model_quirks,
)

from custom_components.better_thermostat.utils.const import (
    CalibrationType,
    CalibrationMode,
)
from homeassistant.util import dt as dt_util

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

    # Dynamische Modell-Erkennung: falls sich das Modell geändert hat (z. B. Z2M liefert model_id), Quirks neu laden
    try:
        if _org_trv_state is not None and isinstance(_org_trv_state.attributes, dict):
            # Nur prüfen, wenn Hinweise vorhanden sind
            if (
                "model_id" in _org_trv_state.attributes
                or "device" in _org_trv_state.attributes
            ):
                detected = await get_device_model(self, entity_id)
                if isinstance(detected, str) and detected:
                    prev = self.real_trvs.get(entity_id, {}).get("model")
                    if prev != detected:
                        _LOGGER.info(
                            "better_thermostat %s: TRV %s model changed: %s -> %s; reloading quirks",
                            self.device_name,
                            entity_id,
                            prev,
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
                self.real_trvs[entity_id]["last_calibration"] = (
                    await get_current_offset(self, entity_id)
                )

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
    _calibration_type,
    _calibration_mode,
    precheck_applies: bool | None = None,
):
    """Compute decentralized balance suggestions and learn Sonoff/TRV open caps.

    This no longer changes setpoints; instead it records recommendations and
    updates learned min/max open percentages per TRV and per target temperature bucket.
    Returns the (unchanged) current_setpoint.
    """
    try:
        min_t = self.real_trvs[entity_id].get("min_temp") or self.bt_min_temp
        max_t = self.real_trvs[entity_id].get("max_temp") or self.bt_max_temp
        cond_has_cur = self.cur_temp is not None
        cond_has_target = self.bt_target_temp is not None
        cond_hvac_ok = hvac_mode is not None and hvac_mode != HVACMode.OFF
        cond_window_closed = self.window_open is False
        cond_not_min_temp_off = current_setpoint is None or current_setpoint > (
            min_t + 0.05
        )
        # Always compute suggestions (we don't change setpoints here)
        apply_balance = (
            cond_has_cur
            and cond_has_target
            and cond_hvac_ok
            and cond_window_closed
            and cond_not_min_temp_off
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
                "not_min_off=%s"
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
        )

        if not apply_balance:
            _LOGGER.debug(
                (
                    "better_thermostat %s: balance NOT applied for %s (conds) -> "
                    "has_cur=%s has_target=%s hvac_ok=%s window_closed=%s "
                    "not_min_off=%s"
                ),
                self.device_name,
                entity_id,
                cond_has_cur,
                cond_has_target,
                cond_hvac_ok,
                cond_window_closed,
                cond_not_min_temp_off,
            )
            return current_setpoint

        # Build balance parameters (optionally from per-TRV advanced settings)
        adv = self.real_trvs.get(entity_id, {}).get("advanced", {}) or {}
        try:
            mode = str(adv.get("balance_mode", "heuristic")).lower()
        except Exception:
            mode = "heuristic"

        # Explizit deaktiviert? Dann Balance überspringen und current_setpoint zurückgeben
        if mode in ("none", "off", ""):
            _LOGGER.debug(
                "better_thermostat %s: balance explicitly disabled for %s (mode=%s)",
                self.device_name,
                entity_id,
                mode,
            )
            return current_setpoint
        try:
            kp = float(adv.get("pid_kp", 60.0))
        except Exception:
            kp = 60.0
        try:
            ki = float(adv.get("pid_ki", 0.01))
        except Exception:
            ki = 0.01
        try:
            kd = float(adv.get("pid_kd", 2000.0))
        except Exception:
            kd = 2000.0
        auto_tune = bool(adv.get("pid_auto_tune", True))
        try:
            trend_mix_trv = float(adv.get("trend_mix_trv", 0.7))
        except Exception:
            trend_mix_trv = 0.7
        try:
            percent_hyst = float(adv.get("percent_hysteresis_pts", 1.0))
        except Exception:
            percent_hyst = 1.0
        try:
            min_interval = float(adv.get("min_update_interval_s", 60.0))
        except Exception:
            min_interval = 60.0
        params = BalanceParams(
            mode=mode,
            kp=kp,
            ki=ki,
            kd=kd,
            auto_tune=auto_tune,
            trend_mix_trv=trend_mix_trv,
            percent_hysteresis_pts=percent_hyst,
            min_update_interval_s=min_interval,
        )

        # Build balance state key and include target bucket (0.5°C rounded) so PID gains learn per bucket
        try:
            tcur = self.bt_target_temp
            bucket_tag = (
                f"t{round(float(tcur) * 2.0) / 2.0:.1f}"
                if isinstance(tcur, (int, float))
                else "tunknown"
            )
        except Exception:
            bucket_tag = "tunknown"
        # Use public unique_id property if available
        uid = getattr(self, "unique_id", None) or getattr(self, "_unique_id", "bt")
        balance_key = f"{uid}:{entity_id}:{bucket_tag}"

        bal = compute_balance(
            BalanceInput(
                key=balance_key,
                target_temp_C=self.bt_target_temp,
                current_temp_C=self.cur_temp,
                trv_temp_C=self.real_trvs.get(entity_id, {}).get("current_temperature"),
                tolerance_K=float(getattr(self, "tolerance", 0.0) or 0.0),
                temp_slope_K_per_min=getattr(self, "temp_slope", None),
                window_open=self.window_open,
                heating_allowed=True,
            ),
            params,
        )
        # Gentle transfer of PID gains from neighboring buckets (first-time init)
        try:
            adv_cfg = self.real_trvs.get(entity_id, {}).get("advanced", {}) or {}
            transfer_enable = bool(adv_cfg.get("pid_bucket_transfer", True))
        except Exception:
            transfer_enable = True
        if transfer_enable and str(params.mode).lower() == "pid":
            try:
                from custom_components.better_thermostat.balance import (
                    get_balance_state,
                    seed_pid_gains,
                )

                st_cur = get_balance_state(balance_key)
                missing = st_cur is None or (
                    st_cur.pid_kp is None
                    or st_cur.pid_ki is None
                    or st_cur.pid_kd is None
                )
                if missing and isinstance(self.bt_target_temp, (int, float)):
                    base = round(float(self.bt_target_temp) * 2.0) / 2.0
                    neighbors = [
                        f"{uid}:{entity_id}:t{base + 0.5:.1f}",
                        f"{uid}:{entity_id}:t{base - 0.5:.1f}",
                        f"{uid}:{entity_id}:t{base + 1.0:.1f}",
                        f"{uid}:{entity_id}:t{base - 1.0:.1f}",
                    ]
                    for nk in neighbors:
                        st_n = get_balance_state(nk)
                        if st_n and (
                            st_n.pid_kp is not None
                            or st_n.pid_ki is not None
                            or st_n.pid_kd is not None
                        ):
                            if seed_pid_gains(
                                balance_key,
                                kp=st_n.pid_kp,
                                ki=st_n.pid_ki,
                                kd=st_n.pid_kd,
                            ):
                                _LOGGER.debug(
                                    "better_thermostat %s: seeded PID gains for %s from neighbor %s",
                                    self.device_name,
                                    balance_key,
                                    nk,
                                )
                                break
            except Exception:
                pass
        # Clamp the computed valve percent to the learned max_open% for the current target bucket (if available)
        try:
            t = self.bt_target_temp
            bucket_now = (
                f"{round(float(t) * 2.0) / 2.0:.1f}"
                if isinstance(t, (int, float))
                else None
            )
            if bucket_now:
                caps_trv = (self.open_caps or {}).get(entity_id, {}) or {}
                caps_now = caps_trv.get(bucket_now)
                if isinstance(caps_now, dict):
                    cap_max = caps_now.get("max_open_pct")
                else:
                    cap_max = None
                if isinstance(cap_max, (int, float)) and isinstance(
                    bal.valve_percent, (int, float)
                ):
                    capped = int(max(0, min(int(cap_max), int(bal.valve_percent))))
                    if capped != bal.valve_percent:
                        bal.valve_percent = capped
        except Exception:
            pass
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
        # Additionally log learned PID gains (if PID mode active), include bucket tag for clarity
        try:
            dbg = getattr(bal, "debug", None) or {}
            pid = dbg.get("pid") or {}
            if str(pid.get("mode")).lower() == "pid":
                try:
                    tcur = self.bt_target_temp
                    bucket_tag = (
                        f"t{round(float(tcur) * 2.0) / 2.0:.1f}"
                        if isinstance(tcur, (int, float))
                        else "tunknown"
                    )
                except Exception:
                    bucket_tag = "tunknown"
                # Hole delta_T und slope_ema robust (erst aus pid, dann aus debug) und zusätzlich slope_in
                dT = pid.get("delta_T") if isinstance(pid, dict) else None
                if dT is None:
                    dT = dbg.get("delta_T") if isinstance(dbg, dict) else None
                slope_ema = pid.get("slope_ema") if isinstance(pid, dict) else None
                if slope_ema is None:
                    slope_ema = dbg.get("slope_ema") if isinstance(dbg, dict) else None
                slope_in = getattr(self, "temp_slope", None)
                # Messwerte und Mischgewichte (falls vorhanden) mitloggen
                meas_ext = pid.get("meas_external_C") if isinstance(pid, dict) else None
                if meas_ext is None:
                    meas_ext = (
                        dbg.get("meas_external_C") if isinstance(dbg, dict) else None
                    )
                meas_trv = pid.get("meas_trv_C") if isinstance(pid, dict) else None
                if meas_trv is None:
                    meas_trv = dbg.get("meas_trv_C") if isinstance(dbg, dict) else None
                meas_blend = pid.get("meas_blend_C") if isinstance(pid, dict) else None
                mix_w_int = pid.get("mix_w_internal") if isinstance(pid, dict) else None
                mix_w_ext = pid.get("mix_w_external") if isinstance(pid, dict) else None
                _LOGGER.debug(
                    "better_thermostat %s: balance pid for %s@%s: kp=%s ki=%s kd=%s | P=%s I=%s D=%s U=%s | dt_s=%s | dT=%sK slope_in=%sK/min slope_ema=%sK/min | ext=%s°C trv=%s°C blend=%s°C mix_in=%s mix_ex=%s",
                    self.device_name,
                    entity_id,
                    bucket_tag,
                    pid.get("kp"),
                    pid.get("ki"),
                    pid.get("kd"),
                    pid.get("p"),
                    pid.get("i"),
                    pid.get("d"),
                    pid.get("u"),
                    pid.get("dt_s"),
                    dT,
                    slope_in,
                    slope_ema,
                    meas_ext,
                    meas_trv,
                    meas_blend,
                    mix_w_int,
                    mix_w_ext,
                )
        except Exception:
            pass

        # Save debug
        self.real_trvs[entity_id]["balance"] = {
            "valve_percent": bal.valve_percent,
            "flow_cap_K": bal.flow_cap_K,
            "setpoint_eff_C": bal.setpoint_eff_C,
            "sonoff_min_open_pct": bal.sonoff_min_open_pct,
            "sonoff_max_open_pct": bal.sonoff_max_open_pct,
            "debug": getattr(bal, "debug", None),
        }
        # --- Learn per-target-temperature min/max open caps ---
        try:
            # Phase-Erkennung anhand ΔT und konservativen Bändern
            # nutzt Parameter (inkl. Bändern) aus balance.py bzw. ggf. Advanced-Overrides
            dT = None
            try:
                if self.bt_target_temp is not None and self.cur_temp is not None:
                    dT = float(self.bt_target_temp) - float(self.cur_temp)
            except Exception:
                dT = None
            # Nur in Heizphase (ausreichend unter Soll) max_open lernen
            learn_max = (dT is not None) and (dT >= params.band_near_K)
            # Nur in Halte-/Abkühlphasen min_open lernen (nahe oder unter Soll)
            learn_min = (dT is not None) and (dT <= params.band_near_K)

            # Bucket by heating target (round to 0.5°C for stability)
            t = self.bt_target_temp
            bucket = (
                f"{round(float(t) * 2.0) / 2.0:.1f}"
                if isinstance(t, (int, float))
                else "unknown"
            )
            if bucket != "unknown":
                # Initialize bucket
                caps_trv = self.open_caps.setdefault(entity_id, {})
                caps = caps_trv.get(bucket)
                # Vorschläge nur übernehmen, wenn für die Phase sinnvoll und numerisch
                suggested_min = None
                if learn_min and isinstance(bal.sonoff_min_open_pct, (int, float)):
                    suggested_min = int(max(0, min(100, int(bal.sonoff_min_open_pct))))
                suggested_max = None
                if learn_max and isinstance(bal.sonoff_max_open_pct, (int, float)):
                    suggested_max = int(max(0, min(100, int(bal.sonoff_max_open_pct))))
                # Ensure min <= max
                if (
                    suggested_min is not None
                    and suggested_max is not None
                    and suggested_min > suggested_max
                ):
                    suggested_min = suggested_max
                # Learning: coarse (5%) when far away, fine (1%) when close

                def _towards(cur: int, target: int) -> int:
                    if cur is None:
                        # first guess in coarse 5% steps
                        return int(round(target / 5.0) * 5)
                    diff = target - cur
                    step = 5 if abs(diff) > 10 else 1
                    if abs(diff) <= step:
                        return target
                    return cur + (step if diff > 0 else -step)

                if not isinstance(caps, dict):
                    # Initialisierung: fehlende Vorschläge mit sinnvollen Defaults füllen
                    # min: Komfort-Default (5%), max: unbeschränkt (100%)
                    if suggested_min is None:
                        new_min = int(
                            round(BalanceParams().sonoff_min_open_default_pct / 5.0) * 5
                        )
                    else:
                        new_min = int(round(suggested_min / 5.0) * 5)
                    if suggested_max is None:
                        new_max = 100
                    else:
                        new_max = int(round(suggested_max / 5.0) * 5)
                    caps = {"min_open_pct": new_min, "max_open_pct": new_max}
                    caps_trv[bucket] = caps
                    _LOGGER.debug(
                        "better_thermostat %s: init open caps for %s@%s → min=%s max=%s (suggested min=%s max=%s)",
                        self.device_name,
                        entity_id,
                        bucket,
                        new_min,
                        new_max,
                        suggested_min,
                        suggested_max,
                    )
                    # Immediately schedule persistence and refresh HA state
                    if hasattr(self, "_schedule_save_open_caps"):
                        self._schedule_save_open_caps()
                    try:
                        if hasattr(self.hass, "async_create_task"):
                            self.hass.async_create_task(
                                self.async_update_ha_state(force_refresh=True)
                            )
                        self.async_write_ha_state()
                    except Exception:
                        pass
                else:
                    caps = cast(Dict[str, Any], caps)
                    cur_min = int(
                        caps.get(
                            "min_open_pct", BalanceParams().sonoff_min_open_default_pct
                        )
                    )
                    cur_max = int(caps.get("max_open_pct", 100))
                    # Nur die in dieser Phase relevanten Werte anpassen, den anderen unverändert lassen
                    new_min = cur_min
                    new_max = cur_max
                    if suggested_min is not None:
                        new_min = _towards(cur_min, suggested_min)
                    if suggested_max is not None:
                        new_max = _towards(cur_max, suggested_max)
                    # maintain ordering
                    if new_min > new_max:
                        new_min = new_max
                    changed = (new_min != cur_min) or (new_max != cur_max)
                    if changed:
                        caps["min_open_pct"] = new_min
                        caps["max_open_pct"] = new_max
                        _LOGGER.debug(
                            "better_thermostat %s: updated open caps for %s@%s → min=%s max=%s (suggested min=%s max=%s)",
                            self.device_name,
                            entity_id,
                            bucket,
                            new_min,
                            new_max,
                            suggested_min,
                            suggested_max,
                        )
                        # Schedule persistence and refresh HA state on change
                        if hasattr(self, "_schedule_save_open_caps"):
                            self._schedule_save_open_caps()
                        try:
                            if hasattr(self.hass, "async_create_task"):
                                self.hass.async_create_task(
                                    self.async_update_ha_state(force_refresh=True)
                                )
                            self.async_write_ha_state()
                        except Exception:
                            pass

                # Update per-bucket stats (lightweight learning diagnostics)
                try:
                    # Ensure dict types for stats
                    caps = cast(Dict[str, Any], caps)
                    stats = caps.get("stats")
                    if not isinstance(stats, dict):
                        stats = {}
                        caps["stats"] = stats
                    prev_samples = int(stats.get("samples", 0))
                    samples = prev_samples + 1
                    stats["samples"] = samples

                    # Avg slope (K/min)
                    slope = getattr(self, "temp_slope", None)
                    if isinstance(slope, (int, float)):
                        prev_avg = stats.get("avg_slope_K_min")
                        if isinstance(prev_avg, (int, float)) and prev_samples > 0:
                            stats["avg_slope_K_min"] = round(
                                (prev_avg * prev_samples + float(slope)) / samples, 5
                            )
                        else:
                            stats["avg_slope_K_min"] = round(float(slope), 5)

                    # Avg valve percent
                    if isinstance(bal.valve_percent, (int, float)):
                        prev_avg_v = stats.get("avg_valve_percent")
                        if isinstance(prev_avg_v, (int, float)) and prev_samples > 0:
                            stats["avg_valve_percent"] = round(
                                (prev_avg_v * prev_samples + float(bal.valve_percent))
                                / samples,
                                2,
                            )
                        else:
                            stats["avg_valve_percent"] = round(
                                float(bal.valve_percent), 2
                            )

                    # Avg delta_T (target - current)
                    try:
                        if (
                            self.bt_target_temp is not None
                            and self.cur_temp is not None
                        ):
                            dT = float(self.bt_target_temp) - float(self.cur_temp)
                            prev_avg_dT = stats.get("avg_delta_T_K")
                            if (
                                isinstance(prev_avg_dT, (int, float))
                                and prev_samples > 0
                            ):
                                stats["avg_delta_T_K"] = round(
                                    (prev_avg_dT * prev_samples + dT) / samples, 4
                                )
                            else:
                                stats["avg_delta_T_K"] = round(dT, 4)
                    except Exception:
                        pass

                    # Timestamp (ISO)
                    try:
                        stats["last_update_ts"] = dt_util.utcnow().isoformat()
                    except Exception:
                        pass
                except Exception:
                    pass

                # Debounced save (also for stats)
                if hasattr(self, "_schedule_save_open_caps"):
                    self._schedule_save_open_caps()
        except Exception as _e:
            _LOGGER.debug(
                "better_thermostat %s: learning open caps failed for %s: %s",
                self.device_name,
                entity_id,
                _e,
            )

        # Return unchanged setpoint
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
                HVACMode.OFF not in _system_modes
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

        # Early balance precondition (simple check, full check/logging in helper)
        _balance_precheck = (
            self.cur_temp is not None
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
