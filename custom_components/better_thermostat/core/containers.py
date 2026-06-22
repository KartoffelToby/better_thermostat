"""Typed containers for the three attribute lifecycles of the entity.

The entity used to mix three lifecycles in one attribute bag:

* :class:`BtConfig` — static configuration, set once at setup and frozen.
* :class:`BtRuntime` — live operating values, changing with every
  observation or control cycle, rebuilt from scratch on restart.
* learned values — these already have owned homes: the heating-power
  and heat-loss trackers on the entity, and the StateManager for
  everything that persists across restarts. No third container is
  duplicated here.

The entity exposes the historical attribute names as properties that
delegate into these containers, so call sites keep reading
``self.cur_temp`` while each value lives in exactly one container.
"""

from __future__ import annotations

from dataclasses import dataclass, fields


@dataclass(frozen=True)
class BtConfig:
    """Static configuration: set once at setup, never changed afterwards."""

    device_name: str = ""
    model: str | None = None
    sensor_entity_id: str | None = None
    humidity_sensor_entity_id: str | None = None
    cooler_entity_id: str | None = None
    window_id: str | None = None
    window_delay: float = 0.0
    window_delay_after: float = 0.0
    weather_entity: str | None = None
    outdoor_sensor: str | None = None
    off_temperature: float | None = None
    tolerance: float = 0.0


@dataclass
class BtRuntime:
    """Live operating values: rebuilt from observations after a restart.

    The discrete mode flags (window open, startup, maintenance,
    degraded) live in the kernel's FSM regions and are exposed as
    derived read-only properties on the entity — they have no second
    home here.
    """

    cur_temp: float | None = None
    cur_temp_filtered: float | None = None
    external_temp_ema: float | None = None
    temp_slope: float | None = None
    call_for_heat: bool = True
    ignore_states: bool = False
    bt_target_temp: float | None = None
    bt_target_cooltemp: float | None = None


def container_field_names() -> dict[str, frozenset[str]]:
    """Return the attribute-to-container assignment for the guard test."""
    return {
        "config": frozenset(f.name for f in fields(BtConfig)),
        "runtime": frozenset(f.name for f in fields(BtRuntime)),
    }
