"""Tests that MPC calibration reads and writes state through the state manager."""

from unittest.mock import MagicMock

from custom_components.better_thermostat.calibration import _compute_mpc_balance
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.calibration.mpc import (
    MpcState,
    build_mpc_key,
)


class _MpcStateStub:
    """Minimal stand-in for the state manager's MPC accessors."""

    def __init__(self) -> None:
        self.mpc: dict[str, MpcState] = {}

    @property
    def state(self):
        return self

    def get_mpc(self, key: str) -> MpcState:
        """Return the stored state for ``key``, creating it on first access."""
        return self.mpc.setdefault(key, MpcState())

    def set_mpc(self, key: str, mpc: MpcState) -> None:
        """Store ``mpc`` under ``key``."""
        self.mpc[key] = mpc


def _make_bt(state_mgr: _MpcStateStub) -> MagicMock:
    """Return a BetterThermostat mock wired for a single heating TRV."""
    bt = MagicMock()
    bt.device_name = "Test BT"
    bt.unique_id = "uid"
    bt.bt_target_temp = 22.0
    bt.cur_temp = 20.0
    bt.cur_temp_filtered = None
    bt.temp_slope = 0.0
    bt.tolerance = 0.0
    bt.window_open = False
    bt.bt_hvac_mode = "heat"
    bt.outdoor_sensor = None
    bt.weather_entity = None
    bt.hass.states.get.return_value = None
    bt.real_trvs = {
        "climate.trv": Trv.from_legacy_dict(
            "climate.trv",
            {
                "advanced": {},
                "current_temperature": 21.0,
                "min_temp": 5.0,
                "max_temp": 30.0,
            },
        )
    }
    bt.state_mgr = state_mgr
    return bt


def test_mpc_balance_persists_state_in_state_manager() -> None:
    """The computed MPC state lands in the state manager under the MPC key."""
    state_mgr = _MpcStateStub()
    bt = _make_bt(state_mgr)

    _compute_mpc_balance(bt, "climate.trv")

    key = build_mpc_key(bt, "climate.trv")
    assert key in state_mgr.mpc
    assert state_mgr.mpc[key].last_integration_ts > 0.0


def test_mpc_balance_handles_multiple_trvs() -> None:
    """Multi-TRV setups aggregate TRV temperatures via attribute access."""
    state_mgr = _MpcStateStub()
    bt = _make_bt(state_mgr)
    bt.real_trvs["climate.trv2"] = Trv.from_legacy_dict(
        "climate.trv2",
        {
            "advanced": {},
            "current_temperature": 23.5,
            "min_temp": 5.0,
            "max_temp": 30.0,
        },
    )

    payload, skipped = _compute_mpc_balance(bt, "climate.trv")

    assert skipped is False
    assert payload is not None


def test_mpc_balance_threads_the_same_state_across_calls() -> None:
    """Repeated calls keep accumulating on the state manager's state object."""
    state_mgr = _MpcStateStub()
    bt = _make_bt(state_mgr)
    key = build_mpc_key(bt, "climate.trv")

    _compute_mpc_balance(bt, "climate.trv")
    first = state_mgr.mpc[key]

    _compute_mpc_balance(bt, "climate.trv")
    assert state_mgr.mpc[key] is first
