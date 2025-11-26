"""Config flow for Better Thermostat."""

import logging
import voluptuous as vol

from collections import OrderedDict
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.components.climate.const import HVACMode
from homeassistant.helpers import config_validation as cv


from .adapters.delegate import load_adapter

from .utils.helpers import get_device_model, get_trv_intigration

from .utils.const import (
    CONF_COOLER,
    CONF_PROTECT_OVERHEATING,
    CONF_CALIBRATION,
    CONF_CHILD_LOCK,
    CONF_HEAT_AUTO_SWAPPED,
    CONF_HEATER,
    CONF_HOMEMATICIP,
    CONF_HUMIDITY,
    CONF_MODEL,
    CONF_NO_SYSTEM_MODE_OFF,
    CONF_OFF_TEMPERATURE,
    CONF_ECO_TEMPERATURE,
    CONF_OUTDOOR_SENSOR,
    CONF_SENSOR,
    CONF_SENSOR_WINDOW,
    CONF_VALVE_MAINTENANCE,
    CONF_WEATHER,
    CONF_WINDOW_TIMEOUT,
    CONF_WINDOW_TIMEOUT_AFTER,
    CONF_CALIBRATION_MODE,
    CONF_TOLERANCE,
    CONF_TARGET_TEMP_STEP,
    CalibrationMode,
    CalibrationType,
)

from . import DOMAIN  # pylint:disable=unused-import

_LOGGER = logging.getLogger(__name__)


def _normalize_advanced(adv_cfg: dict | None, homematic: bool = False) -> dict:
    """Return a normalized advanced dict with default values for missing keys.

    This ensures we persist a consistent shape for `advanced` options across
    the initial config flow and the options flow, so missing keys don't lead
    to unexpected False defaults in runtime logic.
    """
    adv = dict(adv_cfg or {})
    # Calibration related
    adv.setdefault(CONF_CALIBRATION, "target_temp_based")
    adv.setdefault(CONF_CALIBRATION_MODE, CalibrationMode.HEATING_POWER_CALIBRATION)
    # Basic boolean options
    adv.setdefault(CONF_PROTECT_OVERHEATING, False)
    adv.setdefault(CONF_NO_SYSTEM_MODE_OFF, False)
    adv.setdefault(CONF_HEAT_AUTO_SWAPPED, False)
    adv.setdefault(CONF_VALVE_MAINTENANCE, False)
    adv.setdefault(CONF_CHILD_LOCK, False)
    adv.setdefault(CONF_HOMEMATICIP, homematic)
    # Balance / PID params
    adv.setdefault("balance_mode", "none")
    adv.setdefault("trend_mix_trv", 0.7)
    adv.setdefault("percent_hysteresis_pts", 1.0)
    adv.setdefault("min_update_interval_s", 60.0)
    adv.setdefault("pid_auto_tune", True)
    adv.setdefault("pid_kp", 50.0)
    adv.setdefault("pid_ki", 0.02)
    adv.setdefault("pid_kd", 2500.0)
    return adv


CALIBRATION_TYPE_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[
            selector.SelectOptionDict(
                value=CalibrationType.TARGET_TEMP_BASED,
                label="Target Temperature Based",
            )
        ],
        mode=selector.SelectSelectorMode.DROPDOWN,
    )
)


BALANCE_MODE_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[
            selector.SelectOptionDict(value="none", label="None / Off"),
            selector.SelectOptionDict(
                value="heuristic", label="Heuristic (Experimental)"
            ),
            selector.SelectOptionDict(value="pid", label="PID (Experimental)"),
        ],
        mode=selector.SelectSelectorMode.DROPDOWN,
    )
)

CALIBRATION_TYPE_ALL_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[
            selector.SelectOptionDict(
                value=CalibrationType.TARGET_TEMP_BASED,
                label="Target Temperature Based",
            ),
            selector.SelectOptionDict(
                value=CalibrationType.LOCAL_BASED, label="Offset Based"
            ),
            selector.SelectOptionDict(value=CalibrationType.HYBRID, label="Hybrid"),
        ],
        mode=selector.SelectSelectorMode.DROPDOWN,
    )
)

