"""Validated, restart-persistent weather forecast fallback.

Only timestamped forecast slots can substitute for an unavailable weather entity.
A stale forecast is never presented as live weather.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import math
from typing import Any

# Do not trust an old snapshot indefinitely, even when its horizon is long.
MAX_CACHE_AGE = timedelta(hours=24)
SLOT_INTERVALS = {
    "hourly": timedelta(hours=1),
    "twice_daily": timedelta(hours=12),
    "daily": timedelta(days=1),
}
MAX_FUTURE = timedelta(days=2)


def _effective_utc_now(now: datetime | None) -> datetime:
    """Normalize an aware timestamp, rejecting ambiguous naive inputs."""
    if now is None:
        return datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(UTC)


def _utc_time(value: str) -> datetime | None:
    """Return an aware UTC datetime for a persisted ISO timestamp."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _stored_cache(bt: Any) -> dict | None:
    """Read the snapshot either from the live entity or its durable state."""
    snapshot = getattr(bt, "_weather_forecast_cache", None)
    if isinstance(snapshot, dict):
        return snapshot
    manager = getattr(bt, "state_mgr", None)
    if manager is None:
        return None
    state = getattr(manager, "state", None)
    snapshot = getattr(state, "weather_forecast", None)
    return snapshot if isinstance(snapshot, dict) else None


def usable_weather_cache(bt: Any, now: datetime | None = None) -> list[dict]:
    """Return remaining timestamped slots only while the cache is trustworthy.

    The entity ID binds the cache to the configured provider; do not use data
    from a previous integration configuration. Expiry uses wall-clock time so
    it also works after a Home Assistant restart. The optional now must be
    timezone-aware.
    """
    now = _effective_utc_now(now)
    snapshot = _stored_cache(bt)
    if not snapshot or snapshot.get("entity_id") != getattr(bt, "weather_entity", None):
        return []
    saved_at = _utc_time(snapshot.get("saved_at"))
    interval = SLOT_INTERVALS.get(snapshot.get("type"))
    if (
        saved_at is None
        or interval is None
        or saved_at > now + timedelta(minutes=5)
        or now - saved_at > MAX_CACHE_AGE
    ):
        return []
    values = snapshot.get("slots")
    if not isinstance(values, list):
        return []
    valid = []
    for item in values[:48]:
        if not isinstance(item, dict):
            continue
        stamp = _utc_time(item.get("datetime"))
        temp = item.get("temperature")
        if (
            stamp is None
            or type(temp) not in (int, float)
            or not math.isfinite(temp)
            or not now - interval < stamp <= now + MAX_FUTURE
        ):
            continue
        valid.append(
            {
                "datetime": stamp,
                "temperature": float(temp),
                "condition": item.get("condition"),
            }
        )
    valid.sort(key=lambda row: row["datetime"])
    return valid


def cache_current_weather(bt: Any, now: datetime | None = None) -> dict | None:
    """Return the most recent forecast slot covering the current time."""
    now = _effective_utc_now(now)
    slots = usable_weather_cache(bt, now=now)
    return next((slot for slot in reversed(slots) if slot["datetime"] <= now), None)


def remember_weather_forecast(
    bt: Any, forecast_type: str, rows: list[dict], *, current_unit: str | None
) -> bool:
    """Save valid timestamped slots using the existing Better Thermostat store."""
    from .helpers import convert_to_float_celsius

    if forecast_type not in SLOT_INTERVALS:
        return False
    slots = []
    for row in rows[:48]:
        if not isinstance(row, dict):
            continue
        stamp = _utc_time(row.get("datetime"))
        if stamp is None:
            continue
        temperature = convert_to_float_celsius(
            str(row.get("temperature", "")),
            getattr(bt, "device_name", "Better Thermostat"),
            "remember_weather_forecast()",
            unit_of_measurement=row.get("temperature_unit", current_unit),
        )
        if type(temperature) not in (int, float) or not math.isfinite(temperature):
            continue
        slots.append(
            {
                "datetime": stamp.isoformat(),
                "temperature": float(temperature),
                "condition": (
                    row.get("condition")
                    if isinstance(row.get("condition"), str)
                    else None
                ),
            }
        )
    if not slots:
        return False
    snapshot = {
        "entity_id": bt.weather_entity,
        "type": forecast_type,
        "saved_at": datetime.now(UTC).isoformat(),
        "slots": slots,
    }
    bt._weather_forecast_cache = snapshot
    manager = getattr(bt, "state_mgr", None)
    if manager is not None:
        manager.state.weather_forecast = snapshot
        manager.mark_dirty()
        schedule_save = getattr(bt, "schedule_save_state", None)
        if callable(schedule_save):
            schedule_save()
    return True
