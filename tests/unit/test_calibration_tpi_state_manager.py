"""Tests that TPI calibration reads and writes state through the state manager."""

from unittest.mock import MagicMock, patch

from custom_components.better_thermostat.calibration import _compute_tpi_balance
from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.calibration.tpi import (
    TpiState,
    build_tpi_key,
)
from tests.factories import ThermostatStandIn, make_state


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


def _make_bt(state_mgr: _TpiStateStub) -> ThermostatStandIn:
    """Return a BetterThermostat mock wired for a single heating TRV."""
    bt = ThermostatStandIn()
    bt.kernel_state = make_state()
    bt.device_name = "Test BT"
    bt.unique_id = "uid"
    bt.heat_target_temperature = 22.0
    bt.room_temperature = 20.0
    bt.window_open = False
    bt.contact_open = False
    bt.bt_hvac_mode = "heat"
    bt.clock = FakeClock()
    bt.outdoor_sensor_entity_id = None
    bt.weather_entity_id = None
    bt.real_trvs = {
        "climate.trv": Trv(
            entity_id="climate.trv",
            advanced={},
            current_temperature=21.0,
            min_temp=5.0,
            max_temp=30.0,
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


def test_tpi_sanitized_state_is_persisted_when_compute_raises() -> None:
    """The healed state replaces the poisoned one even on a compute failure."""
    state_mgr = _TpiStateStub()
    bt = _make_bt(state_mgr)
    key = build_tpi_key(bt, "climate.trv")
    state_mgr.tpi[key] = TpiState(last_percent=float("nan"))

    with patch(
        "custom_components.better_thermostat.calibration.compute_tpi",
        side_effect=ValueError("boom"),
    ):
        payload, supports_valve = _compute_tpi_balance(bt, "climate.trv")

    assert payload is None
    assert supports_valve is False
    stored = state_mgr.tpi[key]
    assert stored.last_percent is None  # sanitized default, not NaN


def test_tpi_balance_without_a_state_store_publishes_no_valve() -> None:
    """Before the store is loaded there is no state to run the controller on."""
    bt = _make_bt(_TpiStateStub())
    bt.state_mgr = None

    payload, supports_valve = _compute_tpi_balance(bt, "climate.trv")

    assert payload is None
    assert supports_valve is False
    assert bt.real_trvs["climate.trv"].calibration_balance is None


def test_tpi_failed_compute_on_a_healthy_state_leaves_the_store_alone() -> None:
    """Only a healed state is written back after a failed compute."""
    state_mgr = _TpiStateStub()
    bt = _make_bt(state_mgr)
    healthy = state_mgr.get_tpi(build_tpi_key(bt, "climate.trv"))
    state_mgr.set_tpi = MagicMock(wraps=state_mgr.set_tpi)

    with patch(
        "custom_components.better_thermostat.calibration.compute_tpi",
        side_effect=ValueError("boom"),
    ):
        payload, _ = _compute_tpi_balance(bt, "climate.trv")

    assert payload is None
    state_mgr.set_tpi.assert_not_called()
    assert state_mgr.tpi[build_tpi_key(bt, "climate.trv")] is healthy
