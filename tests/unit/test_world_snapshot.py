"""Tests for the core WorldSnapshot type and the shell-side builder.

The completeness table pins that every entity attribute the control path
reads today has a corresponding snapshot field and that ``build_snapshot``
copies each one. A forgotten field would surface here, not deep in M2.
"""

from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime
from unittest.mock import MagicMock

from homeassistant.core import State
import pytest

from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.snapshot import (
    HvacMode,
    TrvReported,
    WorldSnapshot,
    parse_hvac_mode,
)
from custom_components.better_thermostat.model_fixes import ZWA021
from custom_components.better_thermostat.utils.const import CalibrationOutput
from custom_components.better_thermostat.utils.snapshot import build_snapshot
from tests.factories import ThermostatStandIn, trv_from_legacy_dict


def _make_bt() -> MagicMock:
    """Return a fully populated BetterThermostat stand-in."""
    bt = ThermostatStandIn()
    bt.window_sensor_entity_id = None
    bt.device_name = "Test BT"
    bt.clock = FakeClock(
        monotonic_value=1234.5, now_value=datetime(2026, 1, 2, 8, 30, tzinfo=UTC)
    )
    bt.heat_target_temperature = 21.5
    bt.cool_target_temperature = 24.0
    bt.bt_hvac_mode = "heat"
    bt.room_temperature = 20.1
    bt.room_temperature_filtered = 20.2
    bt.temperature_slope = 0.05
    bt.window_open = False
    bt.call_for_heat = True
    bt.preset_mode = "eco"
    bt.tolerance = 0.3
    bt.outdoor_sensor_entity_id = None
    bt.weather_entity_id = None
    bt.startup_running = False
    bt.in_maintenance = False
    bt.ignore_states = False
    bt.degraded_mode = False
    bt.bt_min_temp = 5.0
    bt.bt_max_temp = 30.0
    bt.real_trvs = {
        "climate.trv": trv_from_legacy_dict(
            "climate.trv",
            {
                "hvac_mode": "heat",
                "current_temperature": 21.0,
                "commanded_setpoint": 22.0,
                "min_temp": 5.0,
                "max_temp": 30.0,
                "valve_max_opening": 80.0,
            },
        )
    }
    bt.hass.states.get.return_value = State("climate.trv", "heat")
    return bt


# Entity attribute -> snapshot field, value from _make_bt.
COMPLETENESS_TABLE = [
    ("heat_target_temperature", "heat_target_temperature", 21.5),
    ("cool_target_temperature", "cool_target_temperature", 24.0),
    ("bt_hvac_mode", "hvac_mode", HvacMode.HEAT),
    ("room_temperature", "room_temperature", 20.1),
    ("room_temperature_filtered", "room_temperature_filtered", 20.2),
    ("temperature_slope", "temperature_slope", 0.05),
    ("call_for_heat", "call_for_heat", True),
    ("preset_mode", "preset_mode", "eco"),
    ("tolerance", "tolerance", 0.3),
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
            "outdoor_temperature",
            "is_day",
            "solar_intensity",
            "trvs",
            # Raw sensor reading, read straight from hass in the builder.
            "window_open",
        }
        all_fields = {f.name for f in fields(WorldSnapshot)}
        assert all_fields == mapped | produced_elsewhere

    def test_observations_use_the_shared_float_normalization(self):
        """Raw readings pass the shared converter (0.01 step).

        The snapshot carries the same numbers the rest of BT computes
        with.
        """
        bt = _make_bt()
        bt.room_temperature = 19.974999
        snapshot = build_snapshot(bt)
        assert snapshot.room_temperature == 19.97

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_observations_become_none(self, bad):
        """NaN/inf readings are rejected at the snapshot boundary."""
        bt = _make_bt()
        bt.room_temperature = bad
        snapshot = build_snapshot(bt)
        assert snapshot.room_temperature is None

    def test_time_comes_from_the_injected_clock(self):
        """The snapshot carries both clock axes at build time."""
        bt = _make_bt()
        snapshot = build_snapshot(bt)
        assert snapshot.now == datetime(2026, 1, 2, 8, 30, tzinfo=UTC)
        assert snapshot.now_monotonic == 1234.5


