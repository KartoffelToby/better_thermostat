"""Config flow for Better Thermostat."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Mapping
import copy
from dataclasses import dataclass
import logging
from typing import Final, NotRequired, TypedDict, override

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
from homeassistant.const import CONF_NAME, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    config_validation as cv,
    entity_registry as er,
    selector,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util.unit_conversion import TemperatureConverter
import voluptuous as vol

from . import DOMAIN, other_entries_controlling, trv_entity_ids
from .adapters.delegate import load_adapter
from .adapters.types import TrvAdapter
from .model_fixes.model_quirks import load_model_quirks, quirk_writes_valve
from .utils.advanced_flags import as_bool as _as_bool
from .utils.const import (
    CONF_CALIBRATION,
    CONF_CALIBRATION_MODE,
    CONF_CHILD_LOCK,
    CONF_COOLER,
    CONF_DOOR_OFF_DELAY,
    CONF_DOOR_OFF_DELAY_AFTER,
    CONF_DOOR_SENSORS,
    CONF_HEAT_AUTO_SWAPPED,
    CONF_HOMEMATICIP,
    CONF_HUMIDITY_SENSOR,
    CONF_MODEL,
    CONF_MPC_V2_PLANT_PRESET,
    CONF_NO_OFF_SYSTEM_MODE,
    CONF_OFF_TEMPERATURE,
    CONF_OUTDOOR_SENSOR,
    CONF_PRESETS,
    CONF_PROTECT_OVERHEATING,
    CONF_TARGET_TEMP_MAX,
    CONF_TARGET_TEMP_MIN,
    CONF_TARGET_TEMP_STEP,
    CONF_TEMPERATURE_SENSOR,
    CONF_THERMOSTAT,
    CONF_TOLERANCE,
    CONF_VALVE_MAINTENANCE,
    CONF_WEATHER,
    CONF_WINDOW_OFF_DELAY,
    CONF_WINDOW_OFF_DELAY_AFTER,
    CONF_WINDOW_SENSORS,
    DEFAULT_CALIBRATION_MODE,
    TARGET_TEMP_BOUND_AUTO,
    CalibrationMode,
    CalibrationOutput,
    MpcV2PlantPreset,
)
from .utils.entry_schema import StoredChoice, target_temperature_bound
from .utils.helpers import (
    configured_calibration_mode,
    device_offers_mode,
    entry_name,
    entry_settings,
    get_device_model,
    get_trv_intigration,
)
from .utils.preset_manager import DEFAULT_ENABLED_PRESETS
from .utils.stored_values import is_json_object

_LOGGER = logging.getLogger(__name__)

CONFIG_WALKTHROUGH_URL = (
    "https://better-thermostat.org/setup/configuration-walkthrough/"
)


# The dropdown offers whole degrees Celsius. Bounds are stored as strings, the
# way the target temperature step is, so one reader covers both.
_TARGET_TEMP_MIN_MAX_SELECTOR_TO_VALUE = {"auto": TARGET_TEMP_BOUND_AUTO} | {
    f"min_max_{degree}": f"{float(degree)}" for degree in range(41)
}
_TARGET_TEMP_MIN_MAX_VALUE_TO_SELECTOR = {
    value: key for key, value in _TARGET_TEMP_MIN_MAX_SELECTOR_TO_VALUE.items()
}


def _resolve_min_max_selector_token(value: object) -> str:
    """Return the selector token for a stored bound or for a submitted token.

    Parameters
    ----------
    value :
            a stored bound such as ``"25.0"`` or a selector token such as
            ``"min_max_25"``

    Returns
    -------
    str
            the matching selector token, or ``"auto"`` when nothing matches
    """
    key = str(value)
    if key in _TARGET_TEMP_MIN_MAX_SELECTOR_TO_VALUE:
        return key
    return _TARGET_TEMP_MIN_MAX_VALUE_TO_SELECTOR.get(key, "auto")


TEMP_MIN_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        # Stable selector tokens keep labels translatable without changing stored values.
        options=list(_TARGET_TEMP_MIN_MAX_SELECTOR_TO_VALUE),
        mode=selector.SelectSelectorMode.DROPDOWN,
        translation_key="target_temp_min",
    )
)

TEMP_MAX_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        # Stable selector tokens keep labels translatable without changing stored values.
        options=list(_TARGET_TEMP_MIN_MAX_SELECTOR_TO_VALUE),
        mode=selector.SelectSelectorMode.DROPDOWN,
        translation_key="target_temp_max",
    )
)

_TARGET_TEMP_STEP_SELECTOR_TO_VALUE = {
    "auto_legacy": "0.0",
    "auto": "",
    "step_0_1": "0.1",
    "step_0_2": "0.2",
    "step_0_25": "0.25",
    "step_0_5": "0.5",
    "step_1_0": "1.0",
}
_TARGET_TEMP_STEP_VALUE_TO_SELECTOR = {
    value: key for key, value in _TARGET_TEMP_STEP_SELECTOR_TO_VALUE.items()
}

TEMP_STEP_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        # Stable selector tokens keep labels translatable without changing stored values.
        options=list(_TARGET_TEMP_STEP_SELECTOR_TO_VALUE),
        mode=selector.SelectSelectorMode.DROPDOWN,
        translation_key="target_temp_step",
    )
)


CALIBRATION_MODE_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[
            CalibrationMode.HEATING_POWER_CALIBRATION,
            CalibrationMode.DEFAULT,
            CalibrationMode.MPC_CALIBRATION,
            CalibrationMode.MPC_V2_CALIBRATION,
            CalibrationMode.AGGRESSIVE_CALIBRATION,
            CalibrationMode.TPI_CALIBRATION,
            CalibrationMode.PID_CALIBRATION,
            CalibrationMode.NO_CALIBRATION,
        ],
        mode=selector.SelectSelectorMode.DROPDOWN,
        translation_key="calibration_mode",
    )
)


MPC_V2_PLANT_PRESET_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[
            MpcV2PlantPreset.AUTO,
            MpcV2PlantPreset.SMALL_ROOM,
            MpcV2PlantPreset.MEDIUM_ROOM,
            MpcV2PlantPreset.LARGE_ROOM,
        ],
        mode=selector.SelectSelectorMode.DROPDOWN,
        translation_key="mpc_v2_plant_preset",
    )
)


PRESET_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[
            PRESET_ECO,
            PRESET_AWAY,
            PRESET_BOOST,
            PRESET_COMFORT,
            PRESET_HOME,
            PRESET_SLEEP,
            PRESET_ACTIVITY,
        ],
        mode=selector.SelectSelectorMode.DROPDOWN,
        multiple=True,
        translation_key="presets",
    )
)


# The outdoor threshold is entered and stored in the system unit, so the
# suggestion is this temperature expressed in that unit.
_OFF_TEMPERATURE_DEFAULT_CELSIUS = 20

_UserFieldDefaults = TypedDict(  # noqa: UP013
    "_UserFieldDefaults",
    {
        "tolerance": float,
        "target_temp_min": str,
        "target_temp_max": str,
        "target_temp_step": str,
    },
)

_USER_FIELD_DEFAULTS: Final[_UserFieldDefaults] = {
    CONF_TOLERANCE: 0.0,
    CONF_TARGET_TEMP_MIN: TARGET_TEMP_BOUND_AUTO,
    CONF_TARGET_TEMP_MAX: TARGET_TEMP_BOUND_AUTO,
    CONF_TARGET_TEMP_STEP: "0.0",
}

# The user step's settings as the flow normalises a submission. The
# thermostats are entity ids here; the stored entry holds a bundle per
# thermostat in their place. A delay or the preset list the submission does
# not carry is left out, and the stored value stands. The keys are spelled
# as strings for the reason ``utils/entry_schema.py`` gives.
_UserDraft = TypedDict(  # noqa: UP013
    "_UserDraft",
    {
        "name": str,
        "thermostat": list[str],
        "cooler": str | None,
        "temperature_sensor": str | None,
        "window_sensors": str | None,
        "door_sensors": str | None,
        "humidity_sensor": str | None,
        "outdoor_sensor": str | None,
        "weather": str | None,
        "window_off_delay": NotRequired[int],
        "window_off_delay_after": NotRequired[int],
        "door_off_delay": NotRequired[int],
        "door_off_delay_after": NotRequired[int],
        "off_temperature": int,
        "presets": NotRequired[list[str] | None],
        "tolerance": float,
        "target_temp_min": str,
        "target_temp_max": str,
        "target_temp_step": str,
    },
)

# One thermostat's advanced options as the flow normalises a submission. The
# choices are kept as submitted; the plant preset is left out when the
# submission does not carry it.
_AdvancedDraft = TypedDict(  # noqa: UP013
    "_AdvancedDraft",
    {
        "calibration": StoredChoice,
        "calibration_mode": StoredChoice,
        "mpc_v2_plant_preset": NotRequired[StoredChoice],
        "protect_overheating": bool,
        "no_off_system_mode": bool,
        "heat_auto_swapped": bool,
        "valve_maintenance": bool,
        "child_lock": bool,
        "homematicip": bool,
    },
)


@dataclass(slots=True)
class _TrvDraft:
    """One thermostat while a flow configures it.

    ``stored`` is the thermostat the way the entry stores it: for one the
    entry already has, a copy of its stored mapping with every key it carries,
    known or not, in the stored order. Its ``adapter`` slot is always stored
    as ``None``; the loaded module lives in ``adapter`` for as long as the
    flow runs. ``advanced`` is what the thermostat's advanced step submitted,
    and replaces the stored advanced options once it is set.
    """

    entity_id: str
    integration: str | None
    adapter: TrvAdapter | None
    stored: dict[str, object]
    advanced: _AdvancedDraft | None = None

    def stored_advanced(self) -> Mapping[str, object] | None:
        """Return the advanced options the entry holds for this thermostat."""
        advanced = self.stored.get("advanced")
        return advanced if is_json_object(advanced) else None

    def to_stored(self) -> dict[str, object]:
        """Return the thermostat in the shape the entry stores."""
        stored = dict(self.stored)
        stored["adapter"] = None
        if self.advanced is not None:
            stored["advanced"] = _stored_advanced(self.advanced)
        return stored


@dataclass(frozen=True, slots=True)
class _AdvancedContext:
    """What the advanced step of one thermostat offers and defaults to."""

    entity_id: str
    info: Mapping[str, bool]
    default_calibration: str
    homematic: bool
    has_auto: bool


def _int_or(value: object, fallback: int) -> int:
    """Return ``int(value)``, or ``fallback`` when ``int()`` refuses the value."""
    if not isinstance(value, int | float | str):
        return fallback
    try:
        return int(value)
    except ValueError:
        return fallback


def _float_or(value: object, fallback: float) -> float:
    """Return ``float(value)``, or ``fallback`` when ``float()`` refuses it."""
    if not isinstance(value, int | float | str):
        return fallback
    try:
        return float(value)
    except ValueError:
        return fallback


def _submitted_choice(value: object, fallback: StoredChoice) -> StoredChoice:
    """Return a submitted choice, or ``fallback`` when it is not a scalar."""
    if value is None or isinstance(value, str | int | float):
        return value
    return fallback


def _entity_or_none(value: object) -> str | None:
    """Return a submitted entity id, with an emptied selector read as none."""
    return value if isinstance(value, str) and value else None


def _merged_settings(
    base: Mapping[str, object], draft: _UserDraft
) -> dict[str, object]:
    """Return ``base`` with the user step's settings written over it.

    The keys ``base`` holds keep their place and the others follow in the
    order the user step lists them, so a key ``draft`` leaves out keeps its
    stored value.
    """
    merged = copy.deepcopy(dict(base))
    merged[CONF_NAME] = draft[CONF_NAME]
    merged[CONF_THERMOSTAT] = list(draft[CONF_THERMOSTAT])
    merged[CONF_COOLER] = draft[CONF_COOLER]
    merged[CONF_TEMPERATURE_SENSOR] = draft[CONF_TEMPERATURE_SENSOR]
    merged[CONF_WINDOW_SENSORS] = draft[CONF_WINDOW_SENSORS]
    merged[CONF_DOOR_SENSORS] = draft[CONF_DOOR_SENSORS]
    merged[CONF_HUMIDITY_SENSOR] = draft[CONF_HUMIDITY_SENSOR]
    merged[CONF_OUTDOOR_SENSOR] = draft[CONF_OUTDOOR_SENSOR]
    merged[CONF_WEATHER] = draft[CONF_WEATHER]
    if CONF_WINDOW_OFF_DELAY in draft:
        merged[CONF_WINDOW_OFF_DELAY] = draft[CONF_WINDOW_OFF_DELAY]
    if CONF_WINDOW_OFF_DELAY_AFTER in draft:
        merged[CONF_WINDOW_OFF_DELAY_AFTER] = draft[CONF_WINDOW_OFF_DELAY_AFTER]
    if CONF_DOOR_OFF_DELAY in draft:
        merged[CONF_DOOR_OFF_DELAY] = draft[CONF_DOOR_OFF_DELAY]
    if CONF_DOOR_OFF_DELAY_AFTER in draft:
        merged[CONF_DOOR_OFF_DELAY_AFTER] = draft[CONF_DOOR_OFF_DELAY_AFTER]
    merged[CONF_OFF_TEMPERATURE] = draft[CONF_OFF_TEMPERATURE]
    if CONF_PRESETS in draft:
        merged[CONF_PRESETS] = draft[CONF_PRESETS]
    merged[CONF_TOLERANCE] = draft[CONF_TOLERANCE]
    merged[CONF_TARGET_TEMP_MIN] = draft[CONF_TARGET_TEMP_MIN]
    merged[CONF_TARGET_TEMP_MAX] = draft[CONF_TARGET_TEMP_MAX]
    merged[CONF_TARGET_TEMP_STEP] = draft[CONF_TARGET_TEMP_STEP]
    return merged


def _stored_advanced(draft: _AdvancedDraft) -> dict[str, object]:
    """Return one thermostat's advanced options in the shape the entry stores."""
    stored: dict[str, object] = {
        CONF_CALIBRATION: draft[CONF_CALIBRATION],
        CONF_CALIBRATION_MODE: draft[CONF_CALIBRATION_MODE],
    }
    if CONF_MPC_V2_PLANT_PRESET in draft:
        stored[CONF_MPC_V2_PLANT_PRESET] = draft[CONF_MPC_V2_PLANT_PRESET]
    stored[CONF_PROTECT_OVERHEATING] = draft[CONF_PROTECT_OVERHEATING]
    stored[CONF_NO_OFF_SYSTEM_MODE] = draft[CONF_NO_OFF_SYSTEM_MODE]
    stored[CONF_HEAT_AUTO_SWAPPED] = draft[CONF_HEAT_AUTO_SWAPPED]
    stored[CONF_VALVE_MAINTENANCE] = draft[CONF_VALVE_MAINTENANCE]
    stored[CONF_CHILD_LOCK] = draft[CONF_CHILD_LOCK]
    stored[CONF_HOMEMATICIP] = draft[CONF_HOMEMATICIP]
    return stored


