"""Config flow for Better Thermostat."""

from collections import OrderedDict
from collections.abc import Iterable
import copy
import logging
from typing import Any
from collections import OrderedDict
from typing import Any
from collections.abc import Iterable

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.climate.const import (
    PRESET_ACTIVITY,
    PRESET_AWAY,
    PRESET_BOOST,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_HOME,
    PRESET_SLEEP,
    HVACMode,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv, selector
import voluptuous as vol

from . import DOMAIN  # pylint: disable=unused-import
from .adapters.delegate import load_adapter
from .utils.const import (
    CONF_CALIBRATION,
    CONF_CALIBRATION_MODE,
    CONF_CHILD_LOCK,
    CONF_COOLER,
    CONF_HEAT_AUTO_SWAPPED,
    CONF_HEATER,
    CONF_HOMEMATICIP,
    CONF_HUMIDITY,
    CONF_MODEL,
    CONF_NO_SYSTEM_MODE_OFF,
    CONF_OFF_TEMPERATURE,
    CONF_OUTDOOR_SENSOR,
    CONF_PRESETS,
    CONF_PROTECT_OVERHEATING,
    CONF_SENSOR,
    CONF_SENSOR_WINDOW,
    CONF_TARGET_TEMP_STEP,
    CONF_TOLERANCE,
    CONF_VALVE_MAINTENANCE,
    CONF_WEATHER,
    CONF_WINDOW_TIMEOUT,
    CONF_WINDOW_TIMEOUT_AFTER,
    CalibrationMode,
    CalibrationType,
)
from .utils.helpers import get_device_model, get_trv_intigration

_LOGGER = logging.getLogger(__name__)


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
            selector.SelectOptionDict(
                value=CalibrationMode.MPC_CALIBRATION, label="(AI) MPC Predictive"
            ),
            selector.SelectOptionDict(
                value=CalibrationMode.AGGRESIVE_CALIBRATION, label="Agressive"
            ),
            selector.SelectOptionDict(
                value=CalibrationMode.HEATING_POWER_CALIBRATION, label="Time Based"
            ),
            selector.SelectOptionDict(
                value=CalibrationMode.TPI_CALIBRATION, label="TPI Controller"
            ),
            selector.SelectOptionDict(
                value=CalibrationMode.PID_CALIBRATION, label="PID Controller"
            ),
            selector.SelectOptionDict(
                value=CalibrationMode.NO_CALIBRATION, label="No Calibration"
            ),
        ],
        mode=selector.SelectSelectorMode.DROPDOWN,
    )
)


PRESET_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[
            selector.SelectOptionDict(value=PRESET_ECO, label="Eco"),
            selector.SelectOptionDict(value=PRESET_AWAY, label="Away"),
            selector.SelectOptionDict(value=PRESET_BOOST, label="Boost"),
            selector.SelectOptionDict(value=PRESET_COMFORT, label="Comfort"),
            selector.SelectOptionDict(value=PRESET_HOME, label="Home"),
            selector.SelectOptionDict(value=PRESET_SLEEP, label="Sleep"),
            selector.SelectOptionDict(value=PRESET_ACTIVITY, label="Activity"),
        ],
        mode=selector.SelectSelectorMode.DROPDOWN,
        multiple=True,
        translation_key="presets",
    )
)


_USER_FIELD_DEFAULTS: dict[str, Any] = {
    CONF_OFF_TEMPERATURE: 20,
    CONF_TOLERANCE: 0.0,
    CONF_TARGET_TEMP_STEP: "0.0",
}


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1", "on"}:
            return True
        if lowered in {"false", "no", "0", "off"}:
            return False
    return bool(value)


async def _load_adapter_info(
    flow: config_entries.ConfigFlow,
    integration: str | None,
    trv_id: str | None,
    *,
    existing_adapter: Any | None = None,
) -> tuple[Any | None, dict[str, Any]]:
    adapter = existing_adapter
    info: dict[str, Any] = {}

    if integration and trv_id:
        if adapter is None:
            try:
                adapter = await load_adapter(flow, integration, trv_id)
            except (
                RuntimeError,
                ValueError,
                TypeError,
            ):  # pragma: no cover - defensive
                _LOGGER.debug("load_adapter failed", exc_info=True)

        if adapter is not None and hasattr(adapter, "get_info"):
            try:
                # type: ignore[attr-defined]
                info = await adapter.get_info(flow, trv_id)
            except (RuntimeError, ValueError, TypeError, AttributeError):
                _LOGGER.debug("adapter get_info failed", exc_info=True)

    return adapter, info


