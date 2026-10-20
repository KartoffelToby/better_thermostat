"""Conditions that override the heating intent, and what each one means.

A condition is an active observation together with its meaning: what it
says about the room. The meaning alone fixes what the kernel does about
it (its :class:`Effect`) and how it ranks against other active
conditions. Neither is configurable per condition, so the outcome of any
combination of active conditions is fixed by the two tables below.

The user's mode choice is not a condition. Mode OFF ranks between the
two rank groups: a release outranks it, and it outranks every condition
that turns the TRVs off.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from .desired import Suppression


class Meaning(StrEnum):
    """What an active condition says about the room."""

    STARTUP = "startup"
    MAINTENANCE = "maintenance"
    WINDOW_OPEN = "window_open"
    NO_CALL_FOR_HEAT = "no_call_for_heat"


class Action(StrEnum):
    """What the kernel asks of the TRVs while a condition rules."""

    # No intent at all: Better Thermostat writes nothing to the TRVs.
    RELEASE = "release"
    OFF = "off"


@dataclass(frozen=True)
class Effect:
    """Fixed consequence of one meaning.

    Attributes
    ----------
    action : Action
        What the addressed TRVs are asked to do.
    keeps_call_for_heat : bool
        ``True`` passes the snapshot's heat demand through to the
        desired state, ``False`` reports no heat demand.
    """

    action: Action
    keeps_call_for_heat: bool


@dataclass(frozen=True)
class Condition:
    """One active condition, applying to every addressed TRV.

    Attributes
    ----------
    meaning : Meaning
        What the condition says about the room; selects effect and rank.
    suppression : Suppression | None
        Reason carried on the OFF intents this condition produces. Two
        sources of one meaning may carry different reasons, which is why
        the reason sits on the condition rather than on its effect.
    """

    meaning: Meaning
    suppression: Suppression | None = None


EFFECTS: Final[Mapping[Meaning, Effect]] = MappingProxyType(
    {
        Meaning.STARTUP: Effect(Action.RELEASE, keeps_call_for_heat=True),
        Meaning.MAINTENANCE: Effect(Action.RELEASE, keeps_call_for_heat=True),
        Meaning.WINDOW_OPEN: Effect(Action.OFF, keeps_call_for_heat=True),
        Meaning.NO_CALL_FOR_HEAT: Effect(Action.OFF, keeps_call_for_heat=False),
    }
)

# Rank, strongest first. Mode OFF sits between the two groups.
RANK_ABOVE_MODE_OFF: Final = (Meaning.STARTUP, Meaning.MAINTENANCE)
RANK_BELOW_MODE_OFF: Final = (Meaning.WINDOW_OPEN, Meaning.NO_CALL_FOR_HEAT)


def strongest(
    conditions: Sequence[Condition], rank: Sequence[Meaning]
) -> Condition | None:
    """Return the highest-ranked condition whose meaning ``rank`` lists.

    Among conditions of the same meaning the earliest in ``conditions``
    wins, so the order in which sources are collected decides whose
    suppression reason is carried.

    Parameters
    ----------
    conditions : Sequence[Condition]
        Active conditions in collection order.
    rank : Sequence[Meaning]
        Meanings to consider, strongest first.

    Returns
    -------
    Condition | None
        The ruling condition, or ``None`` when no listed meaning is active.
    """
    for meaning in rank:
        for condition in conditions:
            if condition.meaning == meaning:
                return condition
    return None
