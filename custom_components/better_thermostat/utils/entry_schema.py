"""The shape of the settings a config entry stores, and the parser that checks it.

The config and options flows write the settings, ``async_migrate_entry``
lifts older entries to the current version, and every reader looks them up
with ``.get(key, default)``. Each type below is what the readers of that key
accept, which covers what the writers store today and what released versions
stored before them, so an entry that runs today still parses. A key a
migrated entry can lack is ``NotRequired``, so a reader's default keeps its
meaning.

The TypedDicts use the functional syntax. A key has to be a string literal
there, so the keys are spelled out; readers and writers use the ``CONF_*``
constants, which type checkers resolve to the same literals. As strings the
stored keys are not identifiers, so the glossary, which rejects some of their
spellings (``window_off_delay``, ``outdoor_sensor``) for identifiers, does not
apply to them.

Only a broken structure is refused: a missing or non-string name, a missing,
empty or non-list thermostat list, a thermostat without a string entity id
or integration, and a value that is not a mapping where one has to be. A
container no writer ever stored and no reader can use, such as a list where a
number belongs, is refused as well. Every value a current reader tolerates
parses.

Parsing never changes the stored entry: it builds a new mapping holding the
keys described here, with each value as stored, except that a whole number
stored where a reader expects a float becomes that float, and that the
boolean options are read with ``as_bool``, the reading the options flow saves
them with.

The target temperature bounds and step are stored with sentinels for
"automatic"; ``target_temperature_bound`` and ``target_temperature_step`` read
a stored value into a number, or ``None`` for automatic, so no other reader
compares against the sentinels.
"""

from __future__ import annotations

from collections.abc import Mapping
import math
from typing import Final, Literal, NotRequired, TypedDict

from homeassistant.const import CONF_NAME

from .advanced_flags import as_bool
from .const import (
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
    CONF_INTEGRATION,
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
    TARGET_TEMP_BOUND_AUTO,
)

# A choice as stored: a string, a number from an older version, or ``None``.
type StoredChoice = str | int | float | None

# The per-thermostat options of the advanced step. Every key is optional: an
# entry written before a key existed lacks it, and no migration adds the newer
# ones. The four choices are kept as stored, uninterpreted: older entries hold
# numbers there (``calibration_mode`` 0 for the default mode) or ``None``.
# ``balance_mode`` is the setting ``calibration_mode`` replaced; entries that
# predate it still carry it and it is still read. A flag stored as ``None`` is
# left out, which every reader takes the same way as ``None``.
TrvAdvanced = TypedDict(  # noqa: UP013
    "TrvAdvanced",
    {
        "calibration": StoredChoice,
        "calibration_mode": StoredChoice,
        "mpc_v2_plant_preset": StoredChoice,
        "protect_overheating": bool,
        "no_off_system_mode": bool,
        "heat_auto_swapped": bool,
        "valve_maintenance": bool,
        "child_lock": bool,
        "homematicip": bool,
        "balance_mode": StoredChoice,
    },
    total=False,
)

# One entry of the ``thermostat`` list.
TrvSettings = TypedDict(  # noqa: UP013
    "TrvSettings",
    {
        "trv": str,
        "integration": str,
        "model": NotRequired[str | None],
        "advanced": NotRequired[TrvAdvanced],
    },
)

# The settings of one Better Thermostat. ``None`` under an entity key means no
# entity is set. ``off_temperature`` is in the system unit. The keys that take
# a number or a string are stored as strings by the flows (the bounds and the
# step as selector tokens) and as numbers by older versions and by hand; their
# readers convert either with ``float()``, the bounds and the step through
# ``target_temperature_bound`` and ``target_temperature_step``.
BtSettings = TypedDict(  # noqa: UP013
    "BtSettings",
    {
        "name": str,
        "thermostat": list[TrvSettings],
        "model": NotRequired[str | None],
        "temperature_sensor": NotRequired[str | None],
        "humidity_sensor": NotRequired[str | None],
        "cooler": NotRequired[str | None],
        "window_sensors": NotRequired[str | None],
        "door_sensors": NotRequired[str | None],
        "outdoor_sensor": NotRequired[str | None],
        "weather": NotRequired[str | None],
        "window_off_delay": NotRequired[str | float | None],
        "window_off_delay_after": NotRequired[str | float | None],
        "door_off_delay": NotRequired[str | float | None],
        "door_off_delay_after": NotRequired[str | float | None],
        "off_temperature": NotRequired[str | float | None],
        "tolerance": NotRequired[str | float | None],
        "target_temp_min": NotRequired[str | float | None],
        "target_temp_max": NotRequired[str | float | None],
        "target_temp_step": NotRequired[str | float | None],
        "presets": NotRequired[list[str] | None],
    },
)