def _default_calibration_from_info(info: dict[str, Any]) -> str:
    if info.get("support_offset", False):
        return "local_calibration_based"
    if info.get("support_valve", False):
        return "direct_valve_based"
    return "target_temp_based"


def _trv_supports_auto(flow: config_entries.ConfigFlow, trv_id: str | None) -> bool:
    if not trv_id:
        return False
    trv_state = flow.hass.states.get(trv_id)
    if not trv_state or not hasattr(trv_state, "attributes"):
        return False
    hvac_modes = trv_state.attributes.get("hvac_modes") or []
    return HVACMode.AUTO in hvac_modes


def _build_advanced_fields(
    *,
    sources: Iterable[dict[str, Any] | None],
    default_calibration: str,
    homematic: bool,
    has_auto: bool,
    support_valve: bool = False,
    support_offset: bool = False,
) -> OrderedDict:
    # Migrate old balance_mode to calibration_mode
    sources_list = list(sources)
    for source in sources_list:
        if isinstance(source, dict):
            balance_mode = source.get("balance_mode")
            if balance_mode == "pid":
                # Migrate PID from balance_mode to calibration_mode
                source["calibration_mode"] = CalibrationMode.PID_CALIBRATION.value
                # Remove old balance_mode
                source.pop("balance_mode", None)
            elif balance_mode in ("heuristic", "none"):
                # For other balance modes, set calibration_mode to default if not set
                if "calibration_mode" not in source:
                    source["calibration_mode"] = CalibrationMode.MPC_CALIBRATION.value
                # Remove old balance_mode
                source.pop("balance_mode", None)

    sources = sources_list

    def get_value(key: str, fallback: Any) -> Any:
        for source in sources:
            if isinstance(source, dict) and key in source:
                return source[key]
        return fallback

    def get_bool(key: str, fallback: bool) -> bool:
        return _as_bool(get_value(key, fallback), fallback)

    # Build fields directly in the final desired order without post-reordering
    # Compute values used below
    calib_default = get_value(CONF_CALIBRATION, default_calibration)

    options = []
    if support_valve:
        options.append(
            selector.SelectOptionDict(
                value=CalibrationType.DIRECT_VALVE_BASED, label="Direct Valve Based"
            )
        )

    options.append(
        selector.SelectOptionDict(
            value=CalibrationType.TARGET_TEMP_BASED, label="Target Temperature Based"
        )
    )

    if support_offset:
        options.append(
            selector.SelectOptionDict(
                value=CalibrationType.LOCAL_BASED, label="Offset Based"
            )
        )

    calib_selector = selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=options, mode=selector.SelectSelectorMode.DROPDOWN
        )
    )
    ordered: OrderedDict = OrderedDict()

    # 1) Calibration + protection flags
    ordered[vol.Required(CONF_CALIBRATION, default=calib_default)] = calib_selector
    ordered[
        vol.Required(
            CONF_CALIBRATION_MODE,
            default=get_value(CONF_CALIBRATION_MODE, CalibrationMode.MPC_CALIBRATION),
        )
    ] = CALIBRATION_MODE_SELECTOR

    ordered[
        vol.Optional(
            CONF_PROTECT_OVERHEATING, default=get_bool(CONF_PROTECT_OVERHEATING, True)
        )
    ] = bool
    ordered[
        vol.Optional(
            CONF_NO_SYSTEM_MODE_OFF, default=get_bool(CONF_NO_SYSTEM_MODE_OFF, False)
        )
    ] = bool
    ordered[
        vol.Optional(
            CONF_HEAT_AUTO_SWAPPED, default=get_bool(CONF_HEAT_AUTO_SWAPPED, False)
        )
    ] = bool
    ordered[
        vol.Optional(
            CONF_VALVE_MAINTENANCE, default=get_bool(CONF_VALVE_MAINTENANCE, False)
        )
    ] = bool
    ordered[vol.Optional(CONF_CHILD_LOCK, default=get_bool(CONF_CHILD_LOCK, False))] = (
        bool
    )
    ordered[
        vol.Optional(CONF_HOMEMATICIP, default=get_bool(CONF_HOMEMATICIP, homematic))
    ] = bool

    return ordered


