"""Tests for the core WorldSnapshot type and the shell-side builder.

The completeness table pins that every entity attribute the control path
reads today has a corresponding snapshot field and that ``build_snapshot``
copies each one. A forgotten field would surface here, not deep in M2.
"""

from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import State
from homeassistant.helpers import device_registry as dr
import pytest

from custom_components.better_thermostat.adapters import deconz, delegate, generic, tado
from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.snapshot import (
    HvacMode,
    ObserverReading,
    ThermalEvidence,
    TrvReported,
    WorldSnapshot,
    parse_hvac_mode,
)
from custom_components.better_thermostat.model_fixes import COZB0001, ZWA021, Heater
from custom_components.better_thermostat.utils import helpers
from custom_components.better_thermostat.utils.const import CalibrationOutput
from custom_components.better_thermostat.utils.helpers import (
    REMOTE_SENSING_LOCAL_TEMPERATURE_KEY,
)
from custom_components.better_thermostat.utils.snapshot import build_snapshot
from tests.factories import (
    ThermostatStandIn,
    make_entity_registry,
    make_registry_entry,
    trv_record,
)


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
    bt.devices_released = True
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
        "climate.trv": trv_record(
            entity_id="climate.trv",
            hvac_mode="heat",
            current_temperature=21.0,
            commanded_setpoint=22.0,
            min_temp=5.0,
            max_temp=30.0,
            valve_max_opening=80.0,
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
    ("devices_released", "devices_released", True),
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
            # Continuous residual and observer evidence, assembled separately.
            "thermal_evidence",
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
            raw_temperature=21.0,
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

    def test_thermal_evidence_and_observer_reading_are_frozen(self):
        """ObserverReading and ThermalEvidence fields cannot be reassigned."""
        observer = ObserverReading(
            age_seconds=60.0,
            normalised_innovation=-1.2,
            disturbance_kelvin_per_min=-0.01,
            disturbance_at_bound=False,
            room_time_constant_minutes=120.0,
            disturbance_baseline_kelvin_per_min=0.0,
        )
        evidence = ThermalEvidence(observer=observer)
        with pytest.raises(FrozenInstanceError):
            setattr(observer, "age_seconds", 10.0)
        with pytest.raises(FrozenInstanceError):
            setattr(evidence, "slope_residual_kelvin_per_min", -0.1)


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


class TestReleasedTrvs:
    """The snapshot says which TRVs the user has released on their own."""

    @staticmethod
    def _room_of_two(*, released: set[str], awaiting: set[str]) -> MagicMock:
        bt = _make_bt()
        bt.devices_released = False
        bt.real_trvs = {
            entity_id: trv_record(
                entity_id=entity_id, awaiting_initialization=entity_id in awaiting
            )
            for entity_id in ("climate.trv", "climate.other")
        }
        bt.released_trvs = released
        return bt

    def test_each_trv_carries_its_own_release(self):
        """Only the released TRV reads as released."""
        bt = self._room_of_two(released={"climate.trv"}, awaiting=set())
        snapshot = build_snapshot(bt)
        assert {
            entity_id: trv.released for entity_id, trv in snapshot.trvs.items()
        } == {"climate.trv": True, "climate.other": False}

    def test_a_released_trv_not_yet_set_up_stays_part_of_the_room(self):
        """A TRV awaiting its setup is left out, unless it is released.

        A released TRV is never addressed, so it cannot be commanded with
        values its setup has not read yet; the kernel reads from it that
        the room has a TRV released.
        """
        bt = self._room_of_two(
            released={"climate.trv"}, awaiting={"climate.trv", "climate.other"}
        )
        snapshot = build_snapshot(bt)
        assert set(snapshot.trvs) == {"climate.trv"}
        assert snapshot.trvs["climate.trv"].released is True


class TestTrvRawTemperature:
    """``TrvReported.raw_temperature`` recovers the physical TRV reading across adapters."""

    @staticmethod
    def _adapter_bt(adapter_module) -> MagicMock:
        bt = _make_bt()
        bt.devices_released = False
        bt.context = None
        bt.hass.services.async_call = AsyncMock(return_value=None)
        trv = bt.real_trvs["climate.trv"]
        trv.adapter = adapter_module
        trv.calibration = CalibrationOutput.LOCAL_BASED
        trv.local_temperature_calibration_entity = "number.trv_calibration"
        states = {
            "climate.trv": State("climate.trv", "heat"),
            "number.trv_calibration": State(
                "number.trv_calibration", "0.0", {"min": -6, "max": 6, "step": 0.1}
            ),
        }
        bt.hass.states.get.side_effect = states.get
        return bt

    @pytest.mark.parametrize(
        ("adapter_module", "expected_domain", "expected_wire"),
        [
            pytest.param(generic, "number", -4.0, id="generic"),
            pytest.param(deconz, "deconz", -400, id="deconz"),
            pytest.param(tado, "tado", -4.0, id="tado"),
        ],
    )
    @pytest.mark.asyncio
    async def test_local_offset_sign_recovers_physical_temperature_per_adapter(
        self, adapter_module, expected_domain, expected_wire
    ):
        """After an adapter writes a local calibration offset, raw_temperature matches the unshifted sensor."""
        physical_temperature = 26.4
        calibration_offset = -4.0
        bt = self._adapter_bt(adapter_module)
        registry = make_entity_registry(
            make_registry_entry("climate.trv"),
            make_registry_entry("number.trv_calibration"),
        )

        with patch.object(helpers.er, "async_get", return_value=registry):
            wrote = await delegate.set_calibration_offset(
                bt, "climate.trv", calibration_offset
            )

        assert wrote is True
        call = bt.hass.services.async_call.await_args
        assert call is not None
        assert call.args[0] == expected_domain
        if expected_domain == "deconz":
            assert call.args[2]["data"]["offset"] == expected_wire
        elif expected_domain == "tado":
            assert call.args[2]["offset"] == expected_wire
        else:
            assert call.args[2]["value"] == expected_wire

        # Standard devices add BT's offset to the physical reading they measure.
        bt.real_trvs["climate.trv"].current_temperature = (
            physical_temperature + calibration_offset
        )
        snapshot = build_snapshot(bt)
        assert snapshot.trvs["climate.trv"].raw_temperature == pytest.approx(
            physical_temperature
        )

    @pytest.mark.asyncio
    async def test_inverted_offset_quirk_recovers_physical_temperature(self):
        """TermoWeb heaters subtract the negated wire offset, so raw_temperature still recovers the sensor."""
        physical_temperature = 26.4
        calibration_offset = -4.0
        bt = self._adapter_bt(generic)
        bt.real_trvs["climate.trv"].model_quirks = Heater
        registry = make_entity_registry(
            make_registry_entry("climate.trv", device_id="device_termoweb"),
            make_registry_entry("number.trv_calibration", device_id="device_termoweb"),
        )
        device_registry = MagicMock()
        device_registry.async_get.return_value = MagicMock(
            spec=dr.DeviceEntry, manufacturer="TermoWeb"
        )

        with (
            patch.object(helpers.er, "async_get", return_value=registry),
            patch.object(Heater.er, "async_get", return_value=registry),
            patch.object(Heater.dr, "async_get", return_value=device_registry),
        ):
            wrote = await delegate.set_calibration_offset(
                bt, "climate.trv", calibration_offset
            )
            call = bt.hass.services.async_call.await_args
            assert wrote is True
            assert call is not None
            device_wire_offset = call.args[2]["value"]
            assert device_wire_offset == 4.0
            # TermoWeb subtracts the wire offset from its physical measurement.
            bt.real_trvs["climate.trv"].current_temperature = (
                physical_temperature - device_wire_offset
            )
            snapshot = build_snapshot(bt)

        assert snapshot.trvs["climate.trv"].raw_temperature == pytest.approx(
            physical_temperature
        )

    def test_setpoint_offset_quirk_keeps_unshifted_reading(self):
        """COZB0001 shifts its setpoint and reports the bare physical reading."""
        bt = _make_bt()
        trv = bt.real_trvs["climate.trv"]
        trv.calibration = CalibrationOutput.LOCAL_BASED
        trv.model_quirks = COZB0001
        trv.current_temperature = 26.4
        trv.last_calibration = 4.0

        snapshot = build_snapshot(bt)
        assert snapshot.trvs["climate.trv"].raw_temperature == pytest.approx(26.4)

    @pytest.mark.parametrize(
        ("remote_sensing", "expected"),
        [(STATE_ON, None), (STATE_UNAVAILABLE, None), (STATE_OFF, 26.4)],
    )
    def test_bound_sensor_trv_omits_raw_temperature_unless_unbound(
        self, remote_sensing, expected
    ):
        """A Matter TRV regulating on a remote room sensor exposes no radiator temperature."""
        bt = _make_bt()
        trv = bt.real_trvs["climate.trv"]
        trv.calibration = CalibrationOutput.LOCAL_BASED
        trv.current_temperature = 22.4
        trv.last_calibration = -4.0
        remote_id = "binary_sensor.trv_remote_sensing"
        registry = make_entity_registry(
            make_registry_entry(
                "climate.trv", platform="matter", device_id="device_matter"
            ),
            make_registry_entry(
                remote_id,
                platform="matter",
                device_id="device_matter",
                translation_key=REMOTE_SENSING_LOCAL_TEMPERATURE_KEY,
            ),
        )
        states = {
            "climate.trv": State("climate.trv", "heat"),
            remote_id: State(remote_id, remote_sensing),
        }
        bt.hass.states.get.side_effect = states.get

        with patch.object(helpers.er, "async_get", return_value=registry):
            snapshot = build_snapshot(bt)

        assert snapshot.trvs["climate.trv"].raw_temperature == pytest.approx(expected)

    @pytest.mark.parametrize(
        "calibration_output",
        [
            None,
            CalibrationOutput.TARGET_TEMP_BASED,
            CalibrationOutput.DIRECT_VALVE_BASED,
        ],
    )
    def test_non_offset_channels_report_current_temperature_directly(
        self, calibration_output
    ):
        """Setpoint and direct-valve channels leave the TRV's own sensor reading unshifted."""
        bt = _make_bt()
        trv = bt.real_trvs["climate.trv"]
        trv.calibration = calibration_output
        trv.current_temperature = 24.5
        trv.last_calibration = -3.0

        snapshot = build_snapshot(bt)
        assert snapshot.trvs["climate.trv"].raw_temperature == 24.5

    def test_advanced_local_based_fallback_and_non_finite_calibration(self):
        """``advanced['calibration']`` selects offset recovery when ``trv.calibration`` is unset, and NaN yields None."""
        bt = _make_bt()
        trv = bt.real_trvs["climate.trv"]
        trv.calibration = None
        trv.advanced = {"calibration": CalibrationOutput.LOCAL_BASED}
        trv.current_temperature = 21.0
        trv.last_calibration = -2.5
        assert build_snapshot(bt).trvs["climate.trv"].raw_temperature == 23.5

        trv.last_calibration = float("nan")
        assert build_snapshot(bt).trvs["climate.trv"].raw_temperature is None

    def test_snapshot_without_hass_recovers_raw_temperature(self):
        """An unattached entity with ``hass=None`` marks TRVs unavailable without failing."""
        bt = _make_bt()
        bt.hass = None
        snapshot = build_snapshot(bt)
        assert snapshot.trvs["climate.trv"].available is False
        assert snapshot.trvs["climate.trv"].raw_temperature == 21.0


class TestTrvValvePercent:
    """``TrvReported.valve_percent`` prefers reported position and falls back to last write."""

    @pytest.mark.parametrize(
        ("reported_position", "written_percent", "expected"),
        [
            pytest.param(35.0, 80.0, 35.0, id="reported-wins"),
            pytest.param(None, 65.0, 65.0, id="fallback-to-last-write"),
            pytest.param(-1.0, 50.0, 50.0, id="negative-reported-falls-back"),
            pytest.param(105.0, 40.0, 40.0, id="over-100-reported-falls-back"),
            pytest.param(float("nan"), 25.0, 25.0, id="nan-reported-falls-back"),
            pytest.param(None, 120.0, None, id="both-invalid"),
            pytest.param(None, None, None, id="both-absent"),
        ],
    )
    def test_valve_percent_selection(
        self, reported_position, written_percent, expected
    ):
        """Valid openings in ``[0, 100]`` enter the snapshot in priority order."""
        bt = _make_bt()
        trv = bt.real_trvs["climate.trv"]
        trv.valve_position = reported_position
        trv.last_valve_percent = written_percent

        snapshot = build_snapshot(bt)
        assert snapshot.trvs["climate.trv"].valve_percent == expected
