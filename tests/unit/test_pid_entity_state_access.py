"""PID number and auto-tune switch read/write state through the StateManager.

The entities keep what the user sets on the TRV's loop entry and read the
gains in use from it and the bucket of the current target; without a
manager (startup failure) they fall back to defaults and refuse writes
instead of crashing.
"""

from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest

from custom_components.better_thermostat.number import BetterThermostatPIDNumber
from custom_components.better_thermostat.switch import BetterThermostatPIDAutoTuneSwitch
from custom_components.better_thermostat.utils.calibration.pid import (
    DEFAULT_PID_AUTO_TUNE,
    DEFAULT_PID_KD,
    DEFAULT_PID_KI,
    DEFAULT_PID_KP,
    PIDState,
)
from tests.factories import ThermostatStandIn

_KEY = "uid:climate.trv:t21.0"
_LOOP = "uid:climate.trv"


@pytest.fixture(autouse=True)
def announced() -> Iterator[MagicMock]:
    """Record what the switch tells the thermostat's entities."""
    with patch(
        "custom_components.better_thermostat.switch.announce_learned_state"
    ) as announce:
        yield announce


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
    bt = ThermostatStandIn()
    bt.unique_id = "uid"
    bt.heat_target_temperature = 21.0
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

    @pytest.mark.parametrize(
        ("gain", "default", "bounds"),
        [
            ("kp", DEFAULT_PID_KP, (0.0, 1000.0, 0.1)),
            ("ki", DEFAULT_PID_KI, (0.0, 100.0, 0.001)),
            ("kd", DEFAULT_PID_KD, (0.0, 10000.0, 1.0)),
        ],
    )
    def test_a_gain_not_learned_yet_shows_its_own_default(self, gain, default, bounds):
        """A state that holds other gains shows this one at its default.

        Each gain offers its own range and step: their scales differ by
        orders of magnitude.
        """
        bt = _make_bt()
        others = {f"pid_{name}": 7.0 for name in ("kp", "ki", "kd") if name != gain}
        bt.state_mgr.pid[_KEY] = PIDState(**others)
        number = BetterThermostatPIDNumber(bt, "climate.trv", gain, False)

        assert number.native_value == default
        assert (
            number.native_min_value,
            number.native_max_value,
            number.native_step,
        ) == bounds

    @pytest.mark.asyncio
    async def test_set_writes_the_trv_and_the_current_bucket(self):
        """Setting a gain writes the TRV's entry and the current bucket.

        The gains learned at other targets stay as they are.
        """
        bt = _make_bt()
        bt.state_mgr.pid["uid:climate.trv:t20.0"] = PIDState(pid_kp=1.0)
        number = self._make(bt)

        await number.async_set_native_value(55.0)

        assert bt.state_mgr.pid[_LOOP].pid_kp == 55.0
        assert bt.state_mgr.pid[_KEY].pid_kp == 55.0
        assert bt.state_mgr.pid["uid:climate.trv:t20.0"].pid_kp == 1.0
        assert bt.state_mgr.dirty is True
        bt.schedule_save_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_gain_set_with_auto_tune_off_shows_at_a_new_target(self):
        """With auto-tuning off a gain set by hand holds at every target."""
        bt = _make_bt()
        bt.state_mgr.pid["uid:climate.trv:t19.0"] = PIDState(pid_kp=45.0)
        number = self._make(bt)
        bt.state_mgr.pid[_LOOP] = PIDState(auto_tune=False)

        await number.async_set_native_value(150.0)
        bt.heat_target_temperature = 19.0
        assert number.native_value == 150.0
        bt.heat_target_temperature = 23.0
        assert number.native_value == 150.0

    @pytest.mark.asyncio
    async def test_a_gain_set_with_auto_tune_on_seeds_a_new_target(self):
        """With auto-tuning on, a learned gain wins where there is one."""
        bt = _make_bt()
        bt.state_mgr.pid["uid:climate.trv:t19.0"] = PIDState(pid_kp=45.0)
        number = self._make(bt)

        await number.async_set_native_value(150.0)
        bt.heat_target_temperature = 19.0
        assert number.native_value == 45.0
        bt.heat_target_temperature = 23.0
        assert number.native_value == 150.0

    def test_turning_auto_tune_off_keeps_the_learned_gain(self, announced: MagicMock):
        """Auto-tuning off freezes the gain shown, not an older start value.

        The user set Kp 100 as a start, auto-tuning learned 72 at 21 °C.
        After the switch goes off the number shows 72 at this target and
        at one never visited.
        """
        bt = _make_bt()
        bt.state_mgr.pid[_LOOP] = PIDState(auto_tune=True, pid_kp=100.0)
        bt.state_mgr.pid[_KEY] = PIDState(pid_kp=72.0)
        number = self._make(bt)
        switch = BetterThermostatPIDAutoTuneSwitch(bt, "climate.trv", True)
        switch.async_write_ha_state = MagicMock()

        switch._update_state(False)

        announced.assert_called_once_with(switch.hass, "uid")
        assert number.native_value == 72.0
        bt.heat_target_temperature = 23.0
        assert number.native_value == 72.0

    def test_after_an_upgrade_shows_the_gain_set_by_hand(self):
        """Before the first cycle the number resolves the stored buckets.

        A store from before the loop entry holds the switch flag and the
        hand-set gain on the bucket it was set at; the bucket of the
        current target was created later and holds neither.
        """
        bt = _make_bt()
        bt.state_mgr.pid["uid:climate.trv:t19.0"] = PIDState(
            auto_tune=False, pid_kp=150.0, pid_last_time=900.0
        )
        bt.state_mgr.pid[_KEY] = PIDState(pid_kp=60.0, pid_last_time=950.0)

        assert self._make(bt).native_value == 150.0
        assert _LOOP not in bt.state_mgr.pid

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

    def test_after_an_upgrade_reads_the_flag_of_the_stored_buckets(self):
        """Before the first cycle the switch resolves the stored buckets.

        The flag sits on the buckets that existed when the switch was
        turned off; the bucket of the current target was created later.
        """
        bt = _make_bt()
        bt.state_mgr.pid["uid:climate.trv:t19.0"] = PIDState(
            auto_tune=False, pid_last_time=900.0
        )
        bt.state_mgr.pid[_KEY] = PIDState(pid_last_time=950.0)

        assert self._make(bt).is_on is False
        assert _LOOP not in bt.state_mgr.pid

    def test_update_holds_at_every_target_of_this_trv_only(self):
        """Toggling sets the TRV's flag, which shows at an unvisited target too.

        Another TRV of the same thermostat keeps its own flag.
        """
        bt = _make_bt()
        bt.state_mgr.pid = {
            "uid:climate.trv:t21.0": PIDState(),
            "uid:climate.trv:t20.0": PIDState(),
            "uid:climate.other:t21.0": PIDState(),
        }
        switch = self._make(bt)
        other = BetterThermostatPIDAutoTuneSwitch(bt, "climate.other", True)

        switch._update_state(False)

        assert bt.state_mgr.pid[_LOOP].auto_tune is False
        for target in (21.0, 20.0, 17.5):
            bt.heat_target_temperature = target
            assert switch.is_on is False
            assert other.is_on is True
        assert bt.state_mgr.dirty is True
        bt.schedule_save_state.assert_called_once()

    def test_update_without_state_manager_is_a_noop(self):
        """Toggling without a state manager neither writes nor schedules a save."""
        bt = _make_bt()
        bt.state_mgr = None
        switch = self._make(bt)

        switch._update_state(True)

        bt.schedule_save_state.assert_not_called()
