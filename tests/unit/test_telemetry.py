"""Tests for utils/telemetry.py — collect_cycle/balance/pid_debug helpers."""

import json

from custom_components.better_thermostat.utils.const import CalibrationMode
from custom_components.better_thermostat.utils.telemetry import (
    TELEMETRY_ATTRIBUTES,
    collect_balance_attrs,
    collect_cycle_telemetry,
    collect_mpc_v2_debug_attrs,
    collect_pid_debug_attrs,
)
from tests.factories import ThermostatStandIn, make_balance, trv_from_legacy_dict

# ---------------------------------------------------------------------------
# collect_cycle_telemetry
# ---------------------------------------------------------------------------


class TestCollectCycleTelemetry:
    """Cycle telemetry covers heating cycles, loss cycles, heat loss stats, normalized power."""

    def _bt(self, **overrides):
        """BT mock with all Protocol-required attrs set to safe defaults."""
        bt = ThermostatStandIn()
        bt.heating_cycles = None
        bt.loss_cycles = None
        bt.last_heat_loss_stats = None
        bt.heating_power_normalized = None
        bt.__dict__.update(overrides)
        return bt

    def test_minimal_state_emits_only_normalized_power(self):
        """All empty/None — only heating_power_normalized passes through."""
        out = collect_cycle_telemetry(self._bt())
        assert out == {"heating_power_normalized": None}

    def test_heating_cycle_count_and_last(self):
        """Heating cycles surface count and serialised last entry."""
        cycles = [{"a": 1}, {"b": 2}, {"c": 3}]
        out = collect_cycle_telemetry(self._bt(heating_cycles=cycles))
        assert out["heating_cycle_count"] == 3
        assert out["heating_cycle_last"] == json.dumps({"c": 3})

    def test_loss_cycle_count_and_last(self):
        """Loss cycles surface count and serialised last entry."""
        out = collect_cycle_telemetry(self._bt(loss_cycles=[{"x": 1}, {"y": 2}]))
        assert out["heat_loss_cycle_count"] == 2
        assert out["heat_loss_cycle_last"] == json.dumps({"y": 2})

    def test_heat_loss_stats_serialized(self):
        """The full heat-loss stats list is emitted as JSON."""
        out = collect_cycle_telemetry(
            self._bt(last_heat_loss_stats=[{"loss": 0.1}, {"loss": 0.2}])
        )
        assert out["heat_loss_stats"] == json.dumps([{"loss": 0.1}, {"loss": 0.2}])

    def test_normalized_power_passthrough(self):
        """A numeric heating_power_normalized value is forwarded verbatim."""
        out = collect_cycle_telemetry(self._bt(heating_power_normalized=0.42))
        assert out["heating_power_normalized"] == 0.42

    def test_normalized_power_none_kept(self):
        """None still surfaces as a value (not filtered)."""
        out = collect_cycle_telemetry(self._bt(heating_power_normalized=None))
        assert "heating_power_normalized" in out
        assert out["heating_power_normalized"] is None


# ---------------------------------------------------------------------------
# collect_balance_attrs
# ---------------------------------------------------------------------------


class TestCollectBalanceAttrs:
    """Slope + per-TRV calibration balance summary."""

    def test_empty_when_no_slope_no_balance(self):
        """Nothing is emitted when both slope and per-TRV balance are absent."""
        bt = ThermostatStandIn()
        bt.temperature_slope = None
        bt.real_trvs = {}
        out = collect_balance_attrs(bt)
        assert out == {}

    def test_slope_rounded_to_4_decimals(self):
        """temperature_slope is rounded to 4 decimal places for readability."""
        bt = ThermostatStandIn()
        bt.temperature_slope = 0.001234567
        bt.real_trvs = {}
        out = collect_balance_attrs(bt)
        assert out["temperature_slope_kelvin_per_min"] == 0.0012

    def test_balance_aggregated_across_trvs(self):
        """Per-TRV calibration balance is collected into one JSON map."""
        bt = ThermostatStandIn()
        bt.temperature_slope = None
        bt.real_trvs = {
            "climate.a": trv_from_legacy_dict(
                "climate.a", {"calibration_balance": {"valve_percent": 70, "extra": 1}}
            ),
            "climate.b": trv_from_legacy_dict(
                "climate.b", {"calibration_balance": {"valve_percent": 30}}
            ),
        }
        out = collect_balance_attrs(bt)
        parsed = json.loads(out["calibration_balance"])
        assert parsed == {"climate.a": {"valve%": 70}, "climate.b": {"valve%": 30}}

    def test_trv_without_balance_skipped(self):
        """TRVs with missing or None balance are skipped, not serialised."""
        bt = ThermostatStandIn()
        bt.temperature_slope = None
        bt.real_trvs = {
            "climate.a": trv_from_legacy_dict(
                "climate.a", {"calibration_balance": {"valve_percent": 50}}
            ),
            "climate.b": trv_from_legacy_dict("climate.b", {}),
            "climate.c": trv_from_legacy_dict(
                "climate.c", {"calibration_balance": None}
            ),
        }
        out = collect_balance_attrs(bt)
        parsed = json.loads(out["calibration_balance"])
        assert parsed == {"climate.a": {"valve%": 50}}


