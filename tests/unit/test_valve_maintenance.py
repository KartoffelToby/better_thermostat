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

import asyncio
from datetime import datetime
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.components.climate.const import HVACMode
from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.valve_maintenance import (
    MaintenanceTrvInfo,
    build_trv_snapshots,
    close_step,
    collect_maintenance_trvs,
    compute_initial_maintenance,
    compute_next_maintenance,
    mode_needs_restoring,
    open_step,
    pick_wake_mode,
    restore_one,
    run_valve_maintenance,
    wake_step,
)

_MAINTENANCE_LOGGER = "custom_components.better_thermostat.utils.valve_maintenance"

# ── Helpers ────────────────────────────────────────────────────────────────


def _trv(
    *,
    maintenance: bool = False,
    max_temp: float = 30,
    min_temp: float = 5,
    quirks: object | None = None,
    valve_entity: str | None = None,
    valve_writable: bool = True,
    calibration: str | None = None,
) -> Trv:
    """Build a ``real_trvs[entity_id]`` entry for testing."""
    return Trv.from_legacy_dict(
        "climate.trv",
        {
            "advanced": {"valve_maintenance": maintenance, "calibration": calibration},
            "max_temp": max_temp,
            "min_temp": min_temp,
            "model_quirks": quirks,
            "valve_position_entity": valve_entity,
            "valve_position_writable": valve_writable,
        },
    )


def _state(entity_id: str, mode: str) -> State:
    """A ``State`` for ``entity_id``, which is a bare name in this module.

    ``State`` splits the id into a domain and an object id and rejects one
    without a domain, so a bare name is given the climate domain it stands
    for. Only the reported mode is read back.
    """
    return State(entity_id if "." in entity_id else f"climate.{entity_id}", mode)


def _reports(mode: str | None):
    """A ``hass.states.get`` stand-in reporting every TRV in ``mode``.

    ``None`` stands for a TRV that publishes no state at all.
    """

    def get_state(entity_id: str) -> State | None:
        return None if mode is None else _state(entity_id, mode)

    return get_state


def _reports_a_moved_mode(infos: list[MaintenanceTrvInfo]):
    """A state reader answering with a mode no TRV here was started in.

    The restore writes the mode back only when it moved, so this is the
    reading the tests below assume: they are about what the run does with
    a TRV whose mode has to go back, not about the guard itself.
    """
    moved = {
        info.entity_id: HVACMode.OFF if info.cur_mode != HVACMode.OFF else "heat"
        for info in infos
    }

    def get_state(entity_id: str) -> State | None:
        return _state(entity_id, moved.get(entity_id, HVACMode.OFF))

    return get_state


def _info(
    entity_id: str = "climate.trv1",
    cur_mode: str = "heat",
    cur_temp: float | None = 21.0,
    use_direct_valve: bool = False,
    max_temp: float = 30,
    min_temp: float = 5,
    wake_mode: str | None = None,
) -> MaintenanceTrvInfo:
    """Create a MaintenanceTrvInfo with sensible defaults."""
    return MaintenanceTrvInfo(
        entity_id=entity_id,
        cur_mode=cur_mode,
        cur_temp=cur_temp,
        use_direct_valve=use_direct_valve,
        max_temp=max_temp,
        min_temp=min_temp,
        wake_mode=wake_mode,
    )


def _ha_state(
    state: str = "heat", temperature: float = 21.0, hvac_modes: list[str] | None = None
):
    """Mimic a HA State object."""
    return SimpleNamespace(
        state=state,
        attributes={
            "temperature": temperature,
            "hvac_modes": ["off", "heat"] if hvac_modes is None else hvac_modes,
        },
    )


# ═══════════════════════════════════════════════════════════════════════════
# collect_maintenance_trvs
# ═══════════════════════════════════════════════════════════════════════════


