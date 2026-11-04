"""Offline weather forecast fallback, expiry and reload behavior."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.better_thermostat.utils.state_manager import (
    RuntimeState,
    _deserialize,
    _serialize,
)
from custom_components.better_thermostat.utils.weather_cache import (
    cache_current_weather,
    remember_weather_forecast,
    usable_weather_cache,
)

WEATHER = "weather.home"


def at(hour: int) -> datetime:
    """Fixed sample in UTC to make time-dependent assertions repeatable."""
    return datetime(2026, 11, 4, hour, tzinfo=timezone.utc)


def bt_with_store() -> SimpleNamespace:
    """Minimal thermostat using the existing HA runtime state persistence."""
    manager = SimpleNamespace(state=RuntimeState(), mark_dirty=MagicMock())
    return SimpleNamespace(
        weather_entity=WEATHER,
        device_name="Test room",
        state_mgr=manager,
        schedule_save_state=MagicMock(),
    )


def snapshot(now: datetime, values: list[tuple[int, float]]) -> dict:
    """Compact persisted cache as used by the integration."""
    return {
        "entity_id": WEATHER,
        "type": "hourly",
        "saved_at": now.isoformat(),
        "slots": [
            {
                "datetime": (now + timedelta(hours=h)).isoformat(),
                "temperature": t,
                "condition": "cloudy",
            }
            for h, t in values
        ],
    }


def test_weather_cache_survives_state_manager_roundtrip():
    """A cached hourly forecast must survive a Home Assistant restart."""
    bt = bt_with_store()
    bt.state_mgr.state.weather_forecast = snapshot(
        at(12), [(0, 3.0), (1, 4.0), (2, 5.0)]
    )
    restored = _deserialize(_serialize(bt.state_mgr.state))
    restarted = SimpleNamespace(
        weather_entity=WEATHER, state_mgr=SimpleNamespace(state=restored)
    )
    slots = usable_weather_cache(restarted, now=at(13))
    assert [slot["temperature"] for slot in slots] == [4.0, 5.0]
    assert cache_current_weather(restarted, now=at(13))["temperature"] == 4.0


def test_forecast_uses_current_hour_not_oldest_prediction():
    bt = bt_with_store()
    bt.state_mgr.state.weather_forecast = snapshot(at(12), [(0, 9), (1, 5), (2, 2)])
    assert cache_current_weather(bt, now=at(14))["temperature"] == 2.0


def test_cache_expires_after_forecast_horizon():
    bt = bt_with_store()
    bt.state_mgr.state.weather_forecast = snapshot(at(12), [(0, 9), (1, 5)])
    assert usable_weather_cache(bt, now=at(14) + timedelta(minutes=1)) == []


def test_cache_expires_when_snapshot_is_too_old():
    bt = bt_with_store()
    bt.state_mgr.state.weather_forecast = snapshot(at(12), [(25, 5)])
    assert usable_weather_cache(bt, now=at(12) + timedelta(hours=25)) == []


def test_other_weather_provider_never_reuses_previous_cache():
    bt = bt_with_store()
    bt.state_mgr.state.weather_forecast = snapshot(at(12), [(1, 5)])
    bt.weather_entity = "weather.new_source"
    assert usable_weather_cache(bt, now=at(12)) == []


def test_malformed_or_nonfinite_cache_cannot_mask_missing_weather():
    bt = bt_with_store()
    bt.state_mgr.state.weather_forecast = snapshot(at(12), [(1, float("nan"))])
    assert usable_weather_cache(bt, now=at(12)) == []
    bt.state_mgr.state.weather_forecast["slots"][0]["temperature"] = 5
    bt.state_mgr.state.weather_forecast["slots"][0]["datetime"] = "not-a-time"
    assert usable_weather_cache(bt, now=at(12)) == []


def test_live_forecast_is_recorded_for_reboot():
    bt = bt_with_store()
    fixed = datetime.now(timezone.utc)
    assert remember_weather_forecast(
        bt,
        "hourly",
        [{"datetime": (fixed + timedelta(hours=1)).isoformat(),
          "temperature": 4, "condition": "cloudy"}],
        current_unit="°C",
    )
    assert bt.state_mgr.state.weather_forecast["slots"][0]["temperature"] == 4.0
    assert usable_weather_cache(bt, now=fixed)
    bt.state_mgr.mark_dirty.assert_called_once()
    bt.schedule_save_state.assert_called_once()


@pytest.mark.asyncio
async def test_valid_cache_prevents_weather_only_degraded_mode():
    """Do not suppress other sensor failures or expired weather caches."""
    from custom_components.better_thermostat.utils.watcher import (
        check_and_update_degraded_mode,
    )

    bt = SimpleNamespace(
        hass=MagicMock(),
        sensor_entity_id="sensor.room",
        weather_entity=WEATHER,
        device_name="Room",
        degraded_mode=False,
        unavailable_sensors=[],
        _degraded_grace_until=None,
        _degraded_warning_emitted=False,
        async_write_ha_state=MagicMock(),
    )
    with (
        patch(
            "custom_components.better_thermostat.utils.watcher.get_optional_sensors",
            return_value=[WEATHER],
        ),
        patch(
            "custom_components.better_thermostat.utils.watcher.is_entity_available",
            side_effect=lambda hass, entity: entity != WEATHER,
        ),
        patch(
            "custom_components.better_thermostat.utils.watcher.usable_weather_cache",
            return_value=[{"temperature": 4.0}],
        ),
        patch(
            "custom_components.better_thermostat.utils.watcher.schedule_battery_refresh"
        ),
    ):
        assert await check_and_update_degraded_mode(bt) is False
        assert bt.unavailable_sensors == []

    with (
        patch(
            "custom_components.better_thermostat.utils.watcher.get_optional_sensors",
            return_value=[WEATHER],
        ),
        patch(
            "custom_components.better_thermostat.utils.watcher.is_entity_available",
            side_effect=lambda hass, entity: entity != WEATHER,
        ),
        patch(
            "custom_components.better_thermostat.utils.watcher.usable_weather_cache",
            return_value=[],
        ),
        patch(
            "custom_components.better_thermostat.utils.watcher.ir.async_create_issue"
        ),
        patch(
            "custom_components.better_thermostat.utils.helpers.async_fire_logbook_entry",
        ),
    ):
        assert await check_and_update_degraded_mode(bt) is True
        assert WEATHER in bt.unavailable_sensors