def _stored_settings(
    settings: Mapping[str, object], thermostats: Iterable[_TrvDraft]
) -> dict[str, object]:
    """Return the settings a flow writes, with each thermostat as stored."""
    stored = dict(settings)
    stored[CONF_THERMOSTAT] = [trv.to_stored() for trv in thermostats]
    return stored


def _off_temperature_default(system_unit: str | None) -> int:
    """Return the suggested outdoor threshold in the system unit, in whole degrees."""
    if system_unit == UnitOfTemperature.FAHRENHEIT:
        return round(
            TemperatureConverter.convert(
                _OFF_TEMPERATURE_DEFAULT_CELSIUS,
                UnitOfTemperature.CELSIUS,
                UnitOfTemperature.FAHRENHEIT,
            )
        )
    return _OFF_TEMPERATURE_DEFAULT_CELSIUS


async def _quirk_valve_support(
    flow: ConfigFlow | OptionsFlowHandler, entity_id: str
) -> bool:
    """Answer whether this TRV's model quirk drives its valve.

    The model comes from the device registry and its quirk is loaded the way
    the running thermostat loads it, so the calibration strategies the flow
    offers are the ones the write path can carry out.

    Parameters
    ----------
    flow : ConfigFlow | OptionsFlowHandler
        The flow the probe runs in, supplying Home Assistant access and the
        instance name the model lookup logs against.
    entity_id : str
        Entity ID of the TRV to probe.

    Returns
    -------
    bool
        True when the TRV's model has a quirk of its own that writes the
        valve, False when it has none and when the model cannot be read.
    """
    try:
        model = await get_device_model(flow, entity_id)
        quirks = await load_model_quirks(flow, model, entity_id)
    except RuntimeError, ValueError, TypeError, AttributeError, ImportError:
        _LOGGER.debug("model quirk probe failed", exc_info=True)
        return False
    return quirk_writes_valve(quirks)


