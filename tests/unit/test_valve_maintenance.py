"""Tests for utils/valve_maintenance.py – pure valve-maintenance helpers.

Covers:
  - collect_maintenance_trvs  (filter enabled TRVs)
  - compute_next_maintenance  (interval + jitter)
  - compute_initial_maintenance (startup delay)
  - build_trv_snapshots       (snapshot builder)
  - open_step / close_step    (direct valve vs temp-based)
  - restore_one               (temperature + mode restore)
  - run_valve_maintenance      (full 2-cycle orchestrator)
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.better_thermostat.utils.valve_maintenance import (
    MaintenanceTrvInfo,
    build_trv_snapshots,
    close_step,
    collect_maintenance_trvs,
    compute_initial_maintenance,
    compute_next_maintenance,
    open_step,
    restore_one,
    run_valve_maintenance,
)

# ── Helpers ────────────────────────────────────────────────────────────────


def _trv(
    *,
    maintenance: bool = False,
    max_temp: float = 30,
    min_temp: float = 5,
    quirks: object | None = None,
    valve_entity: str | None = None,
    calibration: str | None = None,
) -> dict:
    """Build a ``real_trvs[entity_id]`` dict for testing."""
    return {
        "advanced": {"valve_maintenance": maintenance, "calibration": calibration},
        "max_temp": max_temp,
        "min_temp": min_temp,
        "model_quirks": quirks,
        "valve_position_entity": valve_entity,
    }


def _info(
    entity_id: str = "climate.trv1",
    cur_mode: str = "heat",
    cur_temp: float | None = 21.0,
    use_direct_valve: bool = False,
    max_temp: float = 30,
    min_temp: float = 5,
) -> MaintenanceTrvInfo:
    """Convenience factory for MaintenanceTrvInfo."""
    return MaintenanceTrvInfo(
        entity_id=entity_id,
        cur_mode=cur_mode,
        cur_temp=cur_temp,
        use_direct_valve=use_direct_valve,
        max_temp=max_temp,
        min_temp=min_temp,
    )


def _ha_state(state: str = "heat", temperature: float = 21.0):
    """Mimic a HA State object."""
    return SimpleNamespace(state=state, attributes={"temperature": temperature})


# ═══════════════════════════════════════════════════════════════════════════
# collect_maintenance_trvs
# ═══════════════════════════════════════════════════════════════════════════


class TestCollectMaintenanceTrvs:
    def test_empty_dict(self):
        assert collect_maintenance_trvs({}) == []

    def test_no_maintenance_enabled(self):
        trvs = {"trv1": _trv(maintenance=False), "trv2": _trv(maintenance=False)}
        assert collect_maintenance_trvs(trvs) == []

    def test_single_enabled(self):
        trvs = {"trv1": _trv(maintenance=True)}
        assert collect_maintenance_trvs(trvs) == ["trv1"]

    def test_mixed(self):
        trvs = {
            "trv1": _trv(maintenance=False),
            "trv2": _trv(maintenance=True),
            "trv3": _trv(maintenance=True),
        }
        result = collect_maintenance_trvs(trvs)
        assert set(result) == {"trv2", "trv3"}

    def test_missing_advanced_key(self):
        """TRV dict without 'advanced' should not crash."""
        trvs = {"trv1": {"max_temp": 30}}
        assert collect_maintenance_trvs(trvs) == []

    def test_advanced_is_none(self):
        """advanced=None should not crash."""
        trvs = {"trv1": {"advanced": None}}
        assert collect_maintenance_trvs(trvs) == []


# ═══════════════════════════════════════════════════════════════════════════
# compute_next_maintenance
# ═══════════════════════════════════════════════════════════════════════════


class TestComputeNextMaintenance:
    def test_default_168h(self):
        """Without quirks the interval should be ~168 h (± 7 % jitter)."""
        trvs = {"trv1": _trv(maintenance=True)}
        now = datetime(2026, 1, 1)
        result = compute_next_maintenance(trvs, ["trv1"], now=now)
        delta_h = (result - now).total_seconds() / 3600
        assert 168 <= delta_h <= 168 + 168 * 0.07 + 1

    def test_quirks_shorter_interval(self):
        quirks = SimpleNamespace(VALVE_MAINTENANCE_INTERVAL_HOURS=24)
        trvs = {"trv1": _trv(maintenance=True, quirks=quirks)}
        now = datetime(2026, 1, 1)
        result = compute_next_maintenance(trvs, ["trv1"], now=now)
        delta_h = (result - now).total_seconds() / 3600
        # 24h + up to ~7% jitter
        assert 24 <= delta_h <= 24 + 24 * 0.07 + 1

    def test_minimum_across_trvs(self):
        q12 = SimpleNamespace(VALVE_MAINTENANCE_INTERVAL_HOURS=12)
        q48 = SimpleNamespace(VALVE_MAINTENANCE_INTERVAL_HOURS=48)
        trvs = {
            "trv1": _trv(maintenance=True, quirks=q12),
            "trv2": _trv(maintenance=True, quirks=q48),
        }
        now = datetime(2026, 1, 1)
        result = compute_next_maintenance(trvs, ["trv1", "trv2"], now=now)
        delta_h = (result - now).total_seconds() / 3600
        # Should use 12h (minimum)
        assert 12 <= delta_h <= 12 + 12 * 0.07 + 1


# ═══════════════════════════════════════════════════════════════════════════
# compute_initial_maintenance
# ═══════════════════════════════════════════════════════════════════════════


class TestComputeInitialMaintenance:
    def test_default_range(self):
        trvs = {"trv1": _trv(maintenance=True)}
        now = datetime(2026, 1, 1)
        result = compute_initial_maintenance(trvs, ["trv1"], now=now)
        delta_h = (result - now).total_seconds() / 3600
        assert 1 <= delta_h <= 24 * 5

    def test_short_quirk_constrains_delay(self):
        quirks = SimpleNamespace(VALVE_MAINTENANCE_INTERVAL_HOURS=6)
        trvs = {"trv1": _trv(maintenance=True, quirks=quirks)}
        now = datetime(2026, 1, 1)
        result = compute_initial_maintenance(trvs, ["trv1"], now=now)
        delta_h = (result - now).total_seconds() / 3600
        # max_delay_hours = min(120, 6) = 6 → randint(1, max(2, 6))
        assert 1 <= delta_h <= 6


# ═══════════════════════════════════════════════════════════════════════════
# build_trv_snapshots
# ═══════════════════════════════════════════════════════════════════════════


class TestBuildTrvSnapshots:
    def test_state_none_skipped(self):
        trvs = {"trv1": _trv(maintenance=True)}
        result = build_trv_snapshots(trvs, ["trv1"], lambda _: None, "Test")
        assert result == []

    def test_basic_snapshot(self):
        trvs = {"trv1": _trv(maintenance=True, max_temp=28, min_temp=6)}

        def get_state(eid):
            return _ha_state("heat", 22.0)

        result = build_trv_snapshots(trvs, ["trv1"], get_state, "Test")
        assert len(result) == 1
        assert result[0].entity_id == "trv1"
        assert result[0].cur_mode == "heat"
        assert result[0].cur_temp == 22.0
        assert result[0].max_temp == 28
        assert result[0].min_temp == 6
        assert result[0].use_direct_valve is False

    def test_direct_valve_detection(self):
        quirks = SimpleNamespace(override_set_valve=lambda: None)
        trvs = {
            "trv1": _trv(
                maintenance=True,
                quirks=quirks,
                calibration="direct_valve_based",
            )
        }
        result = build_trv_snapshots(
            trvs, ["trv1"], lambda _: _ha_state(), "Test"
        )
        assert result[0].use_direct_valve is True

    def test_valve_entity_direct(self):
        trvs = {
            "trv1": _trv(
                maintenance=True,
                valve_entity="number.valve",
                calibration="direct_valve_based",
            )
        }
        result = build_trv_snapshots(
            trvs, ["trv1"], lambda _: _ha_state(), "Test"
        )
        assert result[0].use_direct_valve is True


# ═══════════════════════════════════════════════════════════════════════════
# open_step / close_step
# ═══════════════════════════════════════════════════════════════════════════


class TestOpenStep:
    @pytest.mark.asyncio
    async def test_direct_valve_sets_100(self):
        valve_fn = AsyncMock(return_value=True)
        temp_fn = AsyncMock()
        info = _info(use_direct_valve=True)
        await open_step(info, set_valve_fn=valve_fn, set_temperature_fn=temp_fn)
        valve_fn.assert_awaited_once_with("climate.trv1", 100)
        temp_fn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_temp_based_sets_max(self):
        valve_fn = AsyncMock()
        temp_fn = AsyncMock()
        info = _info(use_direct_valve=False, max_temp=28)
        await open_step(info, set_valve_fn=valve_fn, set_temperature_fn=temp_fn)
        temp_fn.assert_awaited_once_with("climate.trv1", 28)
        valve_fn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_off_mode_no_call(self):
        valve_fn = AsyncMock()
        temp_fn = AsyncMock()
        info = _info(cur_mode="off", use_direct_valve=False)
        await open_step(info, set_valve_fn=valve_fn, set_temperature_fn=temp_fn)
        valve_fn.assert_not_awaited()
        temp_fn.assert_not_awaited()


class TestCloseStep:
    @pytest.mark.asyncio
    async def test_direct_valve_sets_0(self):
        valve_fn = AsyncMock(return_value=True)
        temp_fn = AsyncMock()
        info = _info(use_direct_valve=True)
        await close_step(info, set_valve_fn=valve_fn, set_temperature_fn=temp_fn)
        valve_fn.assert_awaited_once_with("climate.trv1", 0)
        temp_fn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_temp_based_sets_min(self):
        valve_fn = AsyncMock()
        temp_fn = AsyncMock()
        info = _info(use_direct_valve=False, min_temp=4)
        await close_step(info, set_valve_fn=valve_fn, set_temperature_fn=temp_fn)
        temp_fn.assert_awaited_once_with("climate.trv1", 4)
        valve_fn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_off_mode_no_call(self):
        valve_fn = AsyncMock()
        temp_fn = AsyncMock()
        info = _info(cur_mode="off", use_direct_valve=False)
        await close_step(info, set_valve_fn=valve_fn, set_temperature_fn=temp_fn)
        valve_fn.assert_not_awaited()
        temp_fn.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════════════════
# restore_one
# ═══════════════════════════════════════════════════════════════════════════


class TestRestoreOne:
    @pytest.mark.asyncio
    async def test_restores_temp_and_mode(self):
        temp_fn = AsyncMock()
        mode_fn = AsyncMock()
        info = _info(cur_temp=22.5, cur_mode="heat")
        await restore_one(info, set_temperature_fn=temp_fn, set_hvac_mode_fn=mode_fn)
        temp_fn.assert_awaited_once_with("climate.trv1", 22.5)
        mode_fn.assert_awaited_once_with("climate.trv1", "heat")

    @pytest.mark.asyncio
    async def test_cur_temp_none_skips_temperature(self):
        temp_fn = AsyncMock()
        mode_fn = AsyncMock()
        info = _info(cur_temp=None)
        await restore_one(info, set_temperature_fn=temp_fn, set_hvac_mode_fn=mode_fn)
        temp_fn.assert_not_awaited()
        mode_fn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_temp_exception_still_sets_mode(self):
        temp_fn = AsyncMock(side_effect=RuntimeError("fail"))
        mode_fn = AsyncMock()
        info = _info(cur_temp=20.0, cur_mode="heat")
        await restore_one(info, set_temperature_fn=temp_fn, set_hvac_mode_fn=mode_fn)
        mode_fn.assert_awaited_once_with("climate.trv1", "heat")


# ═══════════════════════════════════════════════════════════════════════════
# run_valve_maintenance (full orchestrator)
# ═══════════════════════════════════════════════════════════════════════════


class TestRunValveMaintenance:
    @pytest.mark.asyncio
    async def test_two_cycles_open_close(self):
        """Each TRV should get 2 open + 2 close calls."""
        valve_fn = AsyncMock(return_value=True)
        temp_fn = AsyncMock()
        mode_fn = AsyncMock()
        infos = [_info(entity_id="trv1", use_direct_valve=True)]

        await run_valve_maintenance(
            infos,
            set_valve_fn=valve_fn,
            set_temperature_fn=temp_fn,
            set_hvac_mode_fn=mode_fn,
            device_name="Test",
            cycle_sleep=0,
        )

        # 2 opens (100) + 2 closes (0) = 4 valve calls
        assert valve_fn.await_count == 4
        calls = [c.args for c in valve_fn.await_args_list]
        assert calls == [("trv1", 100), ("trv1", 0), ("trv1", 100), ("trv1", 0)]

    @pytest.mark.asyncio
    async def test_multiple_trvs(self):
        valve_fn = AsyncMock(return_value=True)
        temp_fn = AsyncMock()
        mode_fn = AsyncMock()
        infos = [
            _info(entity_id="trv1", use_direct_valve=True),
            _info(entity_id="trv2", use_direct_valve=True),
        ]

        await run_valve_maintenance(
            infos,
            set_valve_fn=valve_fn,
            set_temperature_fn=temp_fn,
            set_hvac_mode_fn=mode_fn,
            device_name="Test",
            cycle_sleep=0,
        )

        # 2 TRVs × (2 open + 2 close) = 8 valve calls
        assert valve_fn.await_count == 8

    @pytest.mark.asyncio
    async def test_temp_based_cycles(self):
        valve_fn = AsyncMock()
        temp_fn = AsyncMock()
        mode_fn = AsyncMock()
        infos = [_info(entity_id="trv1", use_direct_valve=False, max_temp=30, min_temp=5)]

        await run_valve_maintenance(
            infos,
            set_valve_fn=valve_fn,
            set_temperature_fn=temp_fn,
            set_hvac_mode_fn=mode_fn,
            device_name="Test",
            cycle_sleep=0,
        )

        # 2 opens (max) + 2 closes (min) = 4 temp calls, plus 1 restore = 5
        assert temp_fn.await_count == 5
        valve_fn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_restores_after_cycles(self):
        valve_fn = AsyncMock(return_value=True)
        temp_fn = AsyncMock()
        mode_fn = AsyncMock()
        infos = [_info(entity_id="trv1", cur_temp=22.0, cur_mode="heat", use_direct_valve=True)]

        await run_valve_maintenance(
            infos,
            set_valve_fn=valve_fn,
            set_temperature_fn=temp_fn,
            set_hvac_mode_fn=mode_fn,
            device_name="Test",
            cycle_sleep=0,
        )

        # restore calls temp + mode
        temp_fn.assert_awaited_once_with("trv1", 22.0)
        mode_fn.assert_awaited_once_with("trv1", "heat")

    @pytest.mark.asyncio
    async def test_empty_infos_noop(self):
        """No TRVs → no calls, no crash."""
        valve_fn = AsyncMock()
        temp_fn = AsyncMock()
        mode_fn = AsyncMock()

        await run_valve_maintenance(
            [],
            set_valve_fn=valve_fn,
            set_temperature_fn=temp_fn,
            set_hvac_mode_fn=mode_fn,
            device_name="Test",
            cycle_sleep=0,
        )

        valve_fn.assert_not_awaited()
        temp_fn.assert_not_awaited()
        mode_fn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_off_trvs_skipped_in_temp_mode(self):
        """TRVs in OFF mode with temp-based control should not get open/close calls."""
        valve_fn = AsyncMock()
        temp_fn = AsyncMock()
        mode_fn = AsyncMock()
        infos = [_info(entity_id="trv1", cur_mode="off", use_direct_valve=False, cur_temp=20.0)]

        await run_valve_maintenance(
            infos,
            set_valve_fn=valve_fn,
            set_temperature_fn=temp_fn,
            set_hvac_mode_fn=mode_fn,
            device_name="Test",
            cycle_sleep=0,
        )

        # open/close skipped for OFF, but restore still sets temp + mode
        assert temp_fn.await_count == 1  # only restore
        mode_fn.assert_awaited_once_with("trv1", "off")

    @pytest.mark.asyncio
    async def test_exception_in_valve_fn_doesnt_crash(self):
        """Exceptions in callbacks should be caught (return_exceptions=True)."""
        valve_fn = AsyncMock(side_effect=RuntimeError("hardware fault"))
        temp_fn = AsyncMock()
        mode_fn = AsyncMock()
        infos = [_info(entity_id="trv1", use_direct_valve=True)]

        # Should not raise
        await run_valve_maintenance(
            infos,
            set_valve_fn=valve_fn,
            set_temperature_fn=temp_fn,
            set_hvac_mode_fn=mode_fn,
            device_name="Test",
            cycle_sleep=0,
        )