type _TextKey = Literal[
    "model",
    "temperature_sensor",
    "humidity_sensor",
    "cooler",
    "window_sensors",
    "door_sensors",
    "outdoor_sensor",
    "weather",
]
type _NumberOrTextKey = Literal[
    "window_off_delay",
    "window_off_delay_after",
    "door_off_delay",
    "door_off_delay_after",
    "off_temperature",
    "tolerance",
    "target_temp_min",
    "target_temp_max",
    "target_temp_step",
]
type _ChoiceKey = Literal[
    "calibration", "calibration_mode", "mpc_v2_plant_preset", "balance_mode"
]
type _FlagKey = Literal[
    "protect_overheating",
    "no_off_system_mode",
    "heat_auto_swapped",
    "valve_maintenance",
    "child_lock",
    "homematicip",
]

_TEXT_KEYS: Final[tuple[_TextKey, ...]] = (
    CONF_MODEL,
    CONF_TEMPERATURE_SENSOR,
    CONF_HUMIDITY_SENSOR,
    CONF_COOLER,
    CONF_WINDOW_SENSORS,
    CONF_DOOR_SENSORS,
    CONF_OUTDOOR_SENSOR,
    CONF_WEATHER,
)
_NUMBER_OR_TEXT_KEYS: Final[tuple[_NumberOrTextKey, ...]] = (
    CONF_WINDOW_OFF_DELAY,
    CONF_WINDOW_OFF_DELAY_AFTER,
    CONF_DOOR_OFF_DELAY,
    CONF_DOOR_OFF_DELAY_AFTER,
    CONF_OFF_TEMPERATURE,
    CONF_TOLERANCE,
    CONF_TARGET_TEMP_MIN,
    CONF_TARGET_TEMP_MAX,
    CONF_TARGET_TEMP_STEP,
)
_CHOICE_KEYS: Final[tuple[_ChoiceKey, ...]] = (
    CONF_CALIBRATION,
    CONF_CALIBRATION_MODE,
    CONF_MPC_V2_PLANT_PRESET,
    "balance_mode",
)
_FLAG_KEYS: Final[tuple[_FlagKey, ...]] = (
    CONF_PROTECT_OVERHEATING,
    CONF_NO_OFF_SYSTEM_MODE,
    CONF_HEAT_AUTO_SWAPPED,
    CONF_VALVE_MAINTENANCE,
    CONF_CHILD_LOCK,
    CONF_HOMEMATICIP,
)


class InvalidSettingsError(ValueError):
    """The stored settings do not have the shape ``BtSettings`` describes.

    The message names the offending key, so it can be shown to the user.
    """


def _describe(value: object) -> str:
    """Name the type of ``value`` for an error message."""
    return "null" if value is None else type(value).__name__


def _refuse(key: str, value: object, expected: str) -> InvalidSettingsError:
    """Return the error for ``key`` holding ``value`` instead of ``expected``."""
    return InvalidSettingsError(f"{key} is {_describe(value)}, not {expected}")


def _required(raw: Mapping[str, object], key: str, where: str) -> object:
    """Return the value under ``key``, which the settings must carry."""
    if key not in raw:
        raise InvalidSettingsError(f"{where}{key} is missing")
    return raw[key]


def _text(value: object, key: str) -> str:
    """Return ``value`` as stored, which must be a string."""
    if not isinstance(value, str):
        raise _refuse(key, value, "a string")
    return value


def _text_or_none(value: object, key: str) -> str | None:
    """Return ``value`` as stored, a string or ``None``."""
    return None if value is None else _text(value, key)


