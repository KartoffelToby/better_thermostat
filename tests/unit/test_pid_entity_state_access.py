"""PID number and auto-tune switch read/write state through the StateManager.

The entities resolve the active PID state via ``build_pid_key`` and the
climate's ``state_mgr``; without a manager (startup failure) they fall back
to defaults and refuse writes instead of crashing.
"""

from unittest.mock import MagicMock

import pytest

from custom_components.better_thermostat.number import BetterThermostatPIDNumber
from custom_components.better_thermostat.switch import BetterThermostatPIDAutoTuneSwitch
from custom_components.better_thermostat.utils.calibration.pid import (
    DEFAULT_PID_AUTO_TUNE,
    DEFAULT_PID_KP,
    PIDState,
)

_KEY = "uid:climate.trv:t21.0"


class _StateMgrStub:
    """Minimal stand-in for the StateManager's PID surface."""

    def __init__(self) -> None:
        self.pid: dict[str, PIDState] = {}
        self.dirty = False

    @property
    def state(self):
        return self

    def get_pid(self, key: str) -> PIDState:
        return self.pid.setdefault(key, PIDState())

    def set_pid(self, key: str, pid: PIDState) -> None:
        self.pid[key] = pid
        self.dirty = True

    def mark_dirty(self) -> None:
        self.dirty = True


def _make_bt() -> MagicMock:
    bt = MagicMock()
    bt.unique_id = "uid"
    bt.bt_target_temp = 21.0
    bt.schedule_save_state = MagicMock()
    bt.state_mgr = _StateMgrStub()
    return bt


class TestPidNumber:
    """BetterThermostatPIDNumber reads and writes via the StateManager."""

    def _make(self, bt) -> BetterThermostatPIDNumber:
        number = BetterThermostatPIDNumber(bt, "climate.trv", "kp", False)
        number.async_write_ha_state = MagicMock()
        return number

    def test_reads_learned_gain_from_state_manager(self):
        """A learned gain in the manager is exposed as the number value."""
        bt = _make_bt()
        bt.state_mgr.pid[_KEY] = PIDState(pid_kp=123.0)
        assert self._make(bt).native_value == 123.0

    def test_missing_state_falls_back_to_default(self):
        """Without a stored state the default value is exposed."""
        bt = _make_bt()
        assert self._make(bt).native_value == DEFAULT_PID_KP

    def test_no_state_manager_falls_back_to_default(self):
        """Without a state manager the default value is exposed."""
        bt = _make_bt()
        bt.state_mgr = None
        assert self._make(bt).native_value == DEFAULT_PID_KP

    @pytest.mark.asyncio
    async def test_set_writes_only_current_bucket(self):
        """Setting a gain writes only the current bucket and marks dirty."""
        bt = _make_bt()
        bt.state_mgr.pid["uid:climate.trv:t20.0"] = PIDState(pid_kp=1.0)
        number = self._make(bt)

        await number.async_set_native_value(55.0)

        assert bt.state_mgr.pid[_KEY].pid_kp == 55.0
        assert bt.state_mgr.pid["uid:climate.trv:t20.0"].pid_kp == 1.0
        assert bt.state_mgr.dirty is True
        bt.schedule_save_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_without_state_manager_is_a_noop(self):
        """Setting without a state manager neither writes nor schedules a save."""
        bt = _make_bt()
        bt.state_mgr = None
        number = self._make(bt)

        await number.async_set_native_value(55.0)

        bt.schedule_save_state.assert_not_called()


class TestAutoTuneSwitch:
    """BetterThermostatPIDAutoTuneSwitch reads/writes auto_tune via the manager."""

    def _make(self, bt) -> BetterThermostatPIDAutoTuneSwitch:
        switch = BetterThermostatPIDAutoTuneSwitch(bt, "climate.trv", False)
        switch.async_write_ha_state = MagicMock()
        return switch

    def test_reads_auto_tune_flag(self):
        """A stored auto_tune flag is exposed as the switch state."""
        bt = _make_bt()
        bt.state_mgr.pid[_KEY] = PIDState(auto_tune=False)
        assert self._make(bt).is_on is False

    def test_missing_state_falls_back_to_default(self):
        """Without a stored state the default value is exposed."""
        bt = _make_bt()
        assert self._make(bt).is_on is DEFAULT_PID_AUTO_TUNE

    def test_no_state_manager_falls_back_to_default(self):
        """Without a state manager the default value is exposed."""
        bt = _make_bt()
        bt.state_mgr = None
        assert self._make(bt).is_on is DEFAULT_PID_AUTO_TUNE

    def test_update_sets_flag_on_all_buckets_of_this_trv(self):
        """Toggling updates every bucket of this TRV, not other TRVs."""
        bt = _make_bt()
        bt.state_mgr.pid = {
            "uid:climate.trv:t21.0": PIDState(),
            "uid:climate.trv:t20.0": PIDState(),
            "uid:climate.other:t21.0": PIDState(),
        }
        switch = self._make(bt)

        switch._update_state(False)

        assert bt.state_mgr.pid["uid:climate.trv:t21.0"].auto_tune is False
        assert bt.state_mgr.pid["uid:climate.trv:t20.0"].auto_tune is False
        assert bt.state_mgr.pid["uid:climate.other:t21.0"].auto_tune is None
        assert bt.state_mgr.dirty is True
        bt.schedule_save_state.assert_called_once()

    def test_update_seeds_active_bucket_when_none_exists(self):
        """Toggling with no stored bucket seeds the active one.

        After a PID reset (or before the first PID cycle) the map is
        empty; the toggle must still survive and be readable.
        """
        bt = _make_bt()
        switch = self._make(bt)

        switch._update_state(False)

        assert bt.state_mgr.pid[_KEY].auto_tune is False
        assert bt.state_mgr.dirty is True
        assert switch.is_on is False
        bt.schedule_save_state.assert_called_once()

    def test_update_without_state_manager_is_a_noop(self):
        """Toggling without a state manager neither writes nor schedules a save."""
        bt = _make_bt()
        bt.state_mgr = None
        switch = self._make(bt)

        switch._update_state(True)

        bt.schedule_save_state.assert_not_called()