class TestCollectMaintenanceTrvs:
    """Tests for collect maintenance trvs."""

    def test_empty_dict(self):
        """Test Empty dict."""
        assert collect_maintenance_trvs({}) == []

    def test_no_maintenance_enabled(self):
        """Test No maintenance enabled."""
        trvs = {"trv1": _trv(maintenance=False), "trv2": _trv(maintenance=False)}
        assert collect_maintenance_trvs(trvs) == []

    def test_single_enabled(self):
        """Test Single enabled."""
        trvs = {"trv1": _trv(maintenance=True)}
        assert collect_maintenance_trvs(trvs) == ["trv1"]

    def test_mixed(self):
        """Test Mixed."""
        trvs = {
            "trv1": _trv(maintenance=False),
            "trv2": _trv(maintenance=True),
            "trv3": _trv(maintenance=True),
        }
        result = collect_maintenance_trvs(trvs)
        assert set(result) == {"trv2", "trv3"}

    def test_missing_advanced_key(self):
        """TRV dict without 'advanced' should not crash."""
        trvs = {"trv1": Trv.from_legacy_dict("trv1", {"max_temp": 30})}
        assert collect_maintenance_trvs(trvs) == []

    def test_advanced_is_none(self):
        """advanced=None should not crash."""
        trvs = {"trv1": Trv.from_legacy_dict("trv1", {"advanced": None})}
        assert collect_maintenance_trvs(trvs) == []


# ═══════════════════════════════════════════════════════════════════════════
# compute_next_maintenance
# ═══════════════════════════════════════════════════════════════════════════


class TestComputeNextMaintenance:
    """Tests for compute next maintenance."""

    def test_default_168h(self):
        """Without quirks the interval should be ~168 h (± 7 % jitter)."""
        trvs = {"trv1": _trv(maintenance=True)}
        now = datetime(2026, 1, 1)
        result = compute_next_maintenance(trvs, ["trv1"], now=now)
        delta_h = (result - now).total_seconds() / 3600
        assert 168 <= delta_h <= 168 + 168 * 0.07 + 1

    def test_quirks_shorter_interval(self):
        """Test Quirks shorter interval."""
        quirks = SimpleNamespace(VALVE_MAINTENANCE_INTERVAL_HOURS=24)
        trvs = {"trv1": _trv(maintenance=True, quirks=quirks)}
        now = datetime(2026, 1, 1)
        result = compute_next_maintenance(trvs, ["trv1"], now=now)
        delta_h = (result - now).total_seconds() / 3600
        # 24h + up to ~7% jitter
        assert 24 <= delta_h <= 24 + 24 * 0.07 + 1

    def test_minimum_across_trvs(self):
        """Test Minimum across trvs."""
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
    """Tests for compute initial maintenance."""

    def test_default_range(self):
        """Test Default range."""
        trvs = {"trv1": _trv(maintenance=True)}
        now = datetime(2026, 1, 1)
        result = compute_initial_maintenance(trvs, ["trv1"], now=now)
        delta_h = (result - now).total_seconds() / 3600
        assert 1 <= delta_h <= 24 * 5

    def test_short_quirk_constrains_delay(self):
        """Test Short quirk constrains delay."""
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
    """Tests for build trv snapshots."""

    def test_state_none_skipped(self):
        """Test State none skipped."""
        trvs = {"trv1": _trv(maintenance=True)}
        result = build_trv_snapshots(trvs, ["trv1"], lambda _: None, "Test")
        assert result == []

    def test_basic_snapshot(self):
        """Test Basic snapshot."""
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
        """Test Direct valve detection."""
        quirks = SimpleNamespace(override_set_valve=lambda: None)
        trvs = {
            "trv1": _trv(
                maintenance=True, quirks=quirks, calibration="direct_valve_based"
            )
        }
        result = build_trv_snapshots(trvs, ["trv1"], lambda _: _ha_state(), "Test")
        assert result[0].use_direct_valve is True

    def test_valve_entity_direct(self):
        """Test Valve entity direct."""
        trvs = {
            "trv1": _trv(
                maintenance=True,
                valve_entity="number.valve",
                calibration="direct_valve_based",
            )
        }
        result = build_trv_snapshots(trvs, ["trv1"], lambda _: _ha_state(), "Test")
        assert result[0].use_direct_valve is True

    def test_wake_mode_from_enum_repr_capabilities(self):
        """An off TRV spelling its capabilities as ``HVACMode.*`` still wakes."""
        trvs = {"trv1": _trv(maintenance=True)}

        def get_state(_):
            return _ha_state("off", 21.0, ["HVACMode.OFF", "HVACMode.HEAT"])

        result = build_trv_snapshots(trvs, ["trv1"], get_state, "Test")
        assert result[0].wake_mode == HVACMode.HEAT