# ---------------------------------------------------------------------------
# collect_pid_debug_attrs
# ---------------------------------------------------------------------------


def _bt_with_pid(trvs, real_trv_entries):
    """Build a mock BT with PID-bearing real_trvs."""
    bt = ThermostatStandIn()
    bt.real_trvs = {
        entity_id: trv_from_legacy_dict(entity_id, entry)
        for entity_id, entry in zip(trvs, real_trv_entries)
    }
    return bt


class TestCollectPidDebugAttrs:
    """PID controller debug flattening — only emits when mode == 'pid'."""

    def test_empty_when_no_trvs(self):
        """Nothing is emitted when real_trvs is empty."""
        bt = ThermostatStandIn()
        bt.real_trvs = {}
        out = collect_pid_debug_attrs(bt)
        assert out == {}

    def test_empty_when_mode_not_pid(self):
        """Non-PID controllers (e.g. mpc) suppress PID debug output."""
        bt = _bt_with_pid(
            ["climate.a"],
            [
                {
                    "model": "generic",
                    "calibration_balance": make_balance(
                        CalibrationMode.MPC_CALIBRATION, {"mpc_gain": 0.05}
                    ),
                }
            ],
        )
        out = collect_pid_debug_attrs(bt)
        assert out == {}

    def test_emits_pid_fields_for_pid_mode(self):
        """PID mode flattens all scalar debug fields with proper rounding."""
        bt = _bt_with_pid(
            ["climate.a"],
            [
                {
                    "model": "generic",
                    "calibration_balance": make_balance(
                        CalibrationMode.PID_CALIBRATION,
                        {
                            "mode": "pid",
                            "e_K": 0.12345,
                            "p": 0.5,
                            "i": 0.25,
                            "d": 0.1,
                            "u": 0.85,
                            "kp": 0.0123456,
                            "ki": 0.000789,
                            "kd": 0.0000012,
                            "meas_smooth_C": 19.875,
                            "d_meas_per_s": 0.001,
                            "dt_s": 30.123,
                        },
                    ),
                }
            ],
        )
        out = collect_pid_debug_attrs(bt)
        assert (
            out["pid_error_kelvin"] == 0.1235
        )  # 0.12345 → IEEE-754 rounds up at 4 decimals
        assert out["pid_P"] == 0.5
        assert out["pid_I"] == 0.25
        assert out["pid_D"] == 0.1
        assert out["pid_u"] == 0.85
        assert out["pid_kp"] == 0.012346
        assert out["pid_ki"] == 0.000789
        assert out["pid_kd"] == 0.000001
        assert out["pid_measurement_filtered"] == 19.875
        assert out["pid_measurement_slope_kelvin_per_min"] == 0.06
        assert out["pid_dt_seconds"] == 30.123

    def test_missing_fields_omitted(self):
        """Fields absent from the debug dict are not emitted as keys."""
        bt = _bt_with_pid(
            ["climate.a"],
            [
                {
                    "model": "generic",
                    "calibration_balance": make_balance(
                        CalibrationMode.PID_CALIBRATION, {"mode": "pid", "e_K": 0.1}
                    ),
                }
            ],
        )
        out = collect_pid_debug_attrs(bt)
        assert out == {"pid_error_kelvin": 0.1}

    def test_non_numeric_field_silently_skipped(self):
        """Non-numeric scalar values are dropped, valid neighbours kept."""
        bt = _bt_with_pid(
            ["climate.a"],
            [
                {
                    "model": "generic",
                    "calibration_balance": make_balance(
                        CalibrationMode.PID_CALIBRATION,
                        {"mode": "pid", "e_K": "not a number", "p": 0.4},
                    ),
                }
            ],
        )
        out = collect_pid_debug_attrs(bt)
        assert "pid_error_kelvin" not in out
        assert out["pid_P"] == 0.4

    def test_prefers_sonoff_or_trvzb_trv(self):
        """When multiple TRVs are present, sonoff/trvzb wins as representative."""
        bt = _bt_with_pid(
            ["climate.a", "climate.b"],
            [
                {
                    "model": "generic",
                    "calibration_balance": make_balance(
                        CalibrationMode.PID_CALIBRATION, {"mode": "pid", "e_K": 1.0}
                    ),
                },
                {
                    "model": "SONOFF TRVZB",
                    "calibration_balance": make_balance(
                        CalibrationMode.PID_CALIBRATION, {"mode": "pid", "e_K": 2.0}
                    ),
                },
            ],
        )
        out = collect_pid_debug_attrs(bt)
        assert out["pid_error_kelvin"] == 2.0

    def test_model_none_does_not_crash(self):
        """A TRV with ``model=None`` must not raise AttributeError on .lower()."""
        bt = _bt_with_pid(
            ["climate.a"],
            [
                {
                    "model": None,
                    "calibration_balance": make_balance(
                        CalibrationMode.PID_CALIBRATION, {"mode": "pid", "e_K": 1.0}
                    ),
                }
            ],
        )
        out = collect_pid_debug_attrs(bt)
        assert out["pid_error_kelvin"] == 1.0

    def test_no_balance_no_emit(self):
        """A TRV without calibration_balance produces no PID output."""
        bt = _bt_with_pid(["climate.a"], [{"model": "generic"}])
        out = collect_pid_debug_attrs(bt)
        assert out == {}


