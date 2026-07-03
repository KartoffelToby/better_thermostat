"""Tests that balance calibration keeps working under SENSOR_FALLBACK.

When the external room sensor is dead (``cur_temp`` is ``None``) and the
control mode ladder sits on SENSOR_FALLBACK, ``effective_room_temp()``
substitutes the mean of the TRV-internal temperatures. The balance
computations must consult that fallback instead of bailing out on the
bare ``cur_temp`` reading — and must still skip when no temperature is
available at all.
"""

from unittest.mock import MagicMock

from custom_components.better_thermostat.calibration import (
    _compute_mpc_balance,
    _compute_pid_balance,
    _compute_tpi_balance,
)
from custom_components.better_thermostat.core.fsm.control_mode import ControlMode
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.calibration.mpc import (
    MpcState,
    build_mpc_key,
)
from custom_components.better_thermostat.utils.calibration.pid import (
    PIDState,
    build_pid_key,
)
from custom_components.better_thermostat.utils.calibration.tpi import (
    TpiState,
    build_tpi_key,
)


class _StateStub:
    """Minimal stand-in for the state manager's controller accessors."""

    def __init__(self) -> None:
        self.mpc: dict[str, MpcState] = {}
        self.pid: dict[str, PIDState] = {}
        self.tpi: dict[str, TpiState] = {}

    @property
    def state(self):
        return self

    def get_mpc(self, key: str) -> MpcState:
        """Return the stored MPC state for ``key``, creating it on first access."""
        return self.mpc.setdefault(key, MpcState())

    def set_mpc(self, key: str, mpc: MpcState) -> None:
        """Store ``mpc`` under ``key``."""
        self.mpc[key] = mpc

    def get_pid(self, key: str) -> PIDState:
        """Return the stored PID state for ``key``, creating it on first access."""
        return self.pid.setdefault(key, PIDState())

    def set_pid(self, key: str, pid: PIDState) -> None:
        """Store ``pid`` under ``key``."""
        self.pid[key] = pid

    def get_tpi(self, key: str) -> TpiState:
        """Return the stored TPI state for ``key``, creating it on first access."""
        return self.tpi.setdefault(key, TpiState())

    def set_tpi(self, key: str, tpi: TpiState) -> None:
        """Store ``tpi`` under ``key``."""
        self.tpi[key] = tpi


def _make_bt(state_mgr: _StateStub, trv_temp: float | None) -> MagicMock:
    """Return a BetterThermostat mock in SENSOR_FALLBACK with a dead room sensor."""
    bt = MagicMock()
    bt.device_name = "Test BT"
    bt.unique_id = "uid"
    bt.bt_target_temp = 22.0
    bt.cur_temp = None
    bt.cur_temp_filtered = None
    bt.temp_slope = 0.0
    bt.tolerance = 0.0
    bt.window_open = False
    bt.bt_hvac_mode = "heat"
    bt.outdoor_sensor = None
    bt.weather_entity = None
    bt.hass.states.get.return_value = None
    bt.kernel_state.control_mode.mode = ControlMode.SENSOR_FALLBACK
    bt.real_trvs = {
        "climate.trv": Trv.from_legacy_dict(
            "climate.trv",
            {
                "advanced": {},
                "current_temperature": trv_temp,
                "min_temp": 5.0,
                "max_temp": 30.0,
            },
        )
    }
    bt.state_mgr = state_mgr
    return bt


def test_mpc_balance_uses_trv_temperature_when_room_sensor_is_dead() -> None:
    """MPC keeps computing on the TRV-internal temperature under SENSOR_FALLBACK."""
    state_mgr = _StateStub()
    bt = _make_bt(state_mgr, trv_temp=21.0)

    payload, _ = _compute_mpc_balance(bt, "climate.trv")

    assert payload is not None
    assert bt.real_trvs["climate.trv"].calibration_balance is not None
    assert build_mpc_key(bt, "climate.trv") in state_mgr.mpc


def test_tpi_balance_uses_trv_temperature_when_room_sensor_is_dead() -> None:
    """TPI keeps computing on the TRV-internal temperature under SENSOR_FALLBACK."""
    state_mgr = _StateStub()
    bt = _make_bt(state_mgr, trv_temp=21.0)

    payload, _ = _compute_tpi_balance(bt, "climate.trv")

    assert payload is not None
    assert bt.real_trvs["climate.trv"].calibration_balance is not None
    # error = 22.0 - 21.0 = 1.0 K, duty = coef_int * 1.0 * 100 = 60
    assert state_mgr.tpi[build_tpi_key(bt, "climate.trv")].last_percent == 60.0


def test_pid_balance_uses_trv_temperature_when_room_sensor_is_dead() -> None:
    """PID keeps computing on the TRV-internal temperature under SENSOR_FALLBACK."""
    state_mgr = _StateStub()
    bt = _make_bt(state_mgr, trv_temp=21.0)

    percent, _ = _compute_pid_balance(bt, "climate.trv")

    assert percent is not None
    assert bt.real_trvs["climate.trv"].calibration_balance is not None
    # error = |target - trv temp| = |22.0 - 21.0|
    assert state_mgr.pid[build_pid_key(bt, "climate.trv")].last_abs_error == 1.0


def test_mpc_balance_skips_when_no_temperature_is_available() -> None:
    """MPC still bails out when neither room nor TRV temperature exists."""
    state_mgr = _StateStub()
    bt = _make_bt(state_mgr, trv_temp=None)

    payload, supports_valve = _compute_mpc_balance(bt, "climate.trv")

    assert payload is None
    assert supports_valve is False
    assert bt.real_trvs["climate.trv"].calibration_balance is None


def test_tpi_balance_skips_when_no_temperature_is_available() -> None:
    """TPI still bails out when neither room nor TRV temperature exists."""
    state_mgr = _StateStub()
    bt = _make_bt(state_mgr, trv_temp=None)

    payload, supports_valve = _compute_tpi_balance(bt, "climate.trv")

    assert payload is None
    assert supports_valve is False
    assert bt.real_trvs["climate.trv"].calibration_balance is None


def test_pid_balance_skips_when_no_temperature_is_available() -> None:
    """PID still bails out when neither room nor TRV temperature exists."""
    state_mgr = _StateStub()
    bt = _make_bt(state_mgr, trv_temp=None)

    percent, supports_valve = _compute_pid_balance(bt, "climate.trv")

    assert percent is None
    assert supports_valve is False
    assert bt.real_trvs["climate.trv"].calibration_balance is None