# ═══════════════════════════════════════════════════════════════════════════
# open_step / close_step
# ═══════════════════════════════════════════════════════════════════════════


class TestOpenStep:
    """Tests for open step."""

    @pytest.mark.asyncio
    async def test_direct_valve_sets_100(self):
        """Test Direct valve sets 100."""
        valve_fn = AsyncMock(return_value=True)
        temp_fn = AsyncMock()
        info = _info(use_direct_valve=True)
        await open_step(info, set_valve_fn=valve_fn, set_temperature_fn=temp_fn)
        valve_fn.assert_awaited_once_with("climate.trv1", 100)
        temp_fn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_temp_based_sets_max(self):
        """Test Temp based sets max."""
        valve_fn = AsyncMock()
        temp_fn = AsyncMock()
        info = _info(use_direct_valve=False, max_temp=28)
        await open_step(info, set_valve_fn=valve_fn, set_temperature_fn=temp_fn)
        temp_fn.assert_awaited_once_with("climate.trv1", 28)
        valve_fn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_off_mode_no_call(self):
        """Test Off mode no call."""
        valve_fn = AsyncMock()
        temp_fn = AsyncMock()
        info = _info(cur_mode="off", use_direct_valve=False)
        await open_step(info, set_valve_fn=valve_fn, set_temperature_fn=temp_fn)
        valve_fn.assert_not_awaited()
        temp_fn.assert_not_awaited()


