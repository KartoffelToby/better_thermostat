"""Keys of the learned controller state.

The MPC, TPI and PID states are stored per thermostat and per target
bucket under string keys of two shapes:

- ``<unique_id>:<segment>:<bucket_tag>`` for a state that belongs to one
  target bucket, where the segment is a thermostat's entity id or
  :data:`GROUP_KEY_SEGMENT`, and the bucket tag is ``t21.5`` or
  :data:`UNKNOWN_BUCKET_TAG`;
- ``<unique_id>:<entity_id>`` for a TRV's PID loop entry, which follows
  the TRV across target changes.

The strings are the keys of the stored JSON sections, so their form is
part of the store format. :class:`StateKey` builds and parses them in one
place; the sections themselves stay keyed by ``str``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# The middle segment of a learned-state key that belongs to the room as a
# whole rather than to one thermostat.
GROUP_KEY_SEGMENT: Final = "group"

# The bucket tag of a state keyed while the target was not a number.
UNKNOWN_BUCKET_TAG: Final = "tunknown"


def round_to_bucket(temperature: float) -> float:
    """Round a target temperature to its 0.5 °C bucket centre.

    Parameters
    ----------
    temperature : float
        Target temperature in °C.

    Returns
    -------
    float
        The nearest multiple of 0.5 °C.
    """
    return round(float(temperature) * 2.0) / 2.0


def format_bucket(bucket: float) -> str:
    """Format a bucket centre as a ``t<temperature>`` tag (e.g. ``t21.0``).

    Parameters
    ----------
    bucket : float
        Bucket centre in °C.

    Returns
    -------
    str
        The bucket tag with one decimal.
    """
    return f"t{bucket:.1f}"


def bucket_tag_of(target: object) -> str:
    """Return the bucket tag a state for ``target`` is keyed under.

    Parameters
    ----------
    target : object
        The thermostat's target temperature in °C, or whatever stands in
        for it while there is none.

    Returns
    -------
    str
        The tag of the target's 0.5 °C bucket, or
        :data:`UNKNOWN_BUCKET_TAG` when the target is not a finite number.
    """
    if not isinstance(target, (int, float)):
        return UNKNOWN_BUCKET_TAG
    try:
        return format_bucket(round_to_bucket(target))
    except ValueError, OverflowError:
        return UNKNOWN_BUCKET_TAG


def parse_bucket_tag(tag: str | None) -> float | None:
    """Return the bucket centre a tag such as ``t21.0`` names.

    Parameters
    ----------
    tag : str or None
        A bucket tag.

    Returns
    -------
    float or None
        The bucket centre in °C, or None when ``tag`` names no number.
    """
    if not tag or not tag.startswith("t"):
        return None
    try:
        return float(tag[1:])
    except ValueError:
        return None


def is_bucket_tag(part: str) -> bool:
    """Return whether ``part`` is a bucket tag such as ``t21.0``.

    Parameters
    ----------
    part : str
        The last colon-separated part of a key.

    Returns
    -------
    bool
        True for :data:`UNKNOWN_BUCKET_TAG` and for ``t`` followed by a
        number.
    """
    return part == UNKNOWN_BUCKET_TAG or parse_bucket_tag(part) is not None


@dataclass(frozen=True, slots=True)
class StateKey:
    """Key of one learned controller state.

    Attributes
    ----------
    unique_id : str
        The owning Better Thermostat's unique id.
    segment : str
        A thermostat's entity id, or :data:`GROUP_KEY_SEGMENT`.
    bucket_tag : str or None
        The target bucket tag, or None for a TRV's PID loop entry.
    """

    unique_id: str
    segment: str
    bucket_tag: str | None = None

    def __str__(self) -> str:
        """Return the stored form of the key."""
        if self.bucket_tag is None:
            return f"{self.unique_id}:{self.segment}"
        return f"{self.unique_id}:{self.segment}:{self.bucket_tag}"

    @classmethod
    def parse(cls, key: str) -> StateKey | None:
        """Parse a stored key back into its parts.

        Entity ids hold no colon but a dot between domain and object id,
        so the parts are read from the right. A key ending in a bucket tag
        is a bucket key; a key ending in a dotted segment is a PID loop
        key. A key of any other shape, such as the shared
        ``<unique_id>:reid``, is not a state key of a thermostat.

        Parameters
        ----------
        key : str
            A key as stored.

        Returns
        -------
        StateKey or None
            The parsed key, or None when ``key`` has neither shape.
        """
        head, separator, last = key.rpartition(":")
        if not separator:
            return None
        if is_bucket_tag(last):
            unique_id, separator, segment = head.rpartition(":")
            if not separator:
                return None
            return cls(unique_id, segment, last)
        if "." in last:
            return cls(head, last)
        return None

    @property
    def bucket_temperature(self) -> float | None:
        """The bucket centre in °C, or None when the key names no number."""
        return parse_bucket_tag(self.bucket_tag)

    def is_bucket_of(self, other: StateKey) -> bool:
        """Return whether this is a bucket key of ``other``'s thermostat.

        Parameters
        ----------
        other : StateKey
            Any key of the thermostat in question.

        Returns
        -------
        bool
            True when this key carries a bucket tag and shares
            ``other``'s unique id and segment.
        """
        return (
            self.bucket_tag is not None
            and self.unique_id == other.unique_id
            and self.segment == other.segment
        )
