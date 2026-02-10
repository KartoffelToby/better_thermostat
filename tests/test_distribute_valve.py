"""Tests for multi-TRV valve distribution and group MPC key.

When a BT instance controls multiple TRVs, the MPC controller computes a
single group-level valve command from the external room sensor.
``distribute_valve_percent()`` then distributes this command across TRVs
based on their internal temperatures:

- **Warmest TRV** = reference → receives exactly the MPC output.
- **Colder TRVs** → receive a boost proportional to their temperature
  deficit relative to the warmest TRV.
"""

from unittest.mock import MagicMock

import pytest

from custom_components.better_thermostat.utils.calibration.mpc import (
    DISTRIBUTE_COMPENSATION_PCT_PER_K,
    build_mpc_group_key,
    build_mpc_key,
    distribute_valve_percent,
)


# ---------------------------------------------------------------------------
# distribute_valve_percent
# ---------------------------------------------------------------------------
class TestDistributeValvePercent:
    """Unit tests for distribute_valve_percent()."""

    def test_empty_dict(self):
        """Empty TRV dict → empty result."""
        result = distribute_valve_percent(50.0, {})
        assert result == {}

    def test_single_trv_passthrough(self):
        """Single TRV returns the group percentage unchanged."""
        result = distribute_valve_percent(60.0, {"trv_a": 20.0})
        assert len(result) == 1
        assert result["trv_a"] == pytest.approx(60.0, abs=0.01)

    def test_zero_command(self):
        """When group command is 0 %, all TRVs get 0 %."""
        result = distribute_valve_percent(
            0.0, {"trv_a": 18.0, "trv_b": 25.0}
        )
        for pct in result.values():
            assert pct == pytest.approx(0.0, abs=0.01)

    def test_cold_trv_gets_more_than_warm(self):
        """A colder TRV should get more valve opening than the warmest."""
        trv_temps = {
            "trv_cold": 18.0,
            "trv_warm": 24.0,
        }
        result = distribute_valve_percent(50.0, trv_temps)

        assert result["trv_cold"] > result["trv_warm"]
        # Warmest gets exactly the MPC value
        assert result["trv_warm"] == pytest.approx(50.0, abs=0.01)
        # Cold gets MPC + deficit * compensation
        expected_cold = 50.0 + (24.0 - 18.0) * DISTRIBUTE_COMPENSATION_PCT_PER_K
        assert result["trv_cold"] == pytest.approx(expected_cold, abs=0.01)

    def test_warmest_always_gets_mpc_value(self):
        """The warmest TRV always receives exactly u_total_pct."""
        trv_temps = {
            "trv_a": 28.1,
            "trv_b": 20.3,
        }
        result = distribute_valve_percent(60.0, trv_temps)

        # trv_a is warmest → gets exactly 60 %
        assert result["trv_a"] == pytest.approx(60.0, abs=0.01)
        # trv_b is colder → gets more
        assert result["trv_b"] > result["trv_a"]

    def test_real_world_three_trvs(self):
        """Realistic 3-TRV scenario from the user's description.

        MPC group output = 45 %.
        TRV_1: 28.1 °C (warmest → reference, gets exactly 45 %)
        TRV_2: 24.3 °C (3.8 K colder → 45 + 3.8*8 = 75.4 %)
        TRV_3: 19.5 °C (8.6 K colder → 45 + 8.6*8 = 113.8 → clamped 100 %)
        """
        trv_temps = {
            "trv_1": 28.1,
            "trv_2": 24.3,
            "trv_3": 19.5,
        }
        result = distribute_valve_percent(45.0, trv_temps)

        # Warmest TRV gets exactly the MPC output
        assert result["trv_1"] == pytest.approx(45.0, abs=0.01)

        # Ordering: cold > slightly_warm > warmest
        assert result["trv_3"] > result["trv_2"] > result["trv_1"]

        # TRV_2: 45 + 3.8 * 8 = 75.4
        assert result["trv_2"] == pytest.approx(75.4, abs=0.1)

        # TRV_3: 45 + 8.6 * 8 = 113.8 → clamped to 100
        assert result["trv_3"] == pytest.approx(100.0, abs=0.01)

        # All within [0, 100]
        for pct in result.values():
            assert 0.0 <= pct <= 100.0

    def test_all_same_temperature(self):
        """All TRVs at same temp → uniform distribution (all get MPC value)."""
        trv_temps = {"a": 21.0, "b": 21.0, "c": 21.0}
        result = distribute_valve_percent(60.0, trv_temps)

        for v in result.values():
            assert v == pytest.approx(60.0, abs=0.01)

    def test_small_temp_diff(self):
        """Small temperature differences produce small boosts."""
        trv_temps = {"a": 22.0, "b": 22.5, "c": 23.0}
        result = distribute_valve_percent(40.0, trv_temps)

        # Warmest (23.0) gets exactly 40 %
        assert result["c"] == pytest.approx(40.0, abs=0.01)
        # 0.5 K colder → 40 + 0.5*8 = 44 %
        assert result["b"] == pytest.approx(44.0, abs=0.01)
        # 1.0 K colder → 40 + 1.0*8 = 48 %
        assert result["a"] == pytest.approx(48.0, abs=0.01)

    def test_none_temperature_gets_baseline(self):
        """TRV with None temperature gets the MPC baseline (neutral)."""
        trv_temps = {
            "trv_cold": 18.0,
            "trv_none": None,
            "trv_warm": 25.0,
        }
        result = distribute_valve_percent(60.0, trv_temps)

        # trv_none gets neutral = u_total_pct
        assert result["trv_none"] == pytest.approx(60.0, abs=0.01)
        # cold still gets the most
        assert result["trv_cold"] > result["trv_warm"]
        # warm is warmest → gets exactly 60 %
        assert result["trv_warm"] == pytest.approx(60.0, abs=0.01)

    def test_all_none_temperatures(self):
        """All None temps → uniform distribution."""
        trv_temps = {"a": None, "b": None, "c": None}
        result = distribute_valve_percent(50.0, trv_temps)

        for pct in result.values():
            assert pct == pytest.approx(50.0, abs=0.01)

    def test_clamped_to_100(self):
        """Extreme cold TRV with high group command doesn't exceed 100 %."""
        trv_temps = {
            "trv_extreme_cold": 10.0,
            "trv_warm": 24.0,
        }
        result = distribute_valve_percent(95.0, trv_temps)

        assert result["trv_warm"] == pytest.approx(95.0, abs=0.01)
        # 14 K deficit * 8 = 112 → 95 + 112 = 207 → clamped to 100
        assert result["trv_extreme_cold"] == pytest.approx(100.0, abs=0.01)

    def test_total_power_at_least_mpc_times_n(self):
        """Total heating power is ≥ u_total_pct * N (only boosts, no cuts)."""
        trv_temps = {
            "a": 18.0,
            "b": 20.0,
            "c": 22.0,
            "d": 25.0,
        }
        result = distribute_valve_percent(50.0, trv_temps)

        total = sum(result.values())
        baseline_total = 50.0 * 4
        # Total must be >= baseline (only boosts, never reduces)
        assert total >= baseline_total - 0.01
        # Warmest gets exactly MPC
        assert result["d"] == pytest.approx(50.0, abs=0.01)