class TestCloseStep:
    """Tests for close step."""

    @pytest.mark.asyncio
    async def test_direct_valve_sets_0(self):
        """Test Direct valve sets 0."""
        valve_fn = AsyncMock(return_value=True)
        temp_fn = AsyncMock()
        info = _info(use_direct_valve=True)
        await close_step(info, set_valve_fn=valve_fn, set_temperature_fn=temp_fn)
        valve_fn.assert_awaited_once_with("climate.trv1", 0)
        temp_fn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_temp_based_sets_min(self):
        """Test Temp based sets min."""
        valve_fn = AsyncMock()
        temp_fn = AsyncMock()
        info = _info(use_direct_valve=False, min_temp=4)
        await close_step(info, set_valve_fn=valve_fn, set_temperature_fn=temp_fn)
        temp_fn.assert_awaited_once_with("climate.trv1", 4)
        valve_fn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_off_mode_no_call(self):
        """Test Off mode no call."""
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
    """Tests for restore one."""

    @pytest.mark.asyncio
    async def test_restores_temp_and_mode(self):
        """Test Restores temp and mode."""
        temp_fn = AsyncMock()
        mode_fn = AsyncMock()
        info = _info(cur_temp=22.5, cur_mode="heat")
        await restore_one(
            info,
            set_temperature_fn=temp_fn,
            set_hvac_mode_fn=mode_fn,
            get_state=_reports_a_moved_mode([info]),
        )
        temp_fn.assert_awaited_once_with("climate.trv1", 22.5)
        mode_fn.assert_awaited_once_with("climate.trv1", "heat")

    @pytest.mark.asyncio
    async def test_cur_temp_none_skips_temperature(self):
        """Test Cur temp none skips temperature."""
        temp_fn = AsyncMock()
        mode_fn = AsyncMock()
        info = _info(cur_temp=None)
        await restore_one(
            info,
            set_temperature_fn=temp_fn,
            set_hvac_mode_fn=mode_fn,
            get_state=_reports_a_moved_mode([info]),
        )
        temp_fn.assert_not_awaited()
        mode_fn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_temp_exception_still_sets_mode(self):
        """Test Temp exception still sets mode."""
        temp_fn = AsyncMock(side_effect=RuntimeError("fail"))
        mode_fn = AsyncMock()
        info = _info(cur_temp=20.0, cur_mode="heat")
        await restore_one(
            info,
            set_temperature_fn=temp_fn,
            set_hvac_mode_fn=mode_fn,
            get_state=_reports_a_moved_mode([info]),
        )
        mode_fn.assert_awaited_once_with("climate.trv1", "heat")

    @pytest.mark.asyncio
    async def test_failed_restores_are_traced(self, caplog):
        """Both restore writes report the TRV they could not reach."""
        temp_fn = AsyncMock(side_effect=RuntimeError("fail"))
        mode_fn = AsyncMock(side_effect=HomeAssistantError("fail"))
        info = _info(cur_temp=20.0, cur_mode="heat")
        with caplog.at_level(logging.DEBUG, logger=_MAINTENANCE_LOGGER):
            await restore_one(
                info,
                set_temperature_fn=temp_fn,
                set_hvac_mode_fn=mode_fn,
                get_state=_reports_a_moved_mode([info]),
            )
        assert "restoring the setpoint of climate.trv1 failed" in caplog.text
        assert "restoring the HVAC mode of climate.trv1 failed" in caplog.text
        assert all(record.exc_info for record in caplog.records)

    @pytest.mark.asyncio
    async def test_successful_restore_is_not_traced(self, caplog):
        """A restore that lands reports nothing."""
        info = _info(cur_temp=20.0, cur_mode="heat")
        with caplog.at_level(logging.DEBUG, logger=_MAINTENANCE_LOGGER):
            await restore_one(
                info,
                set_temperature_fn=AsyncMock(),
                set_hvac_mode_fn=AsyncMock(),
                get_state=_reports_a_moved_mode([info]),
            )
        assert "failed" not in caplog.text


# ═══════════════════════════════════════════════════════════════════════════
# run_valve_maintenance (full orchestrator)
# ═══════════════════════════════════════════════════════════════════════════