async def _load_adapter_info(
    flow: ConfigFlow | OptionsFlowHandler,
    integration: str | None,
    entity_id: str | None,
    *,
    existing_adapter: TrvAdapter | None = None,
) -> tuple[TrvAdapter | None, dict[str, bool]]:
    adapter = existing_adapter
    info: dict[str, bool] = {}

    if integration and entity_id:
        if adapter is None:
            try:
                adapter = await load_adapter(flow, integration, entity_id)
            except RuntimeError, ValueError, TypeError:
                _LOGGER.debug("load_adapter failed", exc_info=True)

        if adapter is not None:
            try:
                info = await adapter.get_info(flow, entity_id)
            except RuntimeError, ValueError, TypeError, AttributeError:
                _LOGGER.debug("adapter get_info failed", exc_info=True)

    # A model quirk owns the valve of the devices it covers, whichever adapter
    # serves the ecosystem they are paired through, so the adapter's answer is
    # not the whole of the valve surface.
    if (
        entity_id
        and not info.get("support_valve", False)
        and await _quirk_valve_support(flow, entity_id)
    ):
        info = info | {"support_valve": True}

    return adapter, info


def _default_calibration_from_info(info: Mapping[str, bool]) -> str:
    if info.get("support_offset", False):
        return "local_calibration_based"
    if info.get("support_valve", False):
        return "direct_valve_based"
    return "target_temp_based"


def _trv_supports_auto(
    flow: config_entries.ConfigFlow | config_entries.OptionsFlow, entity_id: str | None
) -> bool:
    if not entity_id:
        return False
    trv_state = flow.hass.states.get(entity_id)
    if not trv_state:
        return False
    hvac_modes = trv_state.attributes.get("hvac_modes") or []
    return device_offers_mode(hvac_modes, HVACMode.AUTO)


def _without_balance_mode(source: Mapping[str, object]) -> dict[str, object]:
    """Return ``source`` with its ``balance_mode`` read as a calibration mode.

    An older entry can carry the ``balance_mode`` that ``calibration_mode``
    replaced. ``pid`` reads as the PID calibration mode; ``heuristic`` and
    ``none`` leave the calibration mode as chosen, or on the default when
    none is. ``source`` itself is left as it is.
    """
    view = dict(source)
    balance_mode = view.get("balance_mode")
    if balance_mode == "pid":
        view[CONF_CALIBRATION_MODE] = CalibrationMode.PID_CALIBRATION.value
        view.pop("balance_mode", None)
    elif balance_mode in ("heuristic", "none"):
        if CONF_CALIBRATION_MODE not in view:
            view[CONF_CALIBRATION_MODE] = DEFAULT_CALIBRATION_MODE.value
        view.pop("balance_mode", None)
    return view