def _normalize_advanced_submission(
    data: dict[str, Any], *, default_calibration: str, homematic: bool, has_auto: bool
) -> dict[str, Any]:
    normalized: dict[str, Any] = dict(data)
    normalized[CONF_CALIBRATION] = normalized.get(CONF_CALIBRATION, default_calibration)
    normalized[CONF_CALIBRATION_MODE] = normalized.get(
        CONF_CALIBRATION_MODE, CalibrationMode.MPC_CALIBRATION
    )
    normalized[CONF_PROTECT_OVERHEATING] = _as_bool(
        normalized.get(CONF_PROTECT_OVERHEATING), False
    )
    normalized[CONF_NO_SYSTEM_MODE_OFF] = _as_bool(
        normalized.get(CONF_NO_SYSTEM_MODE_OFF), False
    )
    normalized[CONF_HEAT_AUTO_SWAPPED] = _as_bool(
        normalized.get(CONF_HEAT_AUTO_SWAPPED), False
    )
    normalized[CONF_VALVE_MAINTENANCE] = _as_bool(
        normalized.get(CONF_VALVE_MAINTENANCE), False
    )
    normalized[CONF_CHILD_LOCK] = _as_bool(normalized.get(CONF_CHILD_LOCK), False)
    normalized[CONF_HOMEMATICIP] = _as_bool(normalized.get(CONF_HOMEMATICIP), homematic)

    _LOGGER.debug("Normalized advanced submission: %s", normalized)

    return normalized


def _duration_dict_to_seconds(duration: Any | None) -> int:
    if duration is None:
        return 0
    if isinstance(duration, (int, float)):
        try:
            return max(int(duration), 0)
        except (TypeError, ValueError):
            return 0
    if isinstance(duration, dict):
        try:
            return int(cv.time_period_dict(duration).total_seconds()) or 0
        except (vol.Invalid, TypeError, ValueError):
            return 0
    return 0


def _seconds_to_duration_dict(value: Any) -> dict[str, int]:
    try:
        total = int(value or 0)
    except (TypeError, ValueError):
        total = 0
    total = max(total, 0)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return {"hours": int(hours), "minutes": int(minutes), "seconds": int(seconds)}