# ---------------------------------------------------------------------------
# Strict JSON in the emitted attributes
# ---------------------------------------------------------------------------


def _reject_constant(constant: str) -> object:
    """Fail the parse on the literals only Python's decoder accepts."""
    raise ValueError(f"not valid JSON: {constant}")


def _parse_as_a_consumer_would(payload: str) -> object:
    """Parse a serialized attribute the way a parser outside Python does."""
    return json.loads(payload, parse_constant=_reject_constant)


class TestNonFiniteValuesStayOutOfTheAttributes:
    """A non-finite sample costs its own attribute, not every reader.

    Python's encoder writes NaN and infinity as bare literals that no other
    JSON parser accepts, so emitting one would leave the whole attribute
    unreadable for dashboards, automations and diagnostics alike.
    """

    def _bt(self, **overrides):
        """BT mock with all Protocol-required attrs set to safe defaults."""
        bt = ThermostatStandIn()
        bt.heating_cycles = None
        bt.loss_cycles = None
        bt.last_heat_loss_stats = None
        bt.heating_power_normalized = None
        bt.temperature_slope = None
        bt.real_trvs = {}
        bt.__dict__.update(overrides)
        return bt

    def test_finite_cycle_parses_outside_python(self):
        """A clean cycle survives a parser that rejects the bare literals."""
        out = collect_cycle_telemetry(self._bt(heating_cycles=[{"slope": 0.25}]))
        assert _parse_as_a_consumer_would(out["heating_cycle_last"]) == {"slope": 0.25}

    def test_non_finite_heating_cycle_is_omitted(self):
        """A NaN in the last heating cycle drops the cycle attributes."""
        out = collect_cycle_telemetry(
            self._bt(heating_cycles=[{"slope": 0.2}, {"slope": float("nan")}])
        )
        assert "heating_cycle_last" not in out
        assert "heating_cycle_count" not in out

    def test_infinite_loss_cycle_is_omitted(self):
        """An infinity in the last loss cycle drops the loss cycle attributes."""
        out = collect_cycle_telemetry(self._bt(loss_cycles=[{"loss": float("inf")}]))
        assert "heat_loss_cycle_last" not in out
        assert "heat_loss_cycle_count" not in out

    def test_non_finite_heat_loss_stat_is_omitted(self):
        """A NaN anywhere in the heat-loss stats drops that attribute."""
        out = collect_cycle_telemetry(
            self._bt(last_heat_loss_stats=[{"loss": 0.1}, {"loss": float("nan")}])
        )
        assert "heat_loss_stats" not in out

    def test_other_attributes_survive_a_non_finite_cycle(self):
        """Only the offending attribute is dropped."""
        out = collect_cycle_telemetry(
            self._bt(
                heating_cycles=[{"slope": float("nan")}],
                loss_cycles=[{"loss": 0.3}],
                heating_power_normalized=0.8,
            )
        )
        assert "heating_cycle_last" not in out
        assert _parse_as_a_consumer_would(out["heat_loss_cycle_last"]) == {"loss": 0.3}
        assert out["heating_power_normalized"] == 0.8

    def test_non_finite_valve_percent_is_omitted(self):
        """A NaN valve percentage drops the calibration balance attribute."""
        bt = self._bt(
            real_trvs={
                "climate.a": trv_from_legacy_dict(
                    "climate.a",
                    {"calibration_balance": {"valve_percent": float("nan")}},
                )
            }
        )
        assert "calibration_balance" not in collect_balance_attrs(bt)

    def test_finite_valve_percent_parses_outside_python(self):
        """A clean balance survives a parser that rejects the bare literals."""
        bt = self._bt(
            real_trvs={
                "climate.a": trv_from_legacy_dict(
                    "climate.a", {"calibration_balance": {"valve_percent": 42}}
                )
            }
        )
        out = collect_balance_attrs(bt)
        assert _parse_as_a_consumer_would(out["calibration_balance"]) == {
            "climate.a": {"valve%": 42}
        }