class TestRawWindowState:
    """The window sensor's own reading, before any delay, enters the snapshot."""

    WINDOW_ID = "binary_sensor.window"

    def _snapshot_with_window(self, window_state: State | None):
        bt = _make_bt()
        bt.window_sensor_entity_id = self.WINDOW_ID
        trv_state = State("climate.trv", "heat")
        bt.hass.states.get.side_effect = lambda entity_id: (
            window_state if entity_id == self.WINDOW_ID else trv_state
        )
        return build_snapshot(bt)

    def test_no_window_sensor_reads_as_unknown(self):
        """Without a configured sensor the snapshot carries no window reading."""
        bt = _make_bt()
        bt.window_sensor_entity_id = None
        assert build_snapshot(bt).window_open is None

    @pytest.mark.parametrize(
        "window_state",
        [None, State(WINDOW_ID, "unavailable"), State(WINDOW_ID, "unknown")],
        ids=["missing", "unavailable", "unknown"],
    )
    def test_a_sensor_without_a_reading_reads_as_unknown(self, window_state):
        """A sensor that is gone or reports no state gives no window reading."""
        assert self._snapshot_with_window(window_state).window_open is None

    @pytest.mark.parametrize(
        ("reported", "expected"),
        [("on", True), ("open", True), ("off", False), ("closed", False)],
    )
    def test_a_sensor_reading_is_carried(self, reported, expected):
        """A sensor that reports a state gives its open or closed reading."""
        window_state = State(self.WINDOW_ID, reported)
        assert self._snapshot_with_window(window_state).window_open is expected


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
            current_temperature=21.0,
            setpoint=22.0,
            min_temp=5.0,
            max_temp=30.0,
            valve_max_opening=80.0,
            min_local_calibration=-7,
            max_local_calibration=7,
        )

    def test_unavailable_state_marks_trv_unavailable(self):
        """An unavailable HA state yields available=False."""
        bt = _make_bt()
        bt.hass.states.get.return_value = State("climate.trv", "unavailable")
        snapshot = build_snapshot(bt)
        assert snapshot.trvs["climate.trv"].available is False

    def test_missing_state_marks_trv_unavailable(self):
        """A missing HA state yields available=False."""
        bt = _make_bt()
        bt.hass.states.get.return_value = None
        snapshot = build_snapshot(bt)
        assert snapshot.trvs["climate.trv"].available is False

    def test_unknown_state_marks_trv_unavailable(self):
        """An entity saying nothing leaves its device unaccounted for."""
        bt = _make_bt()
        bt.hass.states.get.return_value = State("climate.trv", "unknown")
        snapshot = build_snapshot(bt)
        assert snapshot.trvs["climate.trv"].available is False

    def test_a_model_that_reports_unknown_while_driven_stays_available(self):
        """Addressing drops an unavailable TRV, so this one has to be present.

        A TRV driven through a mode its climate entity does not describe
        reports ``unknown`` for as long as that mode holds. Read as the
        absence it means everywhere else, the device would be dropped from
        the addressed set and never written to again.
        """
        bt = _make_bt()
        bt.real_trvs["climate.trv"].model_quirks = ZWA021
        bt.real_trvs["climate.trv"].advanced = {
            "calibration": CalibrationOutput.DIRECT_VALVE_BASED
        }
        bt.hass.states.get.return_value = State("climate.trv", "unknown")
        snapshot = build_snapshot(bt)
        assert snapshot.trvs["climate.trv"].available is True

    def test_a_trv_awaiting_initialization_is_not_part_of_the_room(self):
        """A TRV startup went ahead without is not addressed before it is set up.

        Its capabilities, bounds and setpoint have not been read yet, and boost
        addresses a TRV whatever its availability, so an entry reading
        ``available=False`` would still let a boost write reach it.
        """
        bt = _make_bt()
        bt.real_trvs["climate.trv"].awaiting_initialization = True
        snapshot = build_snapshot(bt)
        assert "climate.trv" not in snapshot.trvs

    def test_unparseable_values_become_none(self):
        """Garbage in the real_trvs entry degrades to None, not a crash."""
        bt = _make_bt()
        bt.real_trvs["climate.trv"].current_temperature = "oops"
        bt.real_trvs["climate.trv"].hvac_mode = "bogus"
        snapshot = build_snapshot(bt)
        trv = snapshot.trvs["climate.trv"]
        assert trv.current_temperature is None
        assert trv.hvac_mode is None

    def test_non_finite_reported_values_become_none(self):
        """NaN/inf in the real_trvs entry degrades to None, not a crash."""
        bt = _make_bt()
        bt.real_trvs["climate.trv"].current_temperature = float("nan")
        bt.real_trvs["climate.trv"].commanded_setpoint = float("inf")
        snapshot = build_snapshot(bt)
        trv = snapshot.trvs["climate.trv"]
        assert trv.current_temperature is None
        assert trv.setpoint is None


class TestWorldSnapshotType:
    """Type-level guarantees of the snapshot."""

    def test_snapshot_is_frozen(self):
        """Snapshot fields cannot be reassigned."""
        snapshot = build_snapshot(_make_bt())
        field_name = "room_temperature"
        with pytest.raises(FrozenInstanceError):
            setattr(snapshot, field_name, 99.0)

    def test_trv_reported_is_frozen(self):
        """TrvReported fields cannot be reassigned."""
        trv = TrvReported(entity_id="climate.trv")
        field_name = "current_temperature"
        with pytest.raises(FrozenInstanceError):
            setattr(trv, field_name, 99.0)


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