def _build_user_fields(
    *, mode: str, current: dict[str, Any], user_input: dict[str, Any] | None = None
) -> OrderedDict:
    user_input = user_input or {}
    is_create = mode == "create"
    fields: OrderedDict = OrderedDict()

    def resolve(key: str, fallback: Any = None) -> Any:
        if key in user_input:
            return user_input[key]
        if key in current and current[key] is not None:
            return current[key]
        if fallback is None and key in _USER_FIELD_DEFAULTS:
            return _USER_FIELD_DEFAULTS[key]
        return fallback

    def add_field(
        key: str, field_type: Any, *, required: bool = False, default: Any = None
    ) -> None:
        description = None
        use_default = default is not None

        if isinstance(field_type, selector.EntitySelector):
            if default not in (None, [], ""):
                description = {"suggested_value": default}
            use_default = False

        if required:
            if use_default:
                fields[vol.Required(key, default=default)] = field_type
            else:
                fields[
                    (
                        vol.Required(key, description=description)
                        if description
                        else vol.Required(key)
                    )
                ] = field_type
        elif use_default:
            fields[vol.Optional(key, default=default)] = field_type
        else:
            fields[
                (
                    vol.Optional(key, description=description)
                    if description
                    else vol.Optional(key)
                )
            ] = field_type

    def add_entity_selector(
        key: str,
        *,
        domain: Any,
        device_class: str | None = None,
        multiple: bool = False,
        required: bool = False,
    ) -> None:
        selector_kwargs: dict[str, Any] = {"domain": domain, "multiple": multiple}
        if device_class is not None:
            selector_kwargs["device_class"] = device_class
        selector_config = selector.EntitySelectorConfig(**selector_kwargs)
        default = resolve(key)
        if key == CONF_HEATER and isinstance(default, list):
            default = [
                item.get("trv")
                for item in default
                if isinstance(item, dict) and item.get("trv")
            ]
        if key == CONF_HEATER and not default:
            default = None
        add_field(
            key,
            selector.EntitySelector(selector_config),
            required=required,
            default=default,
        )

    add_field(CONF_NAME, str, default=resolve(CONF_NAME, ""))

    if is_create:
        add_entity_selector(CONF_HEATER, domain="climate", multiple=True, required=True)
        add_entity_selector(CONF_COOLER, domain="climate", multiple=False)

    add_entity_selector(
        CONF_SENSOR,
        domain=["sensor", "number", "input_number"],
        device_class="temperature",
        required=is_create,
    )
    add_entity_selector(
        CONF_HUMIDITY,
        domain=["sensor", "number", "input_number"],
        device_class="humidity",
    )
    add_entity_selector(
        CONF_OUTDOOR_SENSOR,
        domain=["sensor", "input_number", "number"],
        device_class="temperature",
    )
    add_entity_selector(
        CONF_SENSOR_WINDOW, domain=["group", "sensor", "input_boolean", "binary_sensor"]
    )
    add_entity_selector(CONF_WEATHER, domain="weather")

    for key in (CONF_WINDOW_TIMEOUT, CONF_WINDOW_TIMEOUT_AFTER):
        if key in user_input and user_input[key] is not None:
            duration_default = user_input[key]
        else:
            stored = resolve(key, 0 if not is_create else None)
            if isinstance(stored, dict):
                duration_default = stored
            elif stored is not None:
                duration_default = _seconds_to_duration_dict(stored)
            else:
                duration_default = None
        add_field(key, selector.DurationSelector(), default=duration_default)

    off_temp_default = resolve(
        CONF_OFF_TEMPERATURE, _USER_FIELD_DEFAULTS[CONF_OFF_TEMPERATURE]
    )
    try:
        off_temp_default = int(off_temp_default)
    except (TypeError, ValueError):
        off_temp_default = _USER_FIELD_DEFAULTS[CONF_OFF_TEMPERATURE]
    add_field(CONF_OFF_TEMPERATURE, int, default=off_temp_default)

    add_field(
        CONF_PRESETS, PRESET_SELECTOR, default=resolve(CONF_PRESETS, [PRESET_ECO])
    )

    tolerance_default = resolve(CONF_TOLERANCE, _USER_FIELD_DEFAULTS[CONF_TOLERANCE])
    try:
        tolerance_default = float(tolerance_default)
    except (TypeError, ValueError):
        tolerance_default = _USER_FIELD_DEFAULTS[CONF_TOLERANCE]
    add_field(
        CONF_TOLERANCE,
        vol.All(vol.Coerce(float), vol.Range(min=0)),
        default=tolerance_default,
    )

    target_step_default = resolve(
        CONF_TARGET_TEMP_STEP, _USER_FIELD_DEFAULTS[CONF_TARGET_TEMP_STEP]
    )
    if target_step_default is not None:
        target_step_default = str(target_step_default)
    add_field(CONF_TARGET_TEMP_STEP, TEMP_STEP_SELECTOR, default=target_step_default)

    return fields