# ---------------------------------------------------------------------------
# TELEMETRY_ATTRIBUTES
# ---------------------------------------------------------------------------


def _fully_populated_bt(controller: CalibrationMode, debug: dict) -> ThermostatStandIn:
    """Build a stand-in on which every collector emits every key it knows."""
    bt = _bt_with_pid(
        ["climate.a"],
        [
            {
                "model": "generic",
                "calibration_balance": make_balance(
                    controller, debug, valve_percent=40.0
                ),
            }
        ],
    )
    bt.heating_cycles = [{"start": 1.0}]
    bt.loss_cycles = [{"start": 2.0}]
    bt.last_heat_loss_stats = [{"rate": 0.1}]
    bt.heating_power_normalized = 0.5
    bt.temperature_slope = 0.01
    return bt


class TestTelemetryAttributes:
    """The unrecorded set names exactly the keys the collectors write."""

    def test_names_every_key_the_collectors_write(self):
        """A collector key missing from the set would reach the recorder."""
        pid = _fully_populated_bt(
            CalibrationMode.PID_CALIBRATION,
            {
                "mode": "pid",
                **dict.fromkeys(("e_K", "p", "i", "d", "u", "kp", "ki", "kd"), 0.1),
                "meas_smooth_C": 20.0,
                "d_meas_per_s": 0.001,
                "dt_s": 30.0,
            },
        )
        mpc = _fully_populated_bt(
            CalibrationMode.MPC_V2_CALIBRATION,
            {
                "controller_version": "v2",
                **dict.fromkeys(
                    (
                        "T_room_hat",
                        "T_rad_hat",
                        "D_hat_K_per_min",
                        "tau_room_min",
                        "coupling_rad_room",
                        "group_valve_pct",
                        "reid_tau_room",
                        "reid_gain",
                    ),
                    1.0,
                ),
            },
        )
        written = set()
        for bt in (pid, mpc):
            written |= collect_cycle_telemetry(bt).keys()
            written |= collect_balance_attrs(bt).keys()
            written |= collect_pid_debug_attrs(bt).keys()
            written |= collect_mpc_v2_debug_attrs(bt).keys()

        assert written == TELEMETRY_ATTRIBUTES


class TestCollectMpcV2DebugAttrs:
    """MPC v2 diagnostics are published under their glossary names."""

    def test_publishes_each_diagnostic_under_its_name(self):
        """Each debug value lands under its own spelled-out key."""
        bt = _fully_populated_bt(
            CalibrationMode.MPC_V2_CALIBRATION,
            {
                "controller_version": "v2",
                "T_room_hat": 20.5,
                "T_rad_hat": 35.25,
                "D_hat_K_per_min": 0.0123,
                "tau_room_min": 180.5,
                "coupling_rad_room": 0.75,
                "group_valve_pct": 42.5,
                "reid_tau_room": 200.5,
                "reid_gain": 3.25,
            },
        )

        assert collect_mpc_v2_debug_attrs(bt) == {
            "mpc_v2_room_temperature_estimate": 20.5,
            "mpc_v2_radiator_temperature_estimate": 35.25,
            "mpc_v2_disturbance_kelvin_per_min": 0.0123,
            "mpc_v2_tau_room_minutes": 180.5,
            "mpc_v2_radiator_room_coupling": 0.75,
            "mpc_v2_group_valve_percent": 42.5,
            "mpc_v2_reid_tau_room_minutes": 200.5,
            "mpc_v2_reid_gain": 3.25,
        }
