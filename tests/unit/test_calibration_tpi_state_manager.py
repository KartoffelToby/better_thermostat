"""Tests that TPI calibration reads and writes state through the state manager."""

from unittest.mock import MagicMock

from custom_components.better_thermostat.calibration import _compute_tpi_balance
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.calibration.tpi import (
    TpiState,
    build_tpi_key,
)


class _TpiStateStub:
    """Minimal stand-in for the state manager's TPI accessors."""

    def __init__(self) -> None:
        self.tpi: dict[str, TpiState] = {}

    def get_tpi(self, key: str) -> TpiState:
        """Return the stored state for ``key``, creating it on first access."""
        return self.tpi.setdefault(key, TpiState())

    def set_tpi(self, key: str, tpi: TpiState) -> None:
        """Store ``tpi`` under ``key``."""
        self.tpi[key] = tpi


def _make_bt(state_mgr: _TpiStateStub) -> MagicMock:
    """Return a BetterThermostat mock wired for a single heating TRV."""
    bt = MagicMock()
    bt.device_name = "Test BT"
    bt.unique_id = "uid"
    bt.bt_target_temp = 22.0
    bt.cur_temp = 20.0
    bt.window_open = False
    bt.bt_hvac_mode = "heat"
    bt.outdoor_sensor = None
    bt.weather_entity = None
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


def test_tpi_balance_persists_state_in_state_manager() -> None:
    """The computed TPI state lands in the state manager under the TPI key."""
    state_mgr = _TpiStateStub()
    bt = _make_bt(state_mgr)

    _compute_tpi_balance(bt, "climate.trv")

    key = build_tpi_key(bt, "climate.trv")
    assert key in state_mgr.tpi
    # error = 2.0 K, duty = coef_int * 2.0 * 100 = 120 -> clamped to 100
    assert state_mgr.tpi[key].last_percent == 100.0


def test_tpi_balance_threads_the_same_state_across_calls() -> None:
    """Repeated calls keep accumulating on the state manager's state object."""
    state_mgr = _TpiStateStub()
    bt = _make_bt(state_mgr)
    key = build_tpi_key(bt, "climate.trv")

    _compute_tpi_balance(bt, "climate.trv")
    first = state_mgr.tpi[key]

    _compute_tpi_balance(bt, "climate.trv")
    assert state_mgr.tpi[key] is first