def _normalize_user_submission(
    user_input: dict[str, Any], *, mode: str, base: dict[str, Any] | None = None
) -> dict[str, Any]:
    if base:
        if not isinstance(base, dict):
            base_dict = dict(base)
        else:
            base_dict = base
        base_copy = copy.deepcopy(base_dict)
    else:
        base_copy = {}
    normalized: dict[str, Any] = base_copy

    normalized[CONF_NAME] = user_input.get(CONF_NAME, normalized.get(CONF_NAME, ""))

    if mode == "create":
        heaters_value = user_input.get(CONF_HEATER, normalized.get(CONF_HEATER, []))
        if isinstance(heaters_value, list):
            heaters_list = heaters_value
        elif heaters_value is None:
            heaters_list = []
        else:
            heaters_list = [heaters_value]
        if heaters_list and isinstance(heaters_list[0], dict):
            heaters_list = [
                item.get("trv")
                for item in heaters_list
                if isinstance(item, dict) and item.get("trv")
            ]
        normalized[CONF_HEATER] = list(heaters_list)
        normalized[CONF_COOLER] = user_input.get(
            CONF_COOLER, normalized.get(CONF_COOLER)
        )
    else:
        normalized[CONF_HEATER] = copy.deepcopy(normalized.get(CONF_HEATER, []))
        normalized[CONF_COOLER] = normalized.get(CONF_COOLER)

    optional_keys = (
        CONF_SENSOR,
        CONF_SENSOR_WINDOW,
        CONF_HUMIDITY,
        CONF_OUTDOOR_SENSOR,
        CONF_WEATHER,
    )
    for key in optional_keys:
        if key in user_input:
            value = user_input.get(key)
            if value == "" or value is None:
                normalized[key] = None
            else:
                normalized[key] = value
        else:
            normalized[key] = None

    for key in (CONF_WINDOW_TIMEOUT, CONF_WINDOW_TIMEOUT_AFTER):
        if key in user_input:
            normalized[key] = _duration_dict_to_seconds(user_input.get(key))
        elif mode == "create" and key not in normalized:
            normalized[key] = 0

    off_temp = user_input.get(
        CONF_OFF_TEMPERATURE,
        normalized.get(
            CONF_OFF_TEMPERATURE, _USER_FIELD_DEFAULTS[CONF_OFF_TEMPERATURE]
        ),
    )
    try:
        normalized[CONF_OFF_TEMPERATURE] = int(off_temp)
    except (TypeError, ValueError):
        normalized[CONF_OFF_TEMPERATURE] = _USER_FIELD_DEFAULTS[CONF_OFF_TEMPERATURE]

    if CONF_PRESETS in user_input:
        normalized[CONF_PRESETS] = user_input[CONF_PRESETS]
    elif mode == "create" and CONF_PRESETS not in normalized:
        normalized[CONF_PRESETS] = []

    tolerance = user_input.get(
        CONF_TOLERANCE,
        normalized.get(CONF_TOLERANCE, _USER_FIELD_DEFAULTS[CONF_TOLERANCE]),
    )
    try:
        normalized[CONF_TOLERANCE] = float(tolerance)
    except (TypeError, ValueError):
        normalized[CONF_TOLERANCE] = _USER_FIELD_DEFAULTS[CONF_TOLERANCE]

    target_step = user_input.get(
        CONF_TARGET_TEMP_STEP,
        normalized.get(
            CONF_TARGET_TEMP_STEP, _USER_FIELD_DEFAULTS[CONF_TARGET_TEMP_STEP]
        ),
    )
    if target_step in (None, ""):
        target_step = _USER_FIELD_DEFAULTS[CONF_TARGET_TEMP_STEP]
    normalized[CONF_TARGET_TEMP_STEP] = str(target_step)

    return normalized