class TestRunValveMaintenance:
    """Tests for run valve maintenance."""

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
            get_state=_reports_a_moved_mode(infos),
            device_name="Test",
            cycle_sleep=0,
        )

        # 2 opens (100) + 2 closes (0) = 4 valve calls
        assert valve_fn.await_count == 4
        calls = [c.args for c in valve_fn.await_args_list]
        assert calls == [("trv1", 100), ("trv1", 0), ("trv1", 100), ("trv1", 0)]

    @pytest.mark.asyncio
    async def test_multiple_trvs(self):
        """Test Multiple trvs."""
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
            get_state=_reports_a_moved_mode(infos),
            device_name="Test",
            cycle_sleep=0,
        )

        # 2 TRVs × (2 open + 2 close) = 8 valve calls
        assert valve_fn.await_count == 8

    @pytest.mark.asyncio
    async def test_temp_based_cycles(self):
        """Test Temp based cycles."""
        valve_fn = AsyncMock()
        temp_fn = AsyncMock()
        mode_fn = AsyncMock()
        infos = [
            _info(entity_id="trv1", use_direct_valve=False, max_temp=30, min_temp=5)
        ]

        await run_valve_maintenance(
            infos,
            set_valve_fn=valve_fn,
            set_temperature_fn=temp_fn,
            set_hvac_mode_fn=mode_fn,
            get_state=_reports_a_moved_mode(infos),
            device_name="Test",
            cycle_sleep=0,
        )

        # 2 opens (max) + 2 closes (min) = 4 temp calls, plus 1 restore = 5
        assert temp_fn.await_count == 5
        valve_fn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_restores_after_cycles(self):
        """Test Restores after cycles."""
        valve_fn = AsyncMock(return_value=True)
        temp_fn = AsyncMock()
        mode_fn = AsyncMock()
        infos = [
            _info(
                entity_id="trv1", cur_temp=22.0, cur_mode="heat", use_direct_valve=True
            )
        ]

        await run_valve_maintenance(
            infos,
            set_valve_fn=valve_fn,
            set_temperature_fn=temp_fn,
            set_hvac_mode_fn=mode_fn,
            get_state=_reports_a_moved_mode(infos),
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
            get_state=_reports_a_moved_mode([]),
            device_name="Test",
            cycle_sleep=0,
        )

        valve_fn.assert_not_awaited()
        temp_fn.assert_not_awaited()
        mode_fn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_off_trv_without_wake_mode_is_skipped_in_temp_mode(self):
        """An OFF TRV offering no usable wake mode still gets no open/close calls."""
        valve_fn = AsyncMock()
        temp_fn = AsyncMock()
        mode_fn = AsyncMock()
        infos = [
            _info(
                entity_id="trv1",
                cur_mode="off",
                use_direct_valve=False,
                cur_temp=20.0,
                wake_mode=None,
            )
        ]

        await run_valve_maintenance(
            infos,
            set_valve_fn=valve_fn,
            set_temperature_fn=temp_fn,
            set_hvac_mode_fn=mode_fn,
            get_state=_reports_a_moved_mode(infos),
            device_name="Test",
            cycle_sleep=0,
        )

        # open/close skipped, but restore still sets temp + mode
        assert temp_fn.await_count == 1  # only restore
        mode_fn.assert_awaited_once_with("trv1", "off")

    @pytest.mark.asyncio
    async def test_off_trv_is_woken_exercised_and_switched_back_off(self):
        """An OFF TRV with a wake mode runs the full cycle and ends up off again."""
        valve_fn = AsyncMock()
        temp_fn = AsyncMock()
        mode_fn = AsyncMock()
        infos = [
            _info(
                entity_id="trv1",
                cur_mode="off",
                use_direct_valve=False,
                cur_temp=20.0,
                max_temp=30.0,
                min_temp=5.0,
                wake_mode="heat",
            )
        ]

        await run_valve_maintenance(
            infos,
            set_valve_fn=valve_fn,
            set_temperature_fn=temp_fn,
            set_hvac_mode_fn=mode_fn,
            get_state=_reports_a_moved_mode(infos),
            device_name="Test",
            cycle_sleep=0,
        )

        # Woken first, restored to off last.
        assert [c.args for c in mode_fn.await_args_list] == [
            ("trv1", "heat"),
            ("trv1", "off"),
        ]
        # 2 cycles x (open + close) + restore
        assert temp_fn.await_count == 5
        assert [c.args[1] for c in temp_fn.await_args_list] == [
            30.0,
            5.0,
            30.0,
            5.0,
            20.0,
        ]

    @pytest.mark.asyncio
    async def test_failed_wake_skips_the_temperature_cycle(self):
        """A TRV that would not wake is left out of the cycle, not written to."""
        valve_fn = AsyncMock()
        temp_fn = AsyncMock()

        async def mode_fn(entity_id, mode):
            if entity_id == "trv1" and mode == "heat":
                raise HomeAssistantError("device did not accept the mode")

        mode_mock = AsyncMock(side_effect=mode_fn)
        infos = [
            _info(
                entity_id="trv1",
                cur_mode="off",
                use_direct_valve=False,
                cur_temp=20.0,
                max_temp=30.0,
                min_temp=5.0,
                wake_mode="heat",
            ),
            _info(
                entity_id="trv2",
                cur_mode="heat",
                use_direct_valve=False,
                cur_temp=21.0,
                max_temp=30.0,
                min_temp=5.0,
            ),
        ]

        await run_valve_maintenance(
            infos,
            set_valve_fn=valve_fn,
            set_temperature_fn=temp_fn,
            set_hvac_mode_fn=mode_mock,
            get_state=_reports_a_moved_mode(infos),
            device_name="Test",
            cycle_sleep=0,
        )

        by_entity = [c.args for c in temp_fn.await_args_list]
        # trv1 sees its restore write only, never a cycle extreme.
        assert [args[1] for args in by_entity if args[0] == "trv1"] == [20.0]
        # trv2 is unaffected and runs both cycles plus its restore.
        assert [args[1] for args in by_entity if args[0] == "trv2"] == [
            30.0,
            5.0,
            30.0,
            5.0,
            21.0,
        ]
        # Both are still restored to their pre-maintenance mode.
        assert ("trv1", "off") in [c.args for c in mode_mock.await_args_list]
        assert ("trv2", "heat") in [c.args for c in mode_mock.await_args_list]

    @pytest.mark.asyncio
    async def test_no_cycle_waits_when_nothing_can_be_exercised(self, monkeypatch):
        """With an empty cycle set the run restores without sitting out sleeps."""
        slept: list[float] = []

        async def _record_sleep(seconds):
            slept.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", _record_sleep)

        temp_fn = AsyncMock()
        mode_mock = AsyncMock(
            side_effect=HomeAssistantError("device did not accept the mode")
        )
        infos = [
            _info(
                entity_id="trv1",
                cur_mode="off",
                use_direct_valve=False,
                cur_temp=20.0,
                wake_mode="heat",
            )
        ]

        await run_valve_maintenance(
            infos,
            set_valve_fn=AsyncMock(),
            set_temperature_fn=temp_fn,
            set_hvac_mode_fn=mode_mock,
            get_state=_reports_a_moved_mode(infos),
            device_name="Test",
            cycle_sleep=30,
        )

        assert slept == []
        # The restore still runs for the TRV that could not be woken.
        temp_fn.assert_awaited_once_with("trv1", 20.0)

    @pytest.mark.asyncio
    async def test_no_cycle_waits_for_an_unreachable_off_trv(self, monkeypatch):
        """An off TRV that offered no wake mode is not worth waiting for."""
        slept: list[float] = []

        async def _record_sleep(seconds):
            slept.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", _record_sleep)

        temp_fn = AsyncMock()
        mode_fn = AsyncMock()
        infos = [
            _info(
                entity_id="trv1",
                cur_mode="off",
                use_direct_valve=False,
                cur_temp=20.0,
                wake_mode=None,
            )
        ]

        await run_valve_maintenance(
            infos,
            set_valve_fn=AsyncMock(),
            set_temperature_fn=temp_fn,
            set_hvac_mode_fn=mode_fn,
            get_state=_reports_a_moved_mode(infos),
            device_name="Test",
            cycle_sleep=30,
        )

        assert slept == []
        # Never woken, never cycled, but still restored.
        temp_fn.assert_awaited_once_with("trv1", 20.0)
        mode_fn.assert_awaited_once_with("trv1", "off")

    @pytest.mark.asyncio
    async def test_no_cycle_waits_without_any_trv(self, monkeypatch):
        """An empty snapshot list is the same case and must not wait either."""
        slept: list[float] = []

        async def _record_sleep(seconds):
            slept.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", _record_sleep)

        await run_valve_maintenance(
            [],
            set_valve_fn=AsyncMock(),
            set_temperature_fn=AsyncMock(),
            set_hvac_mode_fn=AsyncMock(),
            get_state=_reports_a_moved_mode([]),
            device_name="Test",
            cycle_sleep=30,
        )

        assert slept == []

    @pytest.mark.asyncio
    async def test_off_trv_on_direct_valve_is_not_woken(self):
        """A valve-driven TRV is exercised through the valve without being woken."""
        valve_fn = AsyncMock(return_value=True)
        temp_fn = AsyncMock()
        mode_fn = AsyncMock()
        infos = [
            _info(
                entity_id="trv1",
                cur_mode="off",
                use_direct_valve=True,
                cur_temp=20.0,
                wake_mode=None,
            )
        ]

        await run_valve_maintenance(
            infos,
            set_valve_fn=valve_fn,
            set_temperature_fn=temp_fn,
            set_hvac_mode_fn=mode_fn,
            get_state=_reports_a_moved_mode(infos),
            device_name="Test",
            cycle_sleep=0,
        )

        assert [c.args[1] for c in valve_fn.await_args_list] == [100, 0, 100, 0]
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
            get_state=_reports_a_moved_mode(infos),
            device_name="Test",
            cycle_sleep=0,
        )