def _number_or_text(value: object, key: str) -> str | float | None:
    """Return a stored whole number as a float, and anything else as stored.

    A string, a float, a bool (which its readers' ``float()`` takes) and
    ``None`` stay as they are; a container is refused.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    if value is None or isinstance(value, str | float | int):
        return value
    raise _refuse(key, value, "a number or a string")


def _choice(value: object, key: str) -> StoredChoice:
    """Return a stored choice unchanged; a container is refused."""
    if value is None or isinstance(value, str | int | float):
        return value
    raise _refuse(key, value, "a string, a number or null")


def _mapping(value: object, key: str) -> Mapping[str, object]:
    """Return ``value``, which must be a mapping."""
    if not isinstance(value, Mapping):
        raise _refuse(key, value, "a mapping")
    return value


def _items(value: object, key: str) -> list[object]:
    """Return ``value``, which must be a list."""
    if not isinstance(value, list):
        raise _refuse(key, value, "a list")
    return value


def _parse_advanced(value: object, where: str) -> TrvAdvanced:
    """Return the advanced options of one thermostat."""
    where = f"{where}advanced"
    raw = _mapping(value, where)
    advanced: TrvAdvanced = {}
    for choice_key in _CHOICE_KEYS:
        if choice_key in raw:
            advanced[choice_key] = _choice(raw[choice_key], f"{where}.{choice_key}")
    for flag_key in _FLAG_KEYS:
        flag = raw.get(flag_key)
        if flag is not None:
            advanced[flag_key] = as_bool(flag)
    return advanced


def _parse_trv(value: object, index: int) -> TrvSettings:
    """Return one entry of the ``thermostat`` list."""
    where = f"{CONF_THERMOSTAT}[{index}]"
    raw = _mapping(value, where)
    where = f"{where}."
    trv_config: TrvSettings = {
        "trv": _text(_required(raw, "trv", where), f"{where}trv"),
        "integration": _text(
            _required(raw, CONF_INTEGRATION, where), f"{where}{CONF_INTEGRATION}"
        ),
    }
    if CONF_MODEL in raw:
        trv_config["model"] = _text_or_none(raw[CONF_MODEL], f"{where}{CONF_MODEL}")
    if "advanced" in raw:
        trv_config["advanced"] = _parse_advanced(raw["advanced"], where)
    return trv_config


def _parse_thermostats(raw: Mapping[str, object]) -> list[TrvSettings]:
    """Return the thermostats the entry controls, of which it needs one."""
    items = _items(_required(raw, CONF_THERMOSTAT, ""), CONF_THERMOSTAT)
    if not items:
        raise InvalidSettingsError(f"{CONF_THERMOSTAT} is empty")
    return [_parse_trv(item, index) for index, item in enumerate(items)]


def _parse_presets(value: object) -> list[str] | None:
    """Return the enabled presets as a new list of names, or ``None``."""
    if value is None:
        return None
    items = _items(value, CONF_PRESETS)
    return [_text(item, f"{CONF_PRESETS}[{index}]") for index, item in enumerate(items)]


def parse_settings(raw: Mapping[str, object]) -> BtSettings:
    """Check the stored settings of an entry and return them typed.

    Parameters
    ----------
    raw : Mapping[str, object]
        The entry's settings as ``entry_settings`` returns them.

    Returns
    -------
    BtSettings
        A new mapping with the keys ``BtSettings`` describes. Keys it does not
        describe are left out; ``raw`` is not changed.

    Raises
    ------
    InvalidSettingsError
        When a required key is missing (the name, the thermostat list, a
        thermostat's entity id or integration), when the thermostat list is
        empty, when a value that has to be a mapping, a list or a string is
        not one, or when a scalar key holds a container.
    """
    settings: BtSettings = {
        "name": _text(_required(raw, CONF_NAME, ""), CONF_NAME),
        "thermostat": _parse_thermostats(raw),
    }
    for text_key in _TEXT_KEYS:
        if text_key in raw:
            settings[text_key] = _text_or_none(raw[text_key], text_key)
    for number_or_text_key in _NUMBER_OR_TEXT_KEYS:
        if number_or_text_key in raw:
            settings[number_or_text_key] = _number_or_text(
                raw[number_or_text_key], number_or_text_key
            )
    if CONF_PRESETS in raw:
        settings["presets"] = _parse_presets(raw[CONF_PRESETS])
    return settings


def _finite_number(value: str | float) -> float:
    """Return ``value`` as a finite float.

    Raises
    ------
    ValueError
        When ``value`` does not read as a finite number.
    """
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{value!r} is not a finite number")
    return number


def target_temperature_bound(value: str | float | None) -> float | None:
    """Read a stored target temperature bound in degrees Celsius.

    Parameters
    ----------
    value : str | float | None
        The bound as ``parse_settings`` returns it.

    Returns
    -------
    float | None
        The bound, or ``None`` when it follows the controlled entities: the
        entry carries no value for it, an empty one, or the one the config
        flow stores for a bound left on automatic (``TARGET_TEMP_BOUND_AUTO``).

    Raises
    ------
    ValueError
        When ``value`` does not read as a finite number.
    """
    if value is None or value == "":
        return None
    bound = _finite_number(value)
    if bound == float(TARGET_TEMP_BOUND_AUTO):
        return None
    return bound


def target_temperature_step(value: str | float | None) -> float | None:
    """Read the stored target temperature step in degrees Celsius.

    Parameters
    ----------
    value : str | float | None
        The step as ``parse_settings`` returns it.

    Returns
    -------
    float | None
        The step, or ``None`` when it is automatic: the entry carries no value
        for it, an empty one (what the flows store for automatic), or a step
        of zero or less. The last covers ``"0.0"``, which older flows stored
        for automatic, and every other spelling of zero.

    Raises
    ------
    ValueError
        When ``value`` does not read as a finite number.
    """
    if value is None or value == "":
        return None
    step = _finite_number(value)
    if step <= 0.0:
        return None
    return step
