"""Tests for utils/telemetry.py — collect_cycle/balance/pid_debug helpers."""

import json
from unittest.mock import MagicMock

from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.telemetry import (
    collect_balance_attrs,
    collect_cycle_telemetry,
    collect_pid_debug_attrs,
)

# ---------------------------------------------------------------------------
# collect_cycle_telemetry
# ---------------------------------------------------------------------------


class TestCollectCycleTelemetry:
    """Cycle telemetry covers heating cycles, loss cycles, heat loss stats, normalized power."""

    def _bt(self, **overrides):
        """BT mock with all Protocol-required attrs set to safe defaults."""
        bt = MagicMock()
        bt.heating_cycles = None
        bt.loss_cycles = None
        bt.last_heat_loss_stats = None
        bt.heating_power_normalized = None
        bt.__dict__.update(overrides)
        return bt

    def test_minimal_state_emits_only_normalized_power(self):
        """All empty/None — only heating_power_norm passes through."""
        out = collect_cycle_telemetry(self._bt())
        assert out == {"heating_power_norm": None}

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
        assert out["heating_power_norm"] == 0.42

    def test_normalized_power_none_kept(self):
        """None still surfaces as a value (not filtered)."""
        out = collect_cycle_telemetry(self._bt(heating_power_normalized=None))
        assert "heating_power_norm" in out
        assert out["heating_power_norm"] is None


# ---------------------------------------------------------------------------
# collect_balance_attrs
# ---------------------------------------------------------------------------


class TestCollectBalanceAttrs:
    """Slope + per-TRV calibration balance summary."""

    def test_empty_when_no_slope_no_balance(self):
        """Nothing is emitted when both slope and per-TRV balance are absent."""
        bt = MagicMock()
        bt.temp_slope = None
        bt.real_trvs = {}
        out = collect_balance_attrs(bt)
        assert out == {}

    def test_slope_rounded_to_4_decimals(self):
        """temp_slope is rounded to 4 decimal places for readability."""
        bt = MagicMock()
        bt.temp_slope = 0.001234567
        bt.real_trvs = {}
        out = collect_balance_attrs(bt)
        assert out["temp_slope_K_min"] == 0.0012

    def test_balance_aggregated_across_trvs(self):
        """Per-TRV calibration balance is collected into one JSON map."""
        bt = MagicMock()
        bt.temp_slope = None
        bt.real_trvs = {
            "climate.a": Trv.from_legacy_dict(
                "climate.a", {"calibration_balance": {"valve_percent": 70, "extra": 1}}
            ),
            "climate.b": Trv.from_legacy_dict(
                "climate.b", {"calibration_balance": {"valve_percent": 30}}
            ),
        }
        out = collect_balance_attrs(bt)
        parsed = json.loads(out["calibration_balance"])
        assert parsed == {"climate.a": {"valve%": 70}, "climate.b": {"valve%": 30}}

    def test_trv_without_balance_skipped(self):
        """TRVs with missing or None balance are skipped, not serialised."""
        bt = MagicMock()
        bt.temp_slope = None
        bt.real_trvs = {
            "climate.a": Trv.from_legacy_dict(
                "climate.a", {"calibration_balance": {"valve_percent": 50}}
            ),
            "climate.b": Trv.from_legacy_dict("climate.b", {}),
            "climate.c": Trv.from_legacy_dict(
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
    bt = MagicMock()
    bt.real_trvs = {
        trv_id: Trv.from_legacy_dict(trv_id, entry)
        for trv_id, entry in zip(trvs, real_trv_entries)
    }
    return bt


class TestCollectPidDebugAttrs:
    """PID controller debug flattening — only emits when mode == 'pid'."""

    def test_empty_when_no_trvs(self):
        """Nothing is emitted when real_trvs is empty."""
        bt = MagicMock()
        bt.real_trvs = {}
        out = collect_pid_debug_attrs(bt)
        assert out == {}

    def test_empty_when_mode_not_pid(self):
        """Non-PID controller modes (e.g. mpc) suppress PID debug output."""
        bt = _bt_with_pid(
            ["climate.a"],
            [{"model": "generic", "calibration_balance": {"debug": {"mode": "mpc"}}}],
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
                    "calibration_balance": {
                        "debug": {
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
                        }
                    },
                }
            ],
        )
        out = collect_pid_debug_attrs(bt)
        assert out["pid_e_K"] == 0.1235  # 0.12345 → IEEE-754 rounds up at 4 decimals
        assert out["pid_P"] == 0.5
        assert out["pid_I"] == 0.25
        assert out["pid_D"] == 0.1
        assert out["pid_u"] == 0.85
        assert out["pid_kp"] == 0.012346
        assert out["pid_ki"] == 0.000789
        assert out["pid_kd"] == 0.000001
        assert out["pid_meas_smooth_C"] == 19.875
        assert out["pid_d_meas_K_per_min"] == 0.06
        assert out["pid_dt_s"] == 30.123

    def test_missing_fields_omitted(self):
        """Fields absent from the debug dict are not emitted as keys."""
        bt = _bt_with_pid(
            ["climate.a"],
            [
                {
                    "model": "generic",
                    "calibration_balance": {"debug": {"mode": "pid", "e_K": 0.1}},
                }
            ],
        )
        out = collect_pid_debug_attrs(bt)
        assert out == {"pid_e_K": 0.1}

    def test_non_numeric_field_silently_skipped(self):
        """Non-numeric scalar values are dropped, valid neighbours kept."""
        bt = _bt_with_pid(
            ["climate.a"],
            [
                {
                    "model": "generic",
                    "calibration_balance": {
                        "debug": {"mode": "pid", "e_K": "not a number", "p": 0.4}
                    },
                }
            ],
        )
        out = collect_pid_debug_attrs(bt)
        assert "pid_e_K" not in out
        assert out["pid_P"] == 0.4

    def test_prefers_sonoff_or_trvzb_trv(self):
        """When multiple TRVs are present, sonoff/trvzb wins as representative."""
        bt = _bt_with_pid(
            ["climate.a", "climate.b"],
            [
                {
                    "model": "generic",
                    "calibration_balance": {"debug": {"mode": "pid", "e_K": 1.0}},
                },
                {
                    "model": "SONOFF TRVZB",
                    "calibration_balance": {"debug": {"mode": "pid", "e_K": 2.0}},
                },
            ],
        )
        out = collect_pid_debug_attrs(bt)
        assert out["pid_e_K"] == 2.0

    def test_model_none_does_not_crash(self):
        """A TRV with ``model=None`` must not raise AttributeError on .lower()."""
        bt = _bt_with_pid(
            ["climate.a"],
            [
                {
                    "model": None,
                    "calibration_balance": {"debug": {"mode": "pid", "e_K": 1.0}},
                }
            ],
        )
        out = collect_pid_debug_attrs(bt)
        assert out["pid_e_K"] == 1.0

    def test_no_balance_no_emit(self):
        """A TRV without calibration_balance produces no PID output."""
        bt = _bt_with_pid(["climate.a"], [{"model": "generic"}])
        out = collect_pid_debug_attrs(bt)
        assert out == {}