# ═══════════════════════════════════════════════════════════════════════════
# pick_wake_mode / wake_step
# ═══════════════════════════════════════════════════════════════════════════


class TestPickWakeMode:
    """Tests for choosing the mode an off TRV is exercised in."""

    def test_running_trv_needs_no_wake(self):
        """A TRV that is not off is already reachable through setpoints."""
        assert pick_wake_mode("heat", False, ["off", "heat"]) is None

    def test_direct_valve_trv_is_never_woken(self):
        """Valve writes reach the device while it is off."""
        assert pick_wake_mode("off", True, ["off", "heat"]) is None

    def test_prefers_heat(self):
        """HEAT wins when the device offers several usable modes."""
        assert pick_wake_mode("off", False, ["off", "auto", "heat"]) == "heat"

    def test_falls_back_to_auto(self):
        """AUTO is used by devices that report no HEAT mode."""
        assert pick_wake_mode("off", False, ["off", "auto"]) == "auto"

    def test_falls_back_to_heat_cool(self):
        """HEAT_COOL is the last usable candidate."""
        assert pick_wake_mode("off", False, ["off", "heat_cool"]) == "heat_cool"

    def test_no_usable_mode(self):
        """A device offering nothing to wake into stays untouched."""
        assert pick_wake_mode("off", False, ["off", "fan_only"]) is None

    def test_missing_hvac_modes_attribute(self):
        """A device that reports no mode list stays untouched."""
        assert pick_wake_mode("off", False, None) is None

    def test_enum_members_are_matched(self):
        """A device reporting HVACMode members is understood."""
        assert (
            pick_wake_mode("off", False, [HVACMode.OFF, HVACMode.HEAT]) == HVACMode.HEAT
        )

    def test_enum_repr_spelling_is_matched(self):
        """``HVACMode.HEAT`` spelled out as a string still wakes the TRV."""
        assert (
            pick_wake_mode("off", False, ["HVACMode.OFF", "HVACMode.HEAT"])
            == HVACMode.HEAT
        )

    def test_enum_repr_spelling_keeps_the_preference_order(self):
        """Preference is read on normalized values, not on the raw spelling."""
        assert (
            pick_wake_mode("off", False, ["HVACMode.AUTO", "HVACMode.HEAT"])
            == HVACMode.HEAT
        )

    def test_enum_repr_spelling_falls_back(self):
        """A spelled-out list without HEAT falls through to the next candidate."""
        assert (
            pick_wake_mode("off", False, ["HVACMode.OFF", "HVACMode.HEAT_COOL"])
            == HVACMode.HEAT_COOL
        )


