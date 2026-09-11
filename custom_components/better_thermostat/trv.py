"""Per-TRV domain object.

A :class:`Trv` bundles everything Better Thermostat knows about one
thermostatic radiator valve: static configuration (integration, model,
adapter, quirks), reported device state, and the write-tracking flags
the control loop maintains. The entries of ``real_trvs`` are
instances of this class, accessed via typed attributes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import ModuleType
from typing import Any

# How many unconfirmed writes a device is remembered to possibly echo. Writes
# since the last confirmation are few; the bound only guards against a device
# that never confirms while the control loop keeps writing. The confirmed
# setpoint is held separately and is not counted against this bound.
ECHO_SETPOINTS_LIMIT = 8


@dataclass
class Trv:
    """State, adapter, and quirks of a single TRV."""

    entity_id: str

    # -- Static configuration --------------------------------------------
    integration: str | None = None
    model: str | None = None
    calibration: Any = None
    adapter: ModuleType | None = None
    model_quirks: ModuleType | None = None
    advanced: dict[str, Any] = field(default_factory=dict)

    # -- Reported device state -------------------------------------------
    valve_position: float | None = None
    valve_position_entity: str | None = None
    valve_position_writable: bool | None = None
    valve_max_opening: float = 100.0
    max_temp: float | None = None
    min_temp: float | None = None
    target_temp_step: float | None = None
    temperature: float | None = None
    current_temperature: float | None = None
    hvac_modes: list[str] | None = None
    hvac_mode: str | None = None
    hvac_action: str | None = None
    local_temperature_calibration_entity: str | None = None
    local_calibration_min: float = -7
    local_calibration_max: float = 7
    local_calibration_step: float = 0.5

    # -- Write tracking ----------------------------------------------------
    ignore_trv_states: bool = False
    calibration_received: bool = True
    target_temp_received: bool = True
    system_mode_received: bool = True
    # One-shot flag: the next live internal reading after an outage must
    # bypass the debounce so it is not dropped as a stale duplicate.
    accept_next_internal_temp: bool = False
    # When this device's internal temperature was last accepted. The debounce
    # that guards it is a property of the device that reported it, so the
    # stamp belongs to that device: a reading taken from one valve says
    # nothing about how fresh another valve's reading is. ``None`` means no
    # reading has been accepted yet and the next one passes.
    last_internal_sensor_change: datetime | None = None
    # The setpoint BT last sent; the device's own at startup.
    last_temperature: float | None = None
    # The setpoint in °C the device last confirmed, the device's own at
    # startup. A device may report it again at any time, so it stays a value
    # BT itself wrote even once later writes are in flight.
    confirmed_setpoint: float | None = None
    # The setpoints in °C written since that confirmation, oldest first. A
    # device that did not take the latest write still reports an earlier one,
    # so every one of them remains a value BT itself wrote.
    # ``trigger_trv_change`` reads a report within the echo window of
    # ``confirmed_setpoint`` or any of these as BT's write coming back rather
    # than as a user press.
    echo_setpoints: list[float] = field(default_factory=list)
    last_valve_position: float | None = None
    last_hvac_mode: str | None = None
    last_current_temperature: float | None = None
    # ``last_calibration`` is the command the adapter actually wrote after its
    # own clamp to the declared offset range; ``last_calibration_requested`` is
    # the value that was asked for before that clamp.
    last_calibration: float | None = None
    last_calibration_requested: float | None = None
    # Identity of the offset command currently in flight. Each accepted write
    # takes the next number, and only the watchdog holding that number may
    # release ``calibration_received``.
    calibration_write_generation: int = 0
    last_valve_percent: float | None = None
    last_valve_method: str | None = None
    # HVAC modes already annunciated as unsupported, so the control loop
    # reports each one once instead of on every cycle. Cleared whenever the
    # device reports a different mode list.
    unsupported_modes_logged: set[str] = field(default_factory=set)

    # -- Calibration results -----------------------------------------------
    calibration_balance: dict[str, Any] | None = None
    balance: dict[str, Any] | None = None

    # -- Quirk scratchpad ----------------------------------------------------
    # Model quirks may stash private bookkeeping here (e.g. TRVZB valve
    # bump sequencing) without widening the typed surface.
    extra: dict[str, Any] = field(default_factory=dict)

    def consume_accept_next_internal_temp(self) -> bool:
        """Return and clear the one-shot accept-next-internal-temp flag.

        Returns
        -------
        bool
            ``True`` if the next internal reading should bypass the
            debounce; the flag is reset to ``False`` as a side effect.
        """
        accepted = self.accept_next_internal_temp
        self.accept_next_internal_temp = False
        return accepted

    def remember_setpoint_written(self, value: float) -> None:
        """Add a setpoint BT put on the wire to the values a report may echo.

        A value written again moves to the end, so the command most recently
        on the wire is the last one a full list gives up. The confirmed
        setpoint is held outside this list and is never evicted.

        Parameters
        ----------
        value : float
            The setpoint in °C as it was sent.
        """
        if value in self.echo_setpoints:
            self.echo_setpoints.remove(value)
        self.echo_setpoints.append(value)
        if len(self.echo_setpoints) > ECHO_SETPOINTS_LIMIT:
            del self.echo_setpoints[0]

    def remember_setpoint_confirmed(self, value: float | None) -> None:
        """Record the setpoint the device confirmed and retire the writes before it.

        The caller passes the command it waited on rather than the current
        ``last_temperature``, which another task may have moved on to. Only
        one write is watched at a time, so writes issued while the wait ran
        sit behind the confirmed one in the list and are still in flight;
        they stay. A value no longer in the list gives no boundary to retire
        against, so nothing is dropped.

        Parameters
        ----------
        value : float | None
            The confirmed setpoint in °C, or ``None`` when the device
            reported none.
        """
        self.confirmed_setpoint = value
        if value is not None and value in self.echo_setpoints:
            del self.echo_setpoints[: self.echo_setpoints.index(value) + 1]

    @classmethod
    def from_legacy_dict(cls, entity_id: str, data: dict[str, Any]) -> Trv:
        """Build a Trv from a plain per-entity dict.

        Known keys become typed fields; unknown keys land in ``extra``.
        A legacy ``extra`` dict is merged into ``extra`` rather than
        nested, and a legacy ``entity_id`` key is ignored in favor of
        the ``entity_id`` argument.

        Parameters
        ----------
        entity_id : str
            Entity id of the TRV this state belongs to.
        data : dict[str, Any]
            Legacy per-entity dict as previously stored in ``real_trvs``.

        Returns
        -------
        Trv
            Typed equivalent of ``data``.
        """
        fields_in: dict[str, Any] = {}
        extra: dict[str, Any] = {}
        for key, value in data.items():
            if key == "entity_id":
                continue
            if key == "extra":
                if isinstance(value, dict):
                    extra.update(value)
                else:
                    extra[key] = value
            elif key in cls.__dataclass_fields__:
                fields_in[key] = value
            else:
                extra[key] = value
        trv = cls(entity_id=entity_id, **fields_in)
        trv.extra.update(extra)
        return trv
