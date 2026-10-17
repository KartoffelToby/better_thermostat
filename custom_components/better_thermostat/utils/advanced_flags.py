"""Read the per-TRV boolean options the way the options flow saves them.

The options flow stores ``protect_overheating``, ``no_off_system_mode``,
``heat_auto_swapped``, ``valve_maintenance``, ``child_lock`` and
``homematicip`` as booleans, but an entry written by an older version can
still hold them as strings such as ``"false"`` or as the integers ``0`` and
``1``. Every reader goes through :func:`advanced_flag`, so a stored value
means the same thing to the running thermostat as it does to the options
flow that would save it.
"""

from __future__ import annotations

from collections.abc import Mapping

_TRUE_SPELLINGS = frozenset({"true", "yes", "1", "on"})
_FALSE_SPELLINGS = frozenset({"false", "no", "0", "off"})


def as_bool(value: object, default: bool = False) -> bool:
    """Return the boolean a stored or submitted value spells.

    Parameters
    ----------
    value : object
        A boolean, ``None``, a string such as ``"true"``, ``" Off "`` or
        ``"0"`` (case and surrounding blanks are ignored), or any other value.
    default : bool
        The answer for ``None``.

    Returns
    -------
    bool
        ``value`` itself for a boolean, ``default`` for ``None``, the
        spelled answer for a recognized string, and the truthiness of
        anything else.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_SPELLINGS:
            return True
        if lowered in _FALSE_SPELLINGS:
            return False
    return bool(value)


def advanced_flag(
    advanced: Mapping[str, object] | None, key: str, default: bool = False
) -> bool:
    """Return one boolean option of a TRV's ``advanced`` mapping.

    Parameters
    ----------
    advanced : Mapping[str, object] | None
        The TRV's ``advanced`` options; ``None`` reads as empty.
    key : str
        The option, for example ``CONF_VALVE_MAINTENANCE``.
    default : bool
        The answer when the option is missing or ``None``.

    Returns
    -------
    bool
        The option read with :func:`as_bool`.
    """
    if advanced is None:
        return default
    return as_bool(advanced.get(key), default)