class TestWakeStep:
    """Tests for the wake step itself."""

    @pytest.mark.asyncio
    async def test_sets_wake_mode(self):
        """The step switches the device into its wake mode."""
        mode_fn = AsyncMock()
        await wake_step(
            _info(entity_id="trv1", wake_mode="heat"), set_hvac_mode_fn=mode_fn
        )
        mode_fn.assert_awaited_once_with("trv1", "heat")

    @pytest.mark.asyncio
    async def test_noop_without_wake_mode(self):
        """No wake mode means no mode write."""
        mode_fn = AsyncMock()
        await wake_step(
            _info(entity_id="trv1", wake_mode=None), set_hvac_mode_fn=mode_fn
        )
        mode_fn.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════════════════
# mode_needs_restoring
# ═══════════════════════════════════════════════════════════════════════════


class TestModeNeedsRestoring:
    """Which TRVs get their HVAC mode written back after a run.

    A device that reports a single HVAC mode implements no setter for it,
    so Home Assistant's climate component raises ``NotImplementedError`` on
    a write of the mode the device is already in. The run has no reason to
    make that write: it never moved the mode of such a device.
    """

    def test_a_woken_trv_is_restored_whatever_it_reports(self):
        """The wake mode alone decides; a stale reading cannot undo it.

        ``wake_step`` wrote the mode, so it has to go back. The device may
        not have published the change yet, and a reading that still shows
        the mode the run started from would otherwise leave the TRV in the
        mode it was woken into.
        """
        info = _info(cur_mode=HVACMode.OFF, wake_mode="heat")
        assert mode_needs_restoring(info, _reports(HVACMode.OFF), woken=True) is True

    def test_a_trv_still_in_its_own_mode_is_left_alone(self):
        """Nothing moved the mode, so writing it back buys nothing."""
        info = _info(cur_mode="heat", wake_mode=None)
        assert mode_needs_restoring(info, _reports("heat"), woken=False) is False

    def test_a_mode_that_moved_without_the_wake_step_is_restored(self):
        """A device that switched itself on is put back.

        A valve-driven TRV is never woken, and a device that answers a
        valve write by leaving ``off`` moved a mode the run has to undo.
        """
        info = _info(cur_mode=HVACMode.OFF, use_direct_valve=True, wake_mode=None)
        assert mode_needs_restoring(info, _reports("heat"), woken=False) is True

    def test_a_wake_that_never_landed_decides_nothing(self):
        """A selected wake mode is an intention, not a write that happened.

        ``wake_step`` can raise, and the orchestrator already drops such a
        TRV from the cycle. Its mode is then still the one the snapshot was
        taken in, so repeating the write that just failed buys nothing.
        """
        info = _info(cur_mode=HVACMode.OFF, wake_mode="heat")
        assert mode_needs_restoring(info, _reports(HVACMode.OFF), woken=False) is False

    def test_a_trv_without_a_state_is_restored(self):
        """No reading is no evidence that the mode is where it belongs."""
        info = _info(cur_mode="heat", wake_mode=None)
        assert mode_needs_restoring(info, _reports(None), woken=False) is True


