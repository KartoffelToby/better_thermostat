"""Tests for the core WorldSnapshot type and the shell-side builder.

The completeness table pins that every entity attribute the control path
reads today has a corresponding snapshot field and that ``build_snapshot``
copies each one. A forgotten field would surface here.
"""

from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.snapshot import (
    HvacMode,
    TrvReported,
    WorldSnapshot,
    parse_hvac_mode,
)
from custom_components.better_thermostat.utils.snapshot import build_snapshot


def _make_bt() -> MagicMock:
    """Return a fully populated BetterThermostat stand-in."""
    bt = MagicMock()
    bt.clock = FakeClock(
        monotonic_value=1234.5, now_value=datetime(2026, 1, 2, 8, 30, tzinfo=UTC)
    )
    bt.bt_target_temp = 21.5
    bt.bt_target_cooltemp = 24.0
    bt.bt_hvac_mode = "heat"
    bt.cur_temp = 20.1
    bt.cur_temp_filtered = 20.2
    bt.temp_slope = 0.05
    bt.window_open = False
    bt.call_for_heat = True
    bt.preset_mode = "eco"
    bt.tolerance = 0.3
    bt.outdoor_sensor = None
    bt.weather_entity = None
    bt.startup_running = False
    bt.in_maintenance = False
    bt.ignore_states = False
    bt.degraded_mode = False
    bt.bt_min_temp = 5.0
    bt.bt_max_temp = 30.0
    bt.real_trvs = {
        "climate.trv": {
            "hvac_mode": "heat",
            "current_temperature": 21.0,
            "last_temperature": 22.0,
            "min_temp": 5.0,
            "max_temp": 30.0,
            "valve_max_opening": 80.0,
        }
    }
    trv_state = MagicMock()
    trv_state.state = "heat"
    bt.hass.states.get.return_value = trv_state
    return bt


# Entity attribute -> snapshot field, value from _make_bt.
COMPLETENESS_TABLE = [
    ("bt_target_temp", "target_temp", 21.5),
    ("bt_target_cooltemp", "target_cooltemp", 24.0),
    ("bt_hvac_mode", "hvac_mode", HvacMode.HEAT),
    ("cur_temp", "room_temp", 20.1),
    ("cur_temp_filtered", "room_temp_filtered", 20.2),
    ("temp_slope", "temp_slope", 0.05),
    ("window_open", "window_open", False),
    ("call_for_heat", "call_for_heat", True),
    ("preset_mode", "preset_mode", "eco"),
    ("tolerance", "tolerance", 0.3),
    ("startup_running", "startup_running", False),
    ("in_maintenance", "in_maintenance", False),
    ("ignore_states", "ignore_states", False),
    ("degraded_mode", "degraded", False),
    ("bt_min_temp", "min_temp", 5.0),
    ("bt_max_temp", "max_temp", 30.0),
]


class TestSnapshotCompleteness:
    """Every control-path input is mapped onto a snapshot field."""

    @pytest.mark.parametrize(
        ("entity_attr", "snapshot_field", "expected"), COMPLETENESS_TABLE
    )
    def test_field_is_copied(self, entity_attr, snapshot_field, expected):
        """build_snapshot copies the entity attribute into the snapshot."""
        bt = _make_bt()
        snapshot = build_snapshot(bt)
        assert getattr(snapshot, snapshot_field) == expected

    def test_no_snapshot_field_is_unmapped(self):
        """Each WorldSnapshot field is produced by the builder (none forgotten)."""
        mapped = {snapshot_field for _, snapshot_field, _ in COMPLETENESS_TABLE}
        produced_elsewhere = {
            "now",
            "now_monotonic",
            "outdoor_temp",
            "is_day",
            "solar_intensity",
            "trvs",
        }
        all_fields = {f.name for f in fields(WorldSnapshot)}
        assert all_fields == mapped | produced_elsewhere

    def test_time_comes_from_the_injected_clock(self):
        """The snapshot carries both clock axes at build time."""
        bt = _make_bt()
        snapshot = build_snapshot(bt)
        assert snapshot.now == datetime(2026, 1, 2, 8, 30, tzinfo=UTC)
        assert snapshot.now_monotonic == 1234.5


class TestTrvReportedBuilding:
    """The TRV part is condensed into typed TrvReported entries."""

    def test_reported_values_are_copied(self):
        """All reported TRV values land in the typed structure."""
        bt = _make_bt()
        snapshot = build_snapshot(bt)
        trv = snapshot.trvs["climate.trv"]
        assert trv == TrvReported(
            entity_id="climate.trv",
            available=True,
            hvac_mode=HvacMode.HEAT,
            current_temp=21.0,
            setpoint=22.0,
            min_temp=5.0,
            max_temp=30.0,
            valve_max_opening=80.0,
        )

    def test_unavailable_state_marks_trv_unavailable(self):
        """An unavailable HA state yields available=False."""
        bt = _make_bt()
        trv_state = MagicMock()
        trv_state.state = "unavailable"
        bt.hass.states.get.return_value = trv_state
        snapshot = build_snapshot(bt)
        assert snapshot.trvs["climate.trv"].available is False

    def test_missing_state_marks_trv_unavailable(self):
        """A missing HA state yields available=False."""
        bt = _make_bt()
        bt.hass.states.get.return_value = None
        snapshot = build_snapshot(bt)
        assert snapshot.trvs["climate.trv"].available is False

    def test_unparseable_values_become_none(self):
        """Garbage in the real_trvs entry degrades to None, not a crash."""
        bt = _make_bt()
        bt.real_trvs["climate.trv"]["current_temperature"] = "oops"
        bt.real_trvs["climate.trv"]["hvac_mode"] = "bogus"
        snapshot = build_snapshot(bt)
        trv = snapshot.trvs["climate.trv"]
        assert trv.current_temp is None
        assert trv.hvac_mode is None


class TestWorldSnapshotType:
    """Type-level guarantees of the snapshot."""

    def test_snapshot_is_frozen(self):
        """Snapshot fields cannot be reassigned."""
        snapshot = build_snapshot(_make_bt())
        with pytest.raises(FrozenInstanceError):
            snapshot.room_temp = 99.0

    def test_trv_reported_is_frozen(self):
        """TrvReported fields cannot be reassigned."""
        trv = TrvReported(entity_id="climate.trv")
        with pytest.raises(FrozenInstanceError):
            trv.current_temp = 99.0


class TestParseHvacMode:
    """parse_hvac_mode maps raw values onto the core vocabulary."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("heat", HvacMode.HEAT),
            ("off", HvacMode.OFF),
            ("cool", HvacMode.COOL),
            ("heat_cool", HvacMode.HEAT_COOL),
            ("auto", HvacMode.AUTO),
            (None, None),
            ("bogus", None),
        ],
    )
    def test_parse(self, raw, expected):
        """Known strings map to members, unknown to None."""
        assert parse_hvac_mode(raw) == expected

    def test_members_compare_equal_to_ha_strings(self):
        """Core values are HA's mode strings, so equality is interoperable."""
        assert HvacMode.OFF == "off"
        assert HvacMode.HEAT == "heat"