async def _prepare_advanced_context(
    flow: config_entries.ConfigFlow, trv_config: dict[str, Any] | None
) -> dict[str, Any]:
    trv_config = trv_config or {}
    integration = trv_config.get("integration")
    trv_id = trv_config.get("trv")
    adapter, info = await _load_adapter_info(
        flow, integration, trv_id, existing_adapter=trv_config.get("adapter")
    )
    default_calibration = _default_calibration_from_info(info)
    homematic = bool(integration and "homematic" in integration.lower())
    has_auto = _trv_supports_auto(flow, trv_id)

    return {
        "adapter": adapter,
        "info": info,
        "default_calibration": default_calibration,
        "homematic": homematic,
        "has_auto": has_auto,
        "integration": integration,
        "trv_id": trv_id,
    }


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
        self._active_trv_config = None
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

        # attach current trv bundle
        self.data[CONF_HEATER] = self.trv_bundle
        if user_input is not None:
            if self.data is not None:
                _LOGGER.debug("Confirm: %s", self.data[CONF_HEATER])
                unique_trv_string = "_".join([x["trv"] for x in self.data[CONF_HEATER]])
                await self.async_set_unique_id(
                    f"{self.data['name']}_{unique_trv_string}"
                )
                _LOGGER.debug(
                    "Creating entry with heater bundle: %s", self.data.get(CONF_HEATER)
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
        """Handle the advanced step of the config flow."""
        trv_cfg = _trv_config if isinstance(_trv_config, dict) else None
        if trv_cfg is None:
            trv_cfg = self._active_trv_config
        if trv_cfg is None:
            _LOGGER.debug(
                "ConfigFlow advanced step missing TRV context; returning to confirm"
            )
            return await self.async_step_confirm()

        self._active_trv_config = trv_cfg
        ctx = await _prepare_advanced_context(self, trv_cfg)
        existing_adv = trv_cfg.get("advanced") if isinstance(trv_cfg, dict) else None
        _LOGGER.debug(
            "ConfigFlow advanced step called (index=%s, trv=%s) with user_input=%s",
            self.i,
            ctx.get("trv_id"),
            user_input,
        )

        if user_input is not None:
            advanced_data = _normalize_advanced_submission(
                user_input,
                default_calibration=ctx["default_calibration"],
                homematic=ctx["homematic"],
                has_auto=ctx["has_auto"],
            )
            _LOGGER.debug(
                "ConfigFlow advanced step storing data for %s (index %s): %s",
                trv_cfg.get("trv"),
                self.i,
                advanced_data,
            )
            self.trv_bundle[self.i]["advanced"] = advanced_data
            self.trv_bundle[self.i]["adapter"] = None

            self.i += 1
            self._active_trv_config = None
            if len(self.trv_bundle) > self.i:
                _LOGGER.debug(
                    "ConfigFlow advanced step moving to next TRV index=%s", self.i
                )
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

            if not _has_off_mode:
                return await self.async_step_confirm(None, "no_off_mode")
            return await self.async_step_confirm()

        user_input = user_input or {}
        info = ctx.get("info", {})
        fields = _build_advanced_fields(
            sources=(user_input, existing_adv),
            default_calibration=ctx["default_calibration"],
            homematic=ctx["homematic"],
            has_auto=ctx["has_auto"],
            support_valve=info.get("support_valve", False),
            support_offset=info.get("support_offset", False),
        )
        _LOGGER.debug(
            "ConfigFlow advanced step showing form for trv=%s with defaults=%s",
            ctx.get("trv_id"),
            existing_adv,
        )

        return self.async_show_form(
            step_id="advanced",
            data_schema=vol.Schema(fields),
            last_step=False,
            description_placeholders={"trv": ctx.get("trv_id") or "-"},
        )

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors: dict[str, str] = {}
        current = self.data or {}

        if user_input is not None:
            _LOGGER.debug("ConfigFlow user step received input: %s", user_input)
            try:
                normalized = _normalize_user_submission(
                    user_input, mode="create", base=current
                )
            except Exception as err:
                _LOGGER.exception("ConfigFlow user step normalization failed: %s", err)
                raise
            self.data = normalized
            _LOGGER.debug("ConfigFlow user step normalized data: %s", normalized)
            if not normalized.get(CONF_NAME):
                errors["base"] = "no_name"

            heaters = normalized.get(CONF_HEATER) or []
            if "base" not in errors:
                self.heater_entity_id = list(heaters)
                self.trv_bundle = []
                for trv in self.heater_entity_id:
                    integration = await get_trv_intigration(self, trv)
                    self.trv_bundle.append(
                        {
                            "trv": trv,
                            "integration": integration,
                            "model": await get_device_model(self, trv),
                            "adapter": await load_adapter(self, integration, trv),
                        }
                    )
                _LOGGER.debug(
                    "ConfigFlow user step built trv bundle: %s", self.trv_bundle
                )
                self.data[CONF_MODEL] = "/".join([x["model"] for x in self.trv_bundle])
                return await self.async_step_advanced(None, self.trv_bundle[0])

        fields = _build_user_fields(
            mode="create", current=self.data or {}, user_input=user_input
        )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(fields),
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
        self._active_trv_config = None
        # Do not set `self.config_entry` directly; store in a private attribute
        # to avoid deprecated behavior. The framework will set `config_entry` on
        # the options flow object as needed.
        self._config_entry = config_entry
        super().__init__()

    async def async_step_init(self, _user_input=None):
        """Manage the options."""
        return await self.async_step_user()

    async def async_step_advanced(
        self, user_input=None, _trv_config=None, _update_config=None
    ):
        """Manage the advanced options."""
        trv_cfg = _trv_config if isinstance(_trv_config, dict) else None
        if trv_cfg is None:
            trv_cfg = self._active_trv_config
        if trv_cfg is None:
            _LOGGER.debug(
                "OptionsFlow advanced step missing TRV context; aborting to init"
            )
            return await self.async_step_init()

        self._active_trv_config = trv_cfg
        ctx = await _prepare_advanced_context(self, trv_cfg)
        existing_adv = trv_cfg.get("advanced") if isinstance(trv_cfg, dict) else None
        _LOGGER.debug(
            "OptionsFlow advanced step called (index=%s, trv=%s) with user_input=%s",
            self.i,
            ctx.get("trv_id"),
            user_input,
        )

        if user_input is not None:
            advanced_data = _normalize_advanced_submission(
                user_input,
                default_calibration=ctx["default_calibration"],
                homematic=ctx["homematic"],
                has_auto=ctx["has_auto"],
            )
            _LOGGER.debug(
                "OptionsFlow advanced step storing data for %s (index %s): %s",
                trv_cfg.get("trv"),
                self.i,
                advanced_data,
            )
            self.trv_bundle[self.i]["advanced"] = advanced_data
            self.trv_bundle[self.i]["adapter"] = None

            self.i += 1
            if len(self.trv_bundle) - 1 >= self.i:
                self._last_step = True

            if len(self.trv_bundle) > self.i:
                self._active_trv_config = None
                return await self.async_step_advanced(
                    None, self.trv_bundle[self.i], _update_config
                )

            self.updated_config[CONF_HEATER] = self.trv_bundle
            _LOGGER.debug("Updated config: %s", self.updated_config)
            _LOGGER.debug(
                "OptionsFlow writing heater bundle: %s",
                self.updated_config.get(CONF_HEATER),
            )
            self.hass.config_entries.async_update_entry(
                self._config_entry, data=self.updated_config
            )
            self._active_trv_config = None
            return self.async_create_entry(
                title=self.updated_config["name"], data=self.updated_config
            )

        user_input = user_input or {}
        info = ctx.get("info", {})
        fields = _build_advanced_fields(
            sources=(user_input, existing_adv),
            default_calibration=ctx["default_calibration"],
            homematic=ctx["homematic"],
            has_auto=ctx["has_auto"],
            support_valve=info.get("support_valve", False),
            support_offset=info.get("support_offset", False),
        )
        _LOGGER.debug(
            "OptionsFlow advanced step showing form for trv=%s with defaults=%s",
            ctx.get("trv_id"),
            existing_adv,
        )
        self.device_name = user_input.get(CONF_NAME, "-")

        return self.async_show_form(
            step_id="advanced",
            data_schema=vol.Schema(fields),
            last_step=self._last_step,
            description_placeholders={"trv": ctx.get("trv_id") or "-"},
        )

    async def async_step_user(self, user_input=None):
        """Handle the user step."""
        if user_input is not None:
            _LOGGER.debug("OptionsFlow user step received input: %s", user_input)
            try:
                normalized = _normalize_user_submission(
                    user_input, mode="update", base=self._config_entry.data
                )
            except Exception as err:
                _LOGGER.exception("OptionsFlow user step normalization failed: %s", err)
                raise
            _LOGGER.debug("OptionsFlow user step normalized data: %s", normalized)
            self.updated_config = normalized
            self.trv_bundle = []
            for trv in normalized.get(CONF_HEATER, []):
                trv_copy = copy.deepcopy(trv)
                trv_copy["adapter"] = None
                self.trv_bundle.append(trv_copy)
            _LOGGER.debug("OptionsFlow user step built trv bundle: %s", self.trv_bundle)

            return await self.async_step_advanced(
                None, self.trv_bundle[0], self.updated_config
            )

        fields = _build_user_fields(
            mode="update", current=self._config_entry.data, user_input=user_input
        )

        return self.async_show_form(
            step_id="user", data_schema=vol.Schema(fields), last_step=False
        )