# ---------------------------------------------------------------------------
# build_mpc_group_key
# ---------------------------------------------------------------------------
class TestBuildMpcGroupKey:
    """Tests for build_mpc_group_key()."""

    def test_group_key_format(self):
        """Group key should contain 'group' instead of entity_id."""
        bt = MagicMock()
        bt.unique_id = "bt_living_room"
        bt.bt_target_temp = 22.0

        key = build_mpc_group_key(bt)
        assert "group" in key
        assert "bt_living_room" in key
        assert "t22.0" in key

    def test_group_key_differs_from_entity_key(self):
        """Group key should differ from per-TRV key."""
        bt = MagicMock()
        bt.unique_id = "bt_test"
        bt.bt_target_temp = 21.0

        group_key = build_mpc_group_key(bt)
        entity_key = build_mpc_key(bt, "climate.trv_1")

        assert group_key != entity_key
        assert "group" in group_key
        assert "climate.trv_1" in entity_key

    def test_group_key_same_bucket(self):
        """Group key should use the same bucket logic as entity key."""
        bt = MagicMock()
        bt.unique_id = "bt_test"
        bt.bt_target_temp = 21.3  # rounds to t21.5

        group_key = build_mpc_group_key(bt)
        entity_key = build_mpc_key(bt, "climate.trv_x")

        # Both should have the same bucket suffix
        group_bucket = group_key.split(":")[-1]
        entity_bucket = entity_key.split(":")[-1]
        assert group_bucket == entity_bucket

    def test_group_key_none_target(self):
        """Group key handles None target temp gracefully."""
        bt = MagicMock()
        bt.unique_id = "bt_test"
        bt.bt_target_temp = None

        key = build_mpc_group_key(bt)
        assert "tunknown" in key

    def test_group_key_fallback_uid(self):
        """Group key falls back to _unique_id if unique_id is None."""
        bt = MagicMock()
        bt.unique_id = None
        bt._unique_id = "fallback_id"
        bt.bt_target_temp = 20.0

        key = build_mpc_group_key(bt)
        assert "fallback_id" in key