class TestRestoreLeavesAnUnmovedModeAlone:
    """The guard as the restore applies it, setpoint included."""

    @pytest.mark.asyncio
    async def test_setpoint_is_restored_and_the_mode_is_not_written(self):
        """A single-mode TRV gets its setpoint back and no mode write."""
        temp_fn = AsyncMock()
        mode_fn = AsyncMock()
        info = _info(cur_temp=21.5, cur_mode="heat", wake_mode=None)
        await restore_one(
            info,
            set_temperature_fn=temp_fn,
            set_hvac_mode_fn=mode_fn,
            get_state=_reports("heat"),
        )
        temp_fn.assert_awaited_once_with("climate.trv1", 21.5)
        mode_fn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_full_run_writes_no_mode_to_a_trv_that_kept_it(self):
        """End to end: the run leaves such a TRV's mode untouched.

        The valve cycle drives the TRV through a number entity, so nothing
        in the run asks its mode to move.
        """
        mode_fn = AsyncMock()
        infos = [_info(entity_id="trv1", cur_mode="heat", use_direct_valve=True)]
        await run_valve_maintenance(
            infos,
            set_valve_fn=AsyncMock(return_value=True),
            set_temperature_fn=AsyncMock(),
            set_hvac_mode_fn=mode_fn,
            get_state=_reports("heat"),
            device_name="Test",
            cycle_sleep=0,
        )
        mode_fn.assert_not_awaited()