TEMP_STEP_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[
            selector.SelectOptionDict(value="0.0", label="Auto"),
            selector.SelectOptionDict(value="0.1", label="0.1 °C"),
            selector.SelectOptionDict(value="0.2", label="0.2 °C"),
            selector.SelectOptionDict(value="0.25", label="0.25 °C"),
            selector.SelectOptionDict(value="0.5", label="0.5 °C"),
            selector.SelectOptionDict(value="1.0", label="1 °C"),
        ],
        mode=selector.SelectSelectorMode.DROPDOWN,
    )
)

CALIBRATION_MODE_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[
            selector.SelectOptionDict(value=CalibrationMode.DEFAULT, label="Normal"),
            selector.SelectOptionDict(
                value=CalibrationMode.AGGRESIVE_CALIBRATION, label="Agressive"
            ),
            selector.SelectOptionDict(
                value=CalibrationMode.HEATING_POWER_CALIBRATION, label="AI Time Based"
            ),
            selector.SelectOptionDict(
                value=CalibrationMode.NO_CALIBRATION, label="No Calibration"
            ),
        ],
        mode=selector.SelectSelectorMode.DROPDOWN,
    )
)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Better Thermostat."""

    VERSION = 7
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    def __init__(self):
        """Initialize the config flow."""
        self.device_name = ""
        self.data = None
        self.model = None
        self.heater_entity_id = None
        self.trv_bundle = []
        self.integration = None
        self.i = 0
        super().__init__()

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Get the options flow for this handler."""
        return OptionsFlowHandler(config_entry)

    # Added to satisfy abstract base in newer HA versions
    # type: ignore[override]
    def is_matching(self, other_flow: config_entries.ConfigFlow) -> bool:
        """Return True if this flow matches an existing config flow (reconfigure)."""
        if (
            getattr(self, "unique_id", None)
            and getattr(other_flow, "unique_id", None) == self.unique_id
        ):
            return True
        return False

    async def async_step_confirm(self, user_input=None, confirm_type=None):
        """Handle user-confirmation of discovered node."""
        errors = {}
        if not self.data:
            errors["base"] = "no_data"
            return self.async_show_form(step_id="confirm", errors=errors)

        # attach current trv bundle (normalize advanced options for safety)
        for trv in self.trv_bundle:
            trv_integration = trv.get("integration", "") or ""
            trv_homematic = trv_integration.find("homematic") != -1
            trv["advanced"] = _normalize_advanced(
                trv.get("advanced", {}), trv_homematic
            )
            # Remove runtime-only objects (e.g. adapter instances) before
            # persisting the configuration to Home Assistant storage to avoid
            # storing non-JSON serializable values like modules.
            if "adapter" in trv:
                trv["adapter"] = None
        self.data[CONF_HEATER] = self.trv_bundle
        if user_input is not None:
            if self.data is not None:
                _LOGGER.debug("Confirm: %s", self.data[CONF_HEATER])
                unique_trv_string = "_".join([x["trv"] for x in self.data[CONF_HEATER]])
                await self.async_set_unique_id(
                    f"{self.data['name']}_{unique_trv_string}"
                )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=self.data["name"], data=self.data)
        if confirm_type is not None:
            errors["base"] = confirm_type
        _trv_list = self.data.get(CONF_HEATER) or []
        _trvs = ",".join([x.get("trv", "?") for x in _trv_list])
        return self.async_show_form(
            step_id="confirm",
            errors=errors,
            description_placeholders={"name": self.data[CONF_NAME], "trv": _trvs},
        )

    async def async_step_advanced(self, user_input=None, _trv_config=None):
        """Handle options flow."""
        if _trv_config is None:
            # Should not happen in normal flow, fallback to confirm
            return await self.async_step_confirm()

        # Determine whether this TRV integration is homematic so the default for
        # `CONF_HOMEMATICIP` can be set correctly when normalizing advanced
        # options. Evaluate before processing `user_input` to avoid undefined
        # variable usage.
        homematic = False
        integration_name = (
            _trv_config.get("integration") if isinstance(_trv_config, dict) else None
        )
        if integration_name and integration_name.find("homematic") != -1:
            homematic = True

        if user_input is not None:
            # Ensure advanced options contain all expected keys. Some values are
            # optional in the UI and may be omitted from `user_input`; ensure a
            # consistent shape so the advanced dict is usable at runtime.
            adv = dict(user_input)
            adv = _normalize_advanced(adv, homematic)
            self.trv_bundle[self.i]["advanced"] = adv
            self.trv_bundle[self.i]["adapter"] = None

            self.i += 1
            if len(self.trv_bundle) > self.i:
                return await self.async_step_advanced(None, self.trv_bundle[self.i])

            _has_off_mode = True
            for trv in self.trv_bundle:
                entity_id = trv.get("trv")
                state_obj = self.hass.states.get(entity_id) if entity_id else None
                hvac_modes = []
                if state_obj and hasattr(state_obj, "attributes"):
                    hvac_modes = state_obj.attributes.get("hvac_modes", []) or []
                if HVACMode.OFF not in hvac_modes:
                    _has_off_mode = False

            if _has_off_mode is False:
                return await self.async_step_confirm(None, "no_off_mode")
            return await self.async_step_confirm()

        user_input = user_input or {}
        homematic = False
        integration_name = (
            _trv_config.get("integration") if isinstance(_trv_config, dict) else None
        )
        if integration_name and integration_name.find("homematic") != -1:
            homematic = True
        adv_cfg = _normalize_advanced(_trv_config.get("advanced") or {}, homematic)

        fields = OrderedDict()

        _default_calibration = "target_temp_based"
        _adapter = _trv_config.get("adapter", None)
        _info = {}
        if _adapter is not None:
            try:
                # type: ignore[attr-defined]
                _info = await _adapter.get_info(self, _trv_config.get("trv"))
            except (
                AttributeError,
                RuntimeError,
                ValueError,
                TypeError,
            ):  # pragma: no cover - defensive
                _LOGGER.debug("Adapter get_info failed", exc_info=True)
            if _info.get("support_offset", False):
                _default_calibration = "local_calibration_based"

        if _default_calibration == "local_calibration_based":
            fields[
                vol.Required(
                    CONF_CALIBRATION,
                    default=user_input.get(CONF_CALIBRATION, _default_calibration),
                )
            ] = CALIBRATION_TYPE_ALL_SELECTOR
        else:
            fields[
                vol.Required(
                    CONF_CALIBRATION,
                    default=user_input.get(CONF_CALIBRATION, _default_calibration),
                )
            ] = CALIBRATION_TYPE_SELECTOR

        fields[
            vol.Required(
                CONF_CALIBRATION_MODE,
                default=user_input.get(
                    CONF_CALIBRATION_MODE, CalibrationMode.HEATING_POWER_CALIBRATION
                ),
            )
        ] = CALIBRATION_MODE_SELECTOR

        fields[
            vol.Optional(
                CONF_PROTECT_OVERHEATING,
                default=user_input.get(
                    CONF_PROTECT_OVERHEATING, adv_cfg.get(CONF_PROTECT_OVERHEATING)
                ),
            )
        ] = bool

        fields[
            vol.Optional(
                CONF_NO_SYSTEM_MODE_OFF,
                default=user_input.get(
                    CONF_NO_SYSTEM_MODE_OFF, adv_cfg.get(CONF_NO_SYSTEM_MODE_OFF)
                ),
            )
        ] = bool

        fields[
            vol.Optional(
                CONF_HEAT_AUTO_SWAPPED,
                default=user_input.get(
                    CONF_HEAT_AUTO_SWAPPED, adv_cfg.get(CONF_HEAT_AUTO_SWAPPED)
                ),
            )
        ] = bool

        # Valve maintenance: always offer; if no native valve control is available, the runtime logic uses a fallback via setpoint extremes
        fields[
            vol.Optional(
                CONF_VALVE_MAINTENANCE,
                default=user_input.get(
                    CONF_VALVE_MAINTENANCE, adv_cfg.get(CONF_VALVE_MAINTENANCE)
                ),
            )
        ] = bool

        fields[
            vol.Optional(
                CONF_CHILD_LOCK,
                default=user_input.get(CONF_CHILD_LOCK, adv_cfg.get(CONF_CHILD_LOCK)),
            )
        ] = bool
        fields[
            vol.Optional(
                CONF_HOMEMATICIP,
                default=user_input.get(
                    CONF_HOMEMATICIP, adv_cfg.get(CONF_HOMEMATICIP, homematic)
                ),
            )
        ] = bool

        # Balance/control (PID) – after HomematicIP; fields dependent on mode
        mode_current = str(user_input.get("balance_mode", "none")).lower()
        fields[vol.Optional("balance_mode", default=mode_current)] = (
            BALANCE_MODE_SELECTOR
        )
        # General balance parameters only show if mode is not 'none'
        fields[
            vol.Optional(
                "trend_mix_trv",
                default=user_input.get(
                    "trend_mix_trv", adv_cfg.get("trend_mix_trv", 0.7)
                ),
            )
        ] = vol.All(vol.Coerce(float), vol.Range(min=0, max=1))
        fields[
            vol.Optional(
                "percent_hysteresis_pts",
                default=user_input.get(
                    "percent_hysteresis_pts", adv_cfg.get("percent_hysteresis_pts", 1.0)
                ),
            )
        ] = vol.All(vol.Coerce(float), vol.Range(min=0, max=10))
        fields[
            vol.Optional(
                "min_update_interval_s",
                default=user_input.get(
                    "min_update_interval_s", adv_cfg.get("min_update_interval_s", 60.0)
                ),
            )
        ] = vol.All(vol.Coerce(float), vol.Range(min=0, max=3600))
        # Only show PID parameters if explicitly chosen
        fields[
            vol.Optional(
                "pid_auto_tune",
                default=user_input.get(
                    "pid_auto_tune", adv_cfg.get("pid_auto_tune", True)
                ),
            )
        ] = bool
        fields[
            vol.Optional(
                "pid_kp", default=user_input.get("pid_kp", adv_cfg.get("pid_kp", 50.0))
            )
        ] = vol.All(vol.Coerce(float), vol.Range(min=0))
        fields[
            vol.Optional(
                "pid_ki", default=user_input.get("pid_ki", adv_cfg.get("pid_ki", 0.02))
            )
        ] = vol.All(vol.Coerce(float), vol.Range(min=0))
        fields[
            vol.Optional(
                "pid_kd",
                default=user_input.get("pid_kd", adv_cfg.get("pid_kd", 2500.0)),
            )
        ] = vol.All(vol.Coerce(float), vol.Range(min=0))

        return self.async_show_form(
            step_id="advanced",
            data_schema=vol.Schema(fields),
            last_step=False,
            description_placeholders={"trv": _trv_config.get("trv", "-")},
        )

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            if self.data is None:
                self.data = user_input
            self.heater_entity_id = self.data[CONF_HEATER]
            if self.data[CONF_NAME] == "":
                errors["base"] = "no_name"
            if CONF_SENSOR_WINDOW not in self.data:
                self.data[CONF_SENSOR_WINDOW] = None
            if CONF_HUMIDITY not in self.data:
                self.data[CONF_HUMIDITY] = None
            if CONF_OUTDOOR_SENSOR not in self.data:
                self.data[CONF_OUTDOOR_SENSOR] = None
            if CONF_WEATHER not in self.data:
                self.data[CONF_WEATHER] = None
            if CONF_COOLER not in self.data:
                self.data[CONF_COOLER] = None

            if CONF_WINDOW_TIMEOUT in self.data:
                self.data[CONF_WINDOW_TIMEOUT] = (
                    int(
                        cv.time_period_dict(
                            user_input.get(CONF_WINDOW_TIMEOUT, None)
                        ).total_seconds()
                    )
                    or 0
                )
            else:
                self.data[CONF_WINDOW_TIMEOUT] = 0

            if CONF_WINDOW_TIMEOUT_AFTER in self.data:
                self.data[CONF_WINDOW_TIMEOUT_AFTER] = (
                    int(
                        cv.time_period_dict(
                            user_input.get(CONF_WINDOW_TIMEOUT_AFTER, None)
                        ).total_seconds()
                    )
                    or 0
                )
            else:
                self.data[CONF_WINDOW_TIMEOUT_AFTER] = 0

            if "base" not in errors:
                for trv in self.heater_entity_id:
                    _intigration = await get_trv_intigration(self, trv)
                    self.trv_bundle.append(
                        {
                            "trv": trv,
                            "integration": _intigration,
                            "model": await get_device_model(self, trv),
                            "adapter": await load_adapter(self, _intigration, trv),
                        }
                    )
                self.data[CONF_MODEL] = "/".join([x["model"] for x in self.trv_bundle])
                return await self.async_step_advanced(None, self.trv_bundle[0])

        user_input = user_input or {}

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_NAME, default=user_input.get(CONF_NAME, "")): str,
                    vol.Required(CONF_HEATER): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="climate", multiple=True)
                    ),
                    vol.Optional(CONF_COOLER): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="climate", multiple=False)
                    ),
                    vol.Required(CONF_SENSOR): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain=["sensor", "number", "input_number"],
                            device_class="temperature",
                            multiple=False,
                        )
                    ),
                    vol.Optional(CONF_HUMIDITY): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain=["sensor", "number", "input_number"],
                            device_class="humidity",
                            multiple=False,
                        )
                    ),
                    vol.Optional(CONF_OUTDOOR_SENSOR): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain=["sensor", "input_number", "number"],
                            device_class="temperature",
                            multiple=False,
                        )
                    ),
                    vol.Optional(CONF_SENSOR_WINDOW): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain=[
                                "group",
                                "sensor",
                                "input_boolean",
                                "binary_sensor",
                            ],
                            multiple=False,
                        )
                    ),
                    vol.Optional(CONF_WEATHER): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="weather", multiple=False)
                    ),
                    vol.Optional(CONF_WINDOW_TIMEOUT): selector.DurationSelector(),
                    vol.Optional(
                        CONF_WINDOW_TIMEOUT_AFTER
                    ): selector.DurationSelector(),
                    vol.Optional(
                        CONF_OFF_TEMPERATURE,
                        default=user_input.get(CONF_OFF_TEMPERATURE, 20),
                    ): int,
                    vol.Optional(
                        CONF_ECO_TEMPERATURE,
                        default=user_input.get(CONF_ECO_TEMPERATURE, 18.0),
                    ): vol.All(vol.Coerce(float), vol.Range(min=5, max=35)),
                    vol.Optional(
                        CONF_TOLERANCE, default=user_input.get(CONF_TOLERANCE, 0.0)
                    ): vol.All(vol.Coerce(float), vol.Range(min=0)),
                    vol.Optional(
                        CONF_TARGET_TEMP_STEP,
                        default=str(user_input.get(CONF_TARGET_TEMP_STEP, "0.0")),
                    ): TEMP_STEP_SELECTOR,
                }
            ),
            errors=errors,
            last_step=False,
        )


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle a option flow for a config entry."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self.i = 0
        self.trv_bundle = []
        self.device_name = ""
        self._last_step = False
        self.updated_config = {}
        # Do NOT explicitly assign `self.config_entry` here; Home Assistant will
        # set it for the options flow automatically. Explicitly setting it is
        # deprecated and will stop working in HA 2025.12.
        # Store a fallback reference for older Home Assistant versions that
        # expect the handler to know the config entry (avoid assigning to
        # `self.config_entry` to prevent deprecation warnings).
        self._passed_config_entry = config_entry
        super().__init__()

    @property
    def current_config_entry(self) -> config_entries.ConfigEntry:
        """Return the config entry set by Home Assistant or the passed one.

        Home Assistant will set the attribute `self.config_entry` on the
        options flow instance in newer versions. For backwards compatibility
        we fall back to the config entry passed to the constructor if the
        attribute isn't set yet.
        """
        return getattr(
            self, "config_entry", getattr(self, "_passed_config_entry", None)
        )

    async def async_step_init(self, _user_input=None):
        """Manage the options."""
        return await self.async_step_user()

    async def async_step_advanced(
        self, user_input=None, _trv_config=None, _update_config=None
    ):
        """Manage the advanced options."""
        # Evaluate homematic before using it in normalization, just like in the
        # main config flow handler. The value depends on the current trv
        # configuration being edited and should be available regardless of
        # whether we're handling user input or simply building the form.
        homematic = False
        integration_name = (
            _trv_config.get("integration") if isinstance(_trv_config, dict) else None
        )
        if integration_name and integration_name.find("homematic") != -1:
            homematic = True

        if user_input is not None:
            adv = dict(user_input)
            # For Options flow, preserve missing options by filling defaults
            adv = _normalize_advanced(adv, homematic)
            self.trv_bundle[self.i]["advanced"] = adv
            self.trv_bundle[self.i]["adapter"] = None

            self.i += 1
            if len(self.trv_bundle) - 1 >= self.i:
                self._last_step = True

            if len(self.trv_bundle) > self.i:
                return await self.async_step_advanced(
                    None, self.trv_bundle[self.i], _update_config
                )

            self.updated_config[CONF_HEATER] = self.trv_bundle
            _LOGGER.debug("Updated config: %s", self.updated_config)
            self.hass.config_entries.async_update_entry(
                self.current_config_entry, data=self.updated_config
            )
            return self.async_create_entry(
                title=self.updated_config["name"], data=self.updated_config
            )

        user_input = user_input or {}
        # Ensure dict shape
        if not isinstance(_trv_config, dict):
            _trv_config = {}
        adv_cfg = _normalize_advanced(_trv_config.get("advanced") or {}, homematic)
        integration_name = _trv_config.get("integration") or ""
        trv_id = _trv_config.get("trv")

        homematic = False
        if integration_name.find("homematic") != -1:
            homematic = True

        fields = OrderedDict()

        _default_calibration = "target_temp_based"
        self.device_name = user_input.get(CONF_NAME, "-")

        _adapter = None
        _info = {}
        if integration_name and trv_id:
            try:
                _adapter = await load_adapter(self, integration_name, trv_id)
            except (
                RuntimeError,
                ValueError,
                TypeError,
            ):  # pragma: no cover - defensive
                _LOGGER.debug("load_adapter failed", exc_info=True)
        if _adapter is not None and trv_id and hasattr(_adapter, "get_info"):
            try:
                # type: ignore[attr-defined]
                _info = await _adapter.get_info(self, trv_id)
            except (
                RuntimeError,
                ValueError,
                TypeError,
                AttributeError,
            ):  # pragma: no cover
                _LOGGER.debug("adapter get_info failed", exc_info=True)
            if _info.get("support_offset", False):
                _default_calibration = "local_calibration_based"

        calib_default = user_input.get(
            CONF_CALIBRATION, adv_cfg.get(CONF_CALIBRATION, _default_calibration)
        )
        if _default_calibration == "local_calibration_based":
            fields[vol.Required(CONF_CALIBRATION, default=calib_default)] = (
                CALIBRATION_TYPE_ALL_SELECTOR
            )
        else:
            fields[vol.Required(CONF_CALIBRATION, default=calib_default)] = (
                CALIBRATION_TYPE_SELECTOR
            )

        fields[
            vol.Required(
                CONF_CALIBRATION_MODE,
                default=adv_cfg.get(
                    CONF_CALIBRATION_MODE, CalibrationMode.HEATING_POWER_CALIBRATION
                ),
            )
        ] = CALIBRATION_MODE_SELECTOR

        fields[
            vol.Optional(
                CONF_PROTECT_OVERHEATING,
                default=adv_cfg.get(CONF_PROTECT_OVERHEATING, False),
            )
        ] = bool

        fields[
            vol.Optional(
                CONF_NO_SYSTEM_MODE_OFF,
                default=adv_cfg.get(CONF_NO_SYSTEM_MODE_OFF, False),
            )
        ] = bool

        # Do not automatically enable heat/auto swap just because a device
        # reports 'auto' as a supported HVAC mode. The option should default
        # to False unless explicitly enabled by the user or preserved in the
        # saved config for existing entries.
        fields[
            vol.Optional(
                CONF_HEAT_AUTO_SWAPPED,
                default=adv_cfg.get(CONF_HEAT_AUTO_SWAPPED, False),
            )
        ] = bool

        # Ventilwartung: immer anbieten; wenn keine native Ventilsteuerung vorhanden ist,
        # nutzt die Laufzeitlogik ein Fallback über Setpoint-Extrema
        fields[
            vol.Optional(
                CONF_VALVE_MAINTENANCE,
                default=adv_cfg.get(CONF_VALVE_MAINTENANCE, False),
            )
        ] = bool

        fields[
            vol.Optional(CONF_CHILD_LOCK, default=adv_cfg.get(CONF_CHILD_LOCK, False))
        ] = bool
        fields[
            vol.Optional(
                CONF_HOMEMATICIP, default=adv_cfg.get(CONF_HOMEMATICIP, homematic)
            )
        ] = bool

        # Balance/Regelung (PID) – nach HomematicIP; Felder abhängig vom Modus
        mode_current = str(adv_cfg.get("balance_mode", "none")).lower()
        fields[vol.Optional("balance_mode", default=mode_current)] = (
            BALANCE_MODE_SELECTOR
        )
        # Allgemeine Balance-Parameter nur anzeigen, wenn Modus nicht 'none'

        fields[
            vol.Optional("trend_mix_trv", default=adv_cfg.get("trend_mix_trv", 0.7))
        ] = vol.All(vol.Coerce(float), vol.Range(min=0, max=1))
        fields[
            vol.Optional(
                "percent_hysteresis_pts",
                default=adv_cfg.get("percent_hysteresis_pts", 1.0),
            )
        ] = vol.All(vol.Coerce(float), vol.Range(min=0, max=10))
        fields[
            vol.Optional(
                "min_update_interval_s",
                default=adv_cfg.get("min_update_interval_s", 60.0),
            )
        ] = vol.All(vol.Coerce(float), vol.Range(min=0, max=3600))
        # PID-Parameter nur anzeigen, wenn explizit gewählt
        fields[
            vol.Optional("pid_auto_tune", default=adv_cfg.get("pid_auto_tune", True))
        ] = bool
        fields[vol.Optional("pid_kp", default=adv_cfg.get("pid_kp", 60.0))] = vol.All(
            vol.Coerce(float), vol.Range(min=0)
        )
        fields[vol.Optional("pid_ki", default=adv_cfg.get("pid_ki", 0.01))] = vol.All(
            vol.Coerce(float), vol.Range(min=0)
        )
        fields[vol.Optional("pid_kd", default=adv_cfg.get("pid_kd", 2000.0))] = vol.All(
            vol.Coerce(float), vol.Range(min=0)
        )

        return self.async_show_form(
            step_id="advanced",
            data_schema=vol.Schema(fields),
            last_step=self._last_step,
            description_placeholders={"trv": trv_id or "-"},
        )

    async def async_step_user(self, user_input=None):
        """Handle the user step."""
        if user_input is not None:
            current_config = self.current_config_entry.data
            self.updated_config = dict(current_config)
            self.updated_config[CONF_SENSOR] = user_input.get(CONF_SENSOR, None)
            self.updated_config[CONF_SENSOR_WINDOW] = user_input.get(
                CONF_SENSOR_WINDOW, None
            )
            self.updated_config[CONF_HUMIDITY] = user_input.get(CONF_HUMIDITY, None)
            self.updated_config[CONF_OUTDOOR_SENSOR] = user_input.get(
                CONF_OUTDOOR_SENSOR, None
            )
            self.updated_config[CONF_WEATHER] = user_input.get(CONF_WEATHER, None)

            if CONF_WINDOW_TIMEOUT in self.updated_config:
                self.updated_config[CONF_WINDOW_TIMEOUT] = (
                    int(
                        cv.time_period_dict(
                            user_input.get(CONF_WINDOW_TIMEOUT, None)
                        ).total_seconds()
                    )
                    or 0
                )
            else:
                self.updated_config[CONF_WINDOW_TIMEOUT] = 0

            if CONF_WINDOW_TIMEOUT_AFTER in self.updated_config:
                self.updated_config[CONF_WINDOW_TIMEOUT_AFTER] = (
                    int(
                        cv.time_period_dict(
                            user_input.get(CONF_WINDOW_TIMEOUT_AFTER, None)
                        ).total_seconds()
                    )
                    or 0
                )
            else:
                self.updated_config[CONF_WINDOW_TIMEOUT_AFTER] = 0

            self.updated_config[CONF_OFF_TEMPERATURE] = user_input.get(
                CONF_OFF_TEMPERATURE
            )
            self.updated_config[CONF_ECO_TEMPERATURE] = float(
                user_input.get(CONF_ECO_TEMPERATURE, 18.0)
            )

            self.updated_config[CONF_TOLERANCE] = float(
                user_input.get(CONF_TOLERANCE, 0.0)
            )
            self.updated_config[CONF_TARGET_TEMP_STEP] = float(
                user_input.get(CONF_TARGET_TEMP_STEP, "0.0")
            )

            for trv in self.updated_config[CONF_HEATER]:
                trv["adapter"] = None
                self.trv_bundle.append(trv)

            return await self.async_step_advanced(
                None, self.trv_bundle[0], self.updated_config
            )

        fields = OrderedDict()

        fields[
            vol.Optional(
                CONF_SENSOR,
                description={
                    "suggested_value": self.current_config_entry.data.get(
                        CONF_SENSOR, ""
                    )
                },
            )
        ] = selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=["sensor", "number", "input_number"],
                device_class="temperature",
                multiple=False,
            )
        )

        fields[
            vol.Optional(
                CONF_HUMIDITY,
                description={
                    "suggested_value": self.current_config_entry.data.get(
                        CONF_HUMIDITY, ""
                    )
                },
            )
        ] = selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=["sensor", "number", "input_number"],
                device_class="humidity",
                multiple=False,
            )
        )

        fields[
            vol.Optional(
                CONF_SENSOR_WINDOW,
                description={
                    "suggested_value": self.current_config_entry.data.get(
                        CONF_SENSOR_WINDOW, ""
                    )
                },
            )
        ] = selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=["group", "sensor", "input_boolean", "binary_sensor"],
                multiple=False,
            )
        )

        fields[
            vol.Optional(
                CONF_OUTDOOR_SENSOR,
                description={
                    "suggested_value": self.current_config_entry.data.get(
                        CONF_OUTDOOR_SENSOR, ""
                    )
                },
            )
        ] = selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=["sensor", "input_number", "number"],
                device_class="temperature",
                multiple=False,
            )
        )

        fields[
            vol.Optional(
                CONF_WEATHER,
                description={
                    "suggested_value": self.current_config_entry.data.get(
                        CONF_WEATHER, ""
                    )
                },
            )
        ] = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="weather", multiple=False)
        )

        _timeout = self.current_config_entry.data.get(CONF_WINDOW_TIMEOUT, 0)
        _timeout = str(cv.time_period_seconds(_timeout))
        _timeout = {
            "hours": int(_timeout.split(":", maxsplit=1)[0]),
            "minutes": int(_timeout.split(":")[1]),
            "seconds": int(_timeout.split(":")[2]),
        }
        fields[
            vol.Optional(
                CONF_WINDOW_TIMEOUT,
                default=_timeout,
                description={"suggested_value": _timeout},
            )
        ] = selector.DurationSelector()

        _timeout = self.current_config_entry.data.get(CONF_WINDOW_TIMEOUT_AFTER, 0)
        _timeout = str(cv.time_period_seconds(_timeout))
        _timeout = {
            "hours": int(_timeout.split(":", maxsplit=1)[0]),
            "minutes": int(_timeout.split(":")[1]),
            "seconds": int(_timeout.split(":")[2]),
        }
        fields[
            vol.Optional(
                CONF_WINDOW_TIMEOUT_AFTER,
                default=_timeout,
                description={"suggested_value": _timeout},
            )
        ] = selector.DurationSelector()

        fields[
            vol.Optional(
                CONF_OFF_TEMPERATURE,
                default=self.current_config_entry.data.get(CONF_OFF_TEMPERATURE, 5),
            )
        ] = int

        fields[
            vol.Optional(
                CONF_ECO_TEMPERATURE,
                default=self.current_config_entry.data.get(CONF_ECO_TEMPERATURE, 18.0),
            )
        ] = vol.All(vol.Coerce(float), vol.Range(min=5, max=35))

        fields[
            vol.Optional(
                CONF_TOLERANCE,
                default=self.current_config_entry.data.get(CONF_TOLERANCE, 0.0),
            )
        ] = vol.All(vol.Coerce(float), vol.Range(min=0))
        fields[
            vol.Optional(
                CONF_TARGET_TEMP_STEP,
                default=str(
                    self.current_config_entry.data.get(CONF_TARGET_TEMP_STEP, 0.0)
                ),
            )
        ] = TEMP_STEP_SELECTOR

        return self.async_show_form(
            step_id="user", data_schema=vol.Schema(fields), last_step=False
        )