def _build_advanced_fields(
    *,
    sources: Iterable[Mapping[str, object] | None],
    default_calibration: str,
    homematic: bool,
    has_auto: bool,
    support_valve: bool = False,
    support_offset: bool = False,
) -> OrderedDict[vol.Marker, object]:
    views = [_without_balance_mode(source) for source in sources if source is not None]

    def get_value(key: str, fallback: object) -> object:
        """Get value from first source dict that contains the key."""
        for view in views:
            if key in view:
                return view[key]
        return fallback

    def get_bool(key: str, fallback: bool) -> bool:
        """Get boolean value from sources, converting string representations."""
        return _as_bool(get_value(key, fallback), fallback)

    # Build fields directly in the final desired order without post-reordering
    # Compute values used below
    calib_default = get_value(CONF_CALIBRATION, default_calibration)

    options = []
    if support_valve:
        options.append(CalibrationOutput.DIRECT_VALVE_BASED)

    options.append(CalibrationOutput.TARGET_TEMP_BASED)

    if support_offset:
        options.append(CalibrationOutput.LOCAL_BASED)

    calib_selector = selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=options,
            mode=selector.SelectSelectorMode.DROPDOWN,
            translation_key="calibration_output",
        )
    )
    ordered: OrderedDict[vol.Marker, object] = OrderedDict()

    # 1) Calibration + protection flags
    ordered[vol.Required(CONF_CALIBRATION, default=calib_default)] = calib_selector
    ordered[
        vol.Required(
            CONF_CALIBRATION_MODE,
            default=get_value(CONF_CALIBRATION_MODE, DEFAULT_CALIBRATION_MODE),
        )
    ] = CALIBRATION_MODE_SELECTOR

    ordered[
        vol.Optional(
            CONF_MPC_V2_PLANT_PRESET,
            default=get_value(CONF_MPC_V2_PLANT_PRESET, MpcV2PlantPreset.AUTO),
        )
    ] = MPC_V2_PLANT_PRESET_SELECTOR

    ordered[
        vol.Optional(
            CONF_PROTECT_OVERHEATING, default=get_bool(CONF_PROTECT_OVERHEATING, True)
        )
    ] = bool
    ordered[
        vol.Optional(
            CONF_NO_OFF_SYSTEM_MODE, default=get_bool(CONF_NO_OFF_SYSTEM_MODE, False)
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
    data: Mapping[str, object],
    *,
    default_calibration: str,
    homematic: bool,
    has_auto: bool,
) -> _AdvancedDraft:
    normalized: _AdvancedDraft = {
        CONF_CALIBRATION: _submitted_choice(
            data.get(CONF_CALIBRATION, default_calibration), default_calibration
        ),
        CONF_CALIBRATION_MODE: _submitted_choice(
            data.get(CONF_CALIBRATION_MODE, DEFAULT_CALIBRATION_MODE),
            DEFAULT_CALIBRATION_MODE,
        ),
        CONF_PROTECT_OVERHEATING: _as_bool(data.get(CONF_PROTECT_OVERHEATING), False),
        CONF_NO_OFF_SYSTEM_MODE: _as_bool(data.get(CONF_NO_OFF_SYSTEM_MODE), False),
        CONF_HEAT_AUTO_SWAPPED: _as_bool(data.get(CONF_HEAT_AUTO_SWAPPED), False),
        CONF_VALVE_MAINTENANCE: _as_bool(data.get(CONF_VALVE_MAINTENANCE), False),
        CONF_CHILD_LOCK: _as_bool(data.get(CONF_CHILD_LOCK), False),
        CONF_HOMEMATICIP: _as_bool(data.get(CONF_HOMEMATICIP), homematic),
    }
    if CONF_MPC_V2_PLANT_PRESET in data:
        normalized[CONF_MPC_V2_PLANT_PRESET] = _submitted_choice(
            data[CONF_MPC_V2_PLANT_PRESET], MpcV2PlantPreset.AUTO
        )

    _LOGGER.debug("Normalized advanced submission: %s", normalized)

    return normalized


def _duration_dict_to_seconds(duration: object) -> int:
    if duration is None:
        return 0
    if isinstance(duration, (int, float)):
        try:
            return max(int(duration), 0)
        except OverflowError, ValueError:
            return 0
    if is_json_object(duration):
        try:
            return int(cv.time_period_dict(duration).total_seconds()) or 0
        except vol.Invalid, TypeError, ValueError:
            return 0
    return 0


def _seconds_to_duration_dict(value: object) -> dict[str, int]:
    total = _int_or(value or 0, 0)
    total = max(total, 0)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return {"hours": hours, "minutes": minutes, "seconds": seconds}


def _build_user_fields(
    *,
    mode: str,
    current: Mapping[str, object],
    user_input: Mapping[str, object] | None = None,
    system_unit: str | None = None,
) -> OrderedDict[vol.Marker, object]:
    user_input = user_input or {}
    is_create = mode == "create"
    fields: OrderedDict[vol.Marker, object] = OrderedDict()

    def resolve(key: str, fallback: object = None) -> object:
        """Resolve field value from user input, current config, or defaults."""
        if key in user_input:
            return user_input[key]
        if key in current and current[key] is not None:
            return current[key]
        return fallback

    def add_field(
        key: str, field_type: object, *, required: bool = False, default: object = None
    ) -> None:
        """Add a field to the form schema with appropriate validation."""
        description = None
        use_default = default is not None

        if isinstance(field_type, selector.EntitySelector):
            if default not in (None, [], ""):
                description = {"suggested_value": default}
            use_default = False

        # Only entity selectors are required, and they carry their default as
        # a suggested value, never as a schema default.
        if required:
            fields[
                vol.Required(key, description=description)
                if description
                else vol.Required(key)
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
        domain: str | list[str],
        device_class: str | None = None,
        multiple: bool = False,
        required: bool = False,
    ) -> None:
        """Add an entity selector field with domain and device class filtering."""
        if device_class is not None:
            selector_config = selector.EntitySelectorConfig(
                domain=domain, multiple=multiple, device_class=device_class
            )
        else:
            selector_config = selector.EntitySelectorConfig(
                domain=domain, multiple=multiple
            )
        default = resolve(key)
        if key == CONF_THERMOSTAT and isinstance(default, list):
            # The stored entry holds a bundle per thermostat, while a form
            # rebuilt after a validation error carries the plain entity ids the
            # user submitted. Both have to survive into the selector, or the
            # redisplayed form loses the thermostats the user had picked.
            default = [
                entity_id
                for item in default
                if (entity_id := item.get("trv") if is_json_object(item) else item)
            ]
        if key == CONF_THERMOSTAT and not default:
            default = None
        add_field(
            key,
            selector.EntitySelector(selector_config),
            required=required,
            default=default,
        )

    add_field(CONF_NAME, str, default=resolve(CONF_NAME, ""))

    add_entity_selector(CONF_THERMOSTAT, domain="climate", multiple=True, required=True)
    add_entity_selector(CONF_COOLER, domain="climate", multiple=False)

    add_entity_selector(
        CONF_TEMPERATURE_SENSOR,
        domain=["sensor", "number", "input_number"],
        device_class="temperature",
        required=is_create,
    )
    add_entity_selector(
        CONF_HUMIDITY_SENSOR,
        domain=["sensor", "number", "input_number"],
        device_class="humidity",
    )
    add_entity_selector(
        CONF_OUTDOOR_SENSOR,
        domain=["sensor", "input_number", "number"],
        device_class="temperature",
    )
    add_entity_selector(
        CONF_WINDOW_SENSORS,
        domain=["group", "sensor", "input_boolean", "binary_sensor"],
    )
    add_entity_selector(
        CONF_DOOR_SENSORS, domain=["group", "sensor", "input_boolean", "binary_sensor"]
    )
    add_entity_selector(CONF_WEATHER, domain="weather")

    for key in (
        CONF_WINDOW_OFF_DELAY,
        CONF_WINDOW_OFF_DELAY_AFTER,
        CONF_DOOR_OFF_DELAY,
        CONF_DOOR_OFF_DELAY_AFTER,
    ):
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

    suggested_off_temperature = _off_temperature_default(system_unit)
    off_temperature_default = _int_or(
        resolve(CONF_OFF_TEMPERATURE, suggested_off_temperature),
        suggested_off_temperature,
    )
    add_field(CONF_OFF_TEMPERATURE, int, default=off_temperature_default)

    # An entry that carries no preset list runs on the PresetManager default
    # set, so that is the set the update form offers. The create form suggests a
    # single preset instead: a new entry has no enabled presets to preserve.
    add_field(
        CONF_PRESETS,
        PRESET_SELECTOR,
        default=resolve(
            CONF_PRESETS, [PRESET_ECO] if is_create else list(DEFAULT_ENABLED_PRESETS)
        ),
    )

    tolerance_fallback = _USER_FIELD_DEFAULTS[CONF_TOLERANCE]
    tolerance_default = _float_or(
        resolve(CONF_TOLERANCE, tolerance_fallback), tolerance_fallback
    )
    add_field(
        CONF_TOLERANCE,
        vol.All(vol.Coerce(float), vol.Range(min=0)),
        default=tolerance_default,
    )

    for bound_key, bound_selector in (
        (CONF_TARGET_TEMP_MIN, TEMP_MIN_SELECTOR),
        (CONF_TARGET_TEMP_MAX, TEMP_MAX_SELECTOR),
    ):
        bound_default = _resolve_min_max_selector_token(
            resolve(bound_key, _USER_FIELD_DEFAULTS[bound_key])
        )
        add_field(bound_key, bound_selector, default=bound_default)

    target_step_key = str(
        resolve(CONF_TARGET_TEMP_STEP, _USER_FIELD_DEFAULTS[CONF_TARGET_TEMP_STEP])
    )
    if target_step_key in _TARGET_TEMP_STEP_SELECTOR_TO_VALUE:
        # A re-displayed form carries the submitted selector token, not a stored value.
        target_step_default = target_step_key
    else:
        target_step_default = _TARGET_TEMP_STEP_VALUE_TO_SELECTOR.get(
            target_step_key, "auto_legacy"
        )
    add_field(CONF_TARGET_TEMP_STEP, TEMP_STEP_SELECTOR, default=target_step_default)

    return fields


def _submitted_thermostats(value: object) -> list[str]:
    """Return the entity ids of the thermostats a submission or an entry names.

    A submission names them as entity ids, a stored entry as one bundle per
    thermostat, and a single thermostat can stand on its own.
    """
    if isinstance(value, list):
        items: list[object] = list(value)
    elif value is None:
        items = []
    else:
        items = [value]
    if items and is_json_object(items[0]):
        return [
            entity_id
            for item in items
            if is_json_object(item)
            and isinstance(entity_id := item.get("trv"), str)
            and entity_id
        ]
    return [item for item in items if isinstance(item, str)]


def _submitted_presets(value: object) -> list[str] | None:
    """Return a submitted preset list, with ``None`` for no list."""
    if isinstance(value, list):
        return [preset for preset in value if isinstance(preset, str)]
    return None


def _submitted_selector_value(
    value: object, selector_to_value: Mapping[str, str], fallback: str
) -> str:
    """Return the stored form of a selector token, a stored value or a default.

    A selector token maps to its value. Anything else is kept as it is, with
    a missing value or an empty one that is not a token taking ``fallback``.
    """
    token = str(value)
    if token in selector_to_value:
        return selector_to_value[token]
    if value is None or value == "":
        return fallback
    return token


def _normalize_user_submission(
    user_input: Mapping[str, object],
    *,
    mode: str,
    base: Mapping[str, object] | None = None,
    errors: dict[str, str] | None = None,
    system_unit: str | None = None,
) -> _UserDraft:
    """Return the user step's settings from a submission on top of ``base``.

    ``base`` is what the step started from: the entry's settings, or the
    settings of a submission the step sent back. The returned draft holds the
    keys the step sets; ``_merged_settings`` writes them over ``base``.
    """
    base = base or {}

    def submitted(key: str, fallback: object) -> object:
        return user_input.get(key, base.get(key, fallback))

    name = submitted(CONF_NAME, "")
    suggested_off_temperature = _off_temperature_default(system_unit)
    off_temperature = submitted(CONF_OFF_TEMPERATURE, suggested_off_temperature)
    tolerance_fallback = _USER_FIELD_DEFAULTS[CONF_TOLERANCE]

    normalized: _UserDraft = {
        CONF_NAME: name if isinstance(name, str) else "",
        CONF_THERMOSTAT: _submitted_thermostats(submitted(CONF_THERMOSTAT, [])),
        CONF_COOLER: _entity_or_none(user_input.get(CONF_COOLER)),
        CONF_TEMPERATURE_SENSOR: _entity_or_none(
            user_input.get(CONF_TEMPERATURE_SENSOR)
        ),
        CONF_WINDOW_SENSORS: _entity_or_none(user_input.get(CONF_WINDOW_SENSORS)),
        CONF_DOOR_SENSORS: _entity_or_none(user_input.get(CONF_DOOR_SENSORS)),
        CONF_HUMIDITY_SENSOR: _entity_or_none(user_input.get(CONF_HUMIDITY_SENSOR)),
        CONF_OUTDOOR_SENSOR: _entity_or_none(user_input.get(CONF_OUTDOOR_SENSOR)),
        CONF_WEATHER: _entity_or_none(user_input.get(CONF_WEATHER)),
        CONF_OFF_TEMPERATURE: _int_or(off_temperature, suggested_off_temperature),
        CONF_TOLERANCE: _float_or(
            submitted(CONF_TOLERANCE, tolerance_fallback), tolerance_fallback
        ),
        CONF_TARGET_TEMP_MIN: _submitted_selector_value(
            submitted(CONF_TARGET_TEMP_MIN, _USER_FIELD_DEFAULTS[CONF_TARGET_TEMP_MIN]),
            _TARGET_TEMP_MIN_MAX_SELECTOR_TO_VALUE,
            _USER_FIELD_DEFAULTS[CONF_TARGET_TEMP_MIN],
        ),
        CONF_TARGET_TEMP_MAX: _submitted_selector_value(
            submitted(CONF_TARGET_TEMP_MAX, _USER_FIELD_DEFAULTS[CONF_TARGET_TEMP_MAX]),
            _TARGET_TEMP_MIN_MAX_SELECTOR_TO_VALUE,
            _USER_FIELD_DEFAULTS[CONF_TARGET_TEMP_MAX],
        ),
        CONF_TARGET_TEMP_STEP: _submitted_selector_value(
            submitted(
                CONF_TARGET_TEMP_STEP, _USER_FIELD_DEFAULTS[CONF_TARGET_TEMP_STEP]
            ),
            _TARGET_TEMP_STEP_SELECTOR_TO_VALUE,
            _USER_FIELD_DEFAULTS[CONF_TARGET_TEMP_STEP],
        ),
    }

    is_create = mode == "create"
    if CONF_WINDOW_OFF_DELAY in user_input:
        normalized[CONF_WINDOW_OFF_DELAY] = _duration_dict_to_seconds(
            user_input[CONF_WINDOW_OFF_DELAY]
        )
    elif is_create and CONF_WINDOW_OFF_DELAY not in base:
        normalized[CONF_WINDOW_OFF_DELAY] = 0
    if CONF_WINDOW_OFF_DELAY_AFTER in user_input:
        normalized[CONF_WINDOW_OFF_DELAY_AFTER] = _duration_dict_to_seconds(
            user_input[CONF_WINDOW_OFF_DELAY_AFTER]
        )
    elif is_create and CONF_WINDOW_OFF_DELAY_AFTER not in base:
        normalized[CONF_WINDOW_OFF_DELAY_AFTER] = 0
    if CONF_DOOR_OFF_DELAY in user_input:
        normalized[CONF_DOOR_OFF_DELAY] = _duration_dict_to_seconds(
            user_input[CONF_DOOR_OFF_DELAY]
        )
    elif is_create and CONF_DOOR_OFF_DELAY not in base:
        normalized[CONF_DOOR_OFF_DELAY] = 0
    if CONF_DOOR_OFF_DELAY_AFTER in user_input:
        normalized[CONF_DOOR_OFF_DELAY_AFTER] = _duration_dict_to_seconds(
            user_input[CONF_DOOR_OFF_DELAY_AFTER]
        )
    elif is_create and CONF_DOOR_OFF_DELAY_AFTER not in base:
        normalized[CONF_DOOR_OFF_DELAY_AFTER] = 0

    if CONF_PRESETS in user_input:
        normalized[CONF_PRESETS] = _submitted_presets(user_input[CONF_PRESETS])
    elif is_create and CONF_PRESETS not in base:
        normalized[CONF_PRESETS] = []

    if errors is not None:
        try:
            lower_bound = target_temperature_bound(normalized[CONF_TARGET_TEMP_MIN])
            upper_bound = target_temperature_bound(normalized[CONF_TARGET_TEMP_MAX])
        except ValueError:
            pass
        else:
            # A bound left on auto imposes no limit, and an entry whose two
            # bounds are equal pins the setpoint to a single value on purpose.
            if (
                lower_bound is not None
                and upper_bound is not None
                and lower_bound > upper_bound
            ):
                errors[CONF_TARGET_TEMP_MIN] = "target_temp_min_above_max"

    return normalized


async def _prepare_advanced_context(
    flow: ConfigFlow | OptionsFlowHandler, trv: _TrvDraft
) -> _AdvancedContext:
    _adapter, info = await _load_adapter_info(
        flow, trv.integration, trv.entity_id, existing_adapter=trv.adapter
    )
    return _AdvancedContext(
        entity_id=trv.entity_id,
        info=info,
        default_calibration=_default_calibration_from_info(info),
        homematic=bool(trv.integration and "homematic" in trv.integration.lower()),
        has_auto=_trv_supports_auto(flow, trv.entity_id),
    )


async def _new_trv_draft(
    flow: ConfigFlow | OptionsFlowHandler, entity_id: str
) -> tuple[_TrvDraft, str]:
    """Return the draft of a thermostat the entry does not have yet, and its model."""
    integration = await get_trv_intigration(flow, entity_id)
    model = await get_device_model(flow, entity_id)
    adapter = await load_adapter(flow, integration, entity_id)
    draft = _TrvDraft(
        entity_id=entity_id,
        integration=integration,
        adapter=adapter,
        stored={
            "trv": entity_id,
            "integration": integration,
            "model": model,
            "adapter": None,
        },
    )
    return draft, model


def _stored_trv_draft(entity_id: str, stored: Mapping[str, object]) -> _TrvDraft:
    """Return the draft of a thermostat the entry has, from its stored mapping."""
    copied = copy.deepcopy(dict(stored))
    copied["adapter"] = None
    integration = copied.get("integration")
    return _TrvDraft(
        entity_id=entity_id,
        integration=integration if isinstance(integration, str) else None,
        adapter=None,
        stored=copied,
    )


def _in_use_placeholders(
    hass: HomeAssistant,
    trv_entity_ids_to_check: Iterable[str],
    entry_id: str | None,
    kept: Iterable[str] = (),
) -> dict[str, str] | None:
    """Name the first thermostat another entry controls, and that entry.

    Thermostats in ``kept`` are skipped. ``None`` means every thermostat is
    free for the entry ``entry_id`` (``None`` for an entry not yet created).
    """
    kept_ids = set(kept)
    for trv_entity_id in trv_entity_ids_to_check:
        if trv_entity_id in kept_ids:
            continue
        owners = other_entries_controlling(hass, trv_entity_id, entry_id)
        if owners:
            return {"trv": trv_entity_id, "entry": entry_name(owners[0])}
    return None


def _unknown_placeholders(
    hass: HomeAssistant,
    trv_entity_ids_to_check: Iterable[str],
    kept: Iterable[str] = (),
) -> dict[str, str] | None:
    """Name the first thermostat Home Assistant knows nothing about.

    A thermostat is known once it has a state or an entity registry entry; one
    that is registered but not reporting yet, as on a boot, is known. The
    thermostats in ``kept`` are skipped: an entry keeps a thermostat that went
    away, so its other settings can still be saved. ``None`` means every
    thermostat is known.
    """
    registry = er.async_get(hass)
    kept_ids = set(kept)
    for trv_entity_id in trv_entity_ids_to_check:
        if trv_entity_id in kept_ids:
            continue
        if hass.states.get(trv_entity_id) is None and (
            registry.async_get(trv_entity_id) is None
        ):
            return {"trv": trv_entity_id}
    return None


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Better Thermostat."""

    VERSION = 18
    # Minor version 2 keeps the settings in the entry's options. 1.9 shares
    # the major version, so it still loads such an entry, and from 1.9.3 on it
    # reads the options as well.
    MINOR_VERSION = 2

    def __init__(self) -> None:
        """Initialize the config flow."""
        self.device_name = ""
        # The user step's settings, with the thermostats as entity ids.
        self.data: dict[str, object] | None = None
        self._user_draft: _UserDraft | None = None
        self.trv_bundle: list[_TrvDraft] = []
        self.i = 0
        self._active_trv: _TrvDraft | None = None
        super().__init__()

    @staticmethod
    @callback
    @override
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Get the options flow for this handler."""
        return OptionsFlowHandler(config_entry)

    async def async_step_confirm(
        self,
        user_input: dict[str, object] | None = None,
        confirm_type: str | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Handle user-confirmation of discovered node."""
        errors: dict[str, str] = {}
        # The user step sets the settings before any step that leads here.
        assert self.data is not None and self._user_draft is not None
        name = self._user_draft[CONF_NAME]
        entity_ids = [trv.entity_id for trv in self.trv_bundle]
        if user_input is not None:
            options = _stored_settings(self.data, self.trv_bundle)
            _LOGGER.debug("Confirm: %s", options[CONF_THERMOSTAT])
            unique_trv_string = "_".join(entity_ids)
            await self.async_set_unique_id(f"{name}_{unique_trv_string}")
            _LOGGER.debug(
                "Creating entry with heater bundle: %s", options[CONF_THERMOSTAT]
            )
            self._abort_if_unique_id_configured()
            # Another flow can have created an entry for one of these
            # thermostats while this one waited on its forms, so the
            # check runs again with nothing awaited before the entry.
            in_use = _in_use_placeholders(self.hass, entity_ids, None)
            if in_use:
                return self.async_abort(
                    reason="trv_in_use", description_placeholders=in_use
                )
            return self.async_create_entry(title=name, data={}, options=options)
        if confirm_type is not None:
            errors["base"] = confirm_type
        return self.async_show_form(
            step_id="confirm",
            errors=errors,
            description_placeholders={"name": name, "trv": ",".join(entity_ids)},
        )

    async def async_step_advanced(
        self, user_input: dict[str, object] | None = None, trv: _TrvDraft | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the advanced step of the config flow."""
        if trv is None:
            trv = self._active_trv
        # The step is entered with a TRV and submitted for the one it showed.
        assert trv is not None

        self._active_trv = trv
        ctx = await _prepare_advanced_context(self, trv)
        existing_adv = trv.stored_advanced()
        _LOGGER.debug(
            "ConfigFlow advanced step called (index=%s, trv=%s) with user_input=%s",
            self.i,
            ctx.entity_id,
            user_input,
        )

        if user_input is not None:
            advanced_data = _normalize_advanced_submission(
                user_input,
                default_calibration=ctx.default_calibration,
                homematic=ctx.homematic,
                has_auto=ctx.has_auto,
            )
            _LOGGER.debug(
                "ConfigFlow advanced step storing data for %s (index %s): %s",
                trv.entity_id,
                self.i,
                advanced_data,
            )
            self.trv_bundle[self.i].advanced = advanced_data
            self.trv_bundle[self.i].adapter = None

            self.i += 1
            self._active_trv = None
            if len(self.trv_bundle) > self.i:
                _LOGGER.debug(
                    "ConfigFlow advanced step moving to next TRV index=%s", self.i
                )
                return await self.async_step_advanced(None, self.trv_bundle[self.i])

            _has_off_mode = True
            for configured in self.trv_bundle:
                state_obj = self.hass.states.get(configured.entity_id)
                hvac_modes: list[str] = []
                if state_obj:
                    hvac_modes = state_obj.attributes.get("hvac_modes", []) or []
                if not device_offers_mode(hvac_modes, HVACMode.OFF):
                    _has_off_mode = False

            if not _has_off_mode:
                return await self.async_step_confirm(None, "no_off_mode")
            return await self.async_step_confirm()

        fields = _build_advanced_fields(
            sources=(user_input, existing_adv),
            default_calibration=ctx.default_calibration,
            homematic=ctx.homematic,
            has_auto=ctx.has_auto,
            support_valve=ctx.info.get("support_valve", False),
            support_offset=ctx.info.get("support_offset", False),
        )
        _LOGGER.debug(
            "ConfigFlow advanced step showing form for trv=%s with defaults=%s",
            ctx.entity_id,
            existing_adv,
        )

        return self.async_show_form(
            step_id="advanced",
            data_schema=vol.Schema(fields),
            last_step=False,
            description_placeholders={
                "trv": ctx.entity_id or "-",
                "docs_url": CONFIG_WALKTHROUGH_URL,
            },
        )

    @override
    async def async_step_user(
        self, user_input: dict[str, object] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        current = self.data or {}

        if user_input is not None:
            _LOGGER.debug("ConfigFlow user step received input: %s", user_input)
            normalized = _normalize_user_submission(
                user_input,
                mode="create",
                base=current,
                errors=errors,
                system_unit=self.hass.config.units.temperature_unit,
            )
            settings = _merged_settings(current, normalized)
            self.data = settings
            self._user_draft = normalized
            _LOGGER.debug("ConfigFlow user step normalized data: %s", settings)
            if not normalized[CONF_NAME]:
                errors["base"] = "no_name"

            heaters = normalized[CONF_THERMOSTAT]
            if not heaters:
                errors[CONF_THERMOSTAT] = "no_heater"

            unknown = _unknown_placeholders(self.hass, heaters) if heaters else None
            if unknown:
                errors[CONF_THERMOSTAT] = "trv_not_found"
                placeholders = unknown

            if not errors:
                in_use = _in_use_placeholders(self.hass, heaters, None)
                if in_use:
                    return self.async_abort(
                        reason="trv_in_use", description_placeholders=in_use
                    )
                self.trv_bundle = []
                models: list[str] = []
                for entity_id in heaters:
                    draft, model = await _new_trv_draft(self, entity_id)
                    self.trv_bundle.append(draft)
                    models.append(model)
                _LOGGER.debug(
                    "ConfigFlow user step built trv bundle: %s", self.trv_bundle
                )
                settings[CONF_MODEL] = "/".join(models)
                return await self.async_step_advanced(None, self.trv_bundle[0])

        fields = _build_user_fields(
            mode="create",
            current=self.data or {},
            user_input=user_input,
            system_unit=self.hass.config.units.temperature_unit,
        )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(fields),
            errors=errors,
            last_step=False,
            description_placeholders={
                "docs_url": CONFIG_WALKTHROUGH_URL,
                **placeholders,
            },
        )


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle a option flow for a config entry."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self.i = 0
        self.trv_bundle: list[_TrvDraft] = []
        self.device_name = ""
        self._last_step = False
        # The entry's settings with the user step's written over them, with
        # the thermostats as entity ids.
        self.updated_config: dict[str, object] = {}
        self._active_trv: _TrvDraft | None = None
        # Do not set `self.config_entry` directly; store in a private attribute
        # to avoid deprecated behavior. The framework will set `config_entry` on
        # the options flow object as needed.
        self._config_entry = config_entry
        super().__init__()

    async def async_step_init(
        self, _user_input: dict[str, object] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Manage the options."""
        return await self.async_step_user()

    async def async_step_advanced(
        self, user_input: dict[str, object] | None = None, trv: _TrvDraft | None = None
    ) -> config_entries.ConfigFlowResult:
        """Manage the advanced options."""
        if trv is None:
            trv = self._active_trv
        # The step is entered with a TRV and submitted for the one it showed.
        assert trv is not None

        self._active_trv = trv
        ctx = await _prepare_advanced_context(self, trv)
        existing_adv = trv.stored_advanced()
        _LOGGER.debug(
            "OptionsFlow advanced step called (index=%s, trv=%s) with user_input=%s",
            self.i,
            ctx.entity_id,
            user_input,
        )

        if user_input is not None:
            advanced_data = _normalize_advanced_submission(
                user_input,
                default_calibration=ctx.default_calibration,
                homematic=ctx.homematic,
                has_auto=ctx.has_auto,
            )
            _LOGGER.debug(
                "OptionsFlow advanced step storing data for %s (index %s): %s",
                trv.entity_id,
                self.i,
                advanced_data,
            )
            self.trv_bundle[self.i].advanced = advanced_data
            self.trv_bundle[self.i].adapter = None

            self.i += 1

            if len(self.trv_bundle) > self.i:
                self._active_trv = None
                return await self.async_step_advanced(None, self.trv_bundle[self.i])

            options = _stored_settings(self.updated_config, self.trv_bundle)
            _LOGGER.debug("Updated config: %s", options)
            _LOGGER.debug(
                "OptionsFlow writing heater bundle: %s", options[CONF_THERMOSTAT]
            )

            # Another entry can have taken a thermostat this one gains while
            # the forms were open, so the check runs again with nothing
            # awaited before the write.
            in_use = self._in_use_placeholders(
                [configured.entity_id for configured in self.trv_bundle]
            )
            if in_use:
                self.i = 0
                self.trv_bundle = []
                self._active_trv = None
                return self._show_user_form(
                    options, {CONF_THERMOSTAT: "trv_in_use"}, in_use
                )

            # The comparison reads the entry as stored, so it runs before the
            # write; the signal goes out only once the write has happened.
            algorithms_changed = self._calibration_algorithms_changed(options)

            # The whole configuration lives in the entry's options. The data
            # is emptied in the same update, so an entry 1.9 saved last, whose
            # settings are in its data, is written - and so reloaded - once
            # rather than twice.
            self.hass.config_entries.async_update_entry(
                self._config_entry, data={}, options=options
            )
            if algorithms_changed:
                # Dynamic entity management adds and removes algorithm sensors.
                signal_key = f"bt_config_changed_{self._config_entry.entry_id}"
                async_dispatcher_send(
                    self.hass, signal_key, {"entry_id": self._config_entry.entry_id}
                )
            self._active_trv = None
            # The options are written above already; finishing the flow with
            # the same options leaves the entry unchanged and reloads nothing.
            name = options.get(CONF_NAME)
            return self.async_create_entry(
                title=name if isinstance(name, str) else None, data=options
            )

        fields = _build_advanced_fields(
            sources=(user_input, existing_adv),
            default_calibration=ctx.default_calibration,
            homematic=ctx.homematic,
            has_auto=ctx.has_auto,
            support_valve=ctx.info.get("support_valve", False),
            support_offset=ctx.info.get("support_offset", False),
        )
        _LOGGER.debug(
            "OptionsFlow advanced step showing form for trv=%s with defaults=%s",
            ctx.entity_id,
            existing_adv,
        )
        self.device_name = "-"
        self._last_step = self.i == len(self.trv_bundle) - 1

        return self.async_show_form(
            step_id="advanced",
            data_schema=vol.Schema(fields),
            last_step=self._last_step,
            description_placeholders={
                "trv": ctx.entity_id or "-",
                "docs_url": CONFIG_WALKTHROUGH_URL,
            },
        )

    async def async_step_user(
        self, user_input: dict[str, object] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the user step."""
        errors: dict[str, str] = {}
        in_use_placeholders: dict[str, str] = {}
        if user_input is not None:
            _LOGGER.debug("OptionsFlow user step received input: %s", user_input)
            stored_settings: Mapping[str, object] = entry_settings(self._config_entry)
            normalized = _normalize_user_submission(
                user_input,
                mode="update",
                base=stored_settings,
                errors=errors,
                system_unit=self.hass.config.units.temperature_unit,
            )
            self.updated_config = _merged_settings(stored_settings, normalized)
            _LOGGER.debug(
                "OptionsFlow user step normalized data: %s", self.updated_config
            )
            heaters = normalized[CONF_THERMOSTAT]
            # The room sensor is required, but in this form it is optional so
            # the stored one can be pre-filled; an emptied selector arrives as
            # a missing key.
            if not normalized[CONF_TEMPERATURE_SENSOR]:
                errors[CONF_TEMPERATURE_SENSOR] = "no_sensor"
            in_use = self._in_use_placeholders(heaters)
            if in_use:
                errors[CONF_THERMOSTAT] = "trv_in_use"
                in_use_placeholders = in_use
            unknown = _unknown_placeholders(
                self.hass, heaters, kept=trv_entity_ids(self._config_entry)
            )
            if unknown:
                errors[CONF_THERMOSTAT] = "trv_not_found"
                in_use_placeholders = unknown

            if not errors:
                self.trv_bundle = []

                # The stored thermostats by entity id.
                existing_trvs = _stored_thermostats(
                    entry_settings(self._config_entry).get(CONF_THERMOSTAT)
                )

                for entity_id in heaters:
                    if entity_id in existing_trvs:
                        self.trv_bundle.append(
                            _stored_trv_draft(entity_id, existing_trvs[entity_id])
                        )
                    else:
                        # This is a new TRV added during edit
                        draft, _model = await _new_trv_draft(self, entity_id)
                        self.trv_bundle.append(draft)

                _LOGGER.debug(
                    "OptionsFlow user step built trv bundle: %s", self.trv_bundle
                )

                if self.trv_bundle:
                    return await self.async_step_advanced(None, self.trv_bundle[0])

                errors[CONF_THERMOSTAT] = "no_heater"

        return self._show_user_form(
            entry_settings(self._config_entry), errors, in_use_placeholders, user_input
        )

    def _in_use_placeholders(
        self, trv_entity_ids_to_check: Iterable[str]
    ) -> dict[str, str] | None:
        """Name a thermostat this entry gains that another entry controls.

        A thermostat belongs to one entry. An overlap the entry already has is
        left alone; only a thermostat it gains is checked.
        """
        return _in_use_placeholders(
            self.hass,
            trv_entity_ids_to_check,
            self._config_entry.entry_id,
            kept=trv_entity_ids(self._config_entry),
        )

    def _show_user_form(
        self,
        current: Mapping[str, object],
        errors: dict[str, str],
        placeholders: Mapping[str, str],
        user_input: Mapping[str, object] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Show the user step prefilled from ``current`` and ``user_input``."""
        fields = _build_user_fields(
            mode="update",
            current=current,
            user_input=user_input,
            system_unit=self.hass.config.units.temperature_unit,
        )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(fields),
            errors=errors,
            last_step=False,
            description_placeholders={
                "docs_url": CONFIG_WALKTHROUGH_URL,
                **placeholders,
            },
        )

    def _calibration_algorithms_changed(self, options: Mapping[str, object]) -> bool:
        """Return whether ``options`` change the set of calibration algorithms."""
        old_algorithms = self._get_active_algorithms(entry_settings(self._config_entry))
        new_algorithms = self._get_active_algorithms(options)

        if old_algorithms != new_algorithms:
            algorithms_added = new_algorithms - old_algorithms
            algorithms_removed = old_algorithms - new_algorithms

            _LOGGER.info(
                "Better Thermostat %s: Calibration algorithms changed. Added: %s, Removed: %s",
                options.get(CONF_NAME, "unknown"),
                [alg.value for alg in algorithms_added],
                [alg.value for alg in algorithms_removed],
            )
            return True
        return False

    @staticmethod
    def _get_active_algorithms(config: Mapping[str, object]) -> set[CalibrationMode]:
        """Get set of calibration algorithms currently in use by any TRV."""
        thermostats = config.get(CONF_THERMOSTAT)
        if not isinstance(thermostats, list):
            return set()

        active_algorithms: set[CalibrationMode] = set()
        for trv in thermostats:
            if not is_json_object(trv):
                continue
            advanced = trv.get("advanced", {})
            if not is_json_object(advanced):
                continue
            calibration_mode = configured_calibration_mode(advanced)
            if calibration_mode is not None:
                active_algorithms.add(calibration_mode)

        return active_algorithms


def _stored_thermostats(value: object) -> dict[str, Mapping[str, object]]:
    """Return the thermostats an entry stores, by entity id."""
    if not isinstance(value, list):
        return {}
    return {
        entity_id: trv
        for trv in value
        if is_json_object(trv)
        and isinstance(entity_id := trv.get("trv"), str)
        and entity_id
    }
