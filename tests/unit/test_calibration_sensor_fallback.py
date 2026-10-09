"""Tests that balance calibration keeps working under SENSOR_FALLBACK.

When the external room sensor is dead (``room_temperature`` is ``None``) and the
control mode ladder sits on SENSOR_FALLBACK, ``effective_room_temperature()``
substitutes the mean of the TRV-internal temperatures. The balance
computations must consult that fallback instead of bailing out on the
bare ``room_temperature`` reading — and must still skip when no temperature is
available at all.
"""

from unittest.mock import patch

from homeassistant.core import State

from custom_components.better_thermostat.calibration import (
    _compute_mpc_balance,
    _compute_mpc_v2_balance,
    _compute_pid_balance,
    _compute_tpi_balance,
    _record_mpc_v2_reid_sample,
)
from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.fsm.control_mode import (
    ControlMode,
    ControlModeState,
)
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.calibration.mpc import (
    MpcState,
    build_mpc_key,
)
from custom_components.better_thermostat.utils.calibration.mpc_v2 import MpcV2State
from custom_components.better_thermostat.utils.calibration.pid import (
    PIDState,
    build_pid_key,
)
from custom_components.better_thermostat.utils.calibration.tpi import (
    TpiState,
    build_tpi_key,
)
from custom_components.better_thermostat.utils.state_manager import MpcV2ReidRuntime
from tests.factories import ThermostatStandIn, make_state


class _StateStub:
    """Minimal stand-in for the state manager's controller accessors."""

    def __init__(self) -> None:
        self.mpc: dict[str, MpcState] = {}
        self.pid: dict[str, PIDState] = {}
        self.tpi: dict[str, TpiState] = {}
        self.mpc_v2_live: dict[str, MpcV2State] = {}
        self.mpc_v2_reid_runtime: dict[str, MpcV2ReidRuntime] = {}
        self.mpc_v2_reid: dict[str, object] = {}

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

    def get_mpc_v2_live(self, key: str, params) -> MpcV2State:
        """Return the live MPC v2 state for ``key``, creating it on first access."""
        return self.mpc_v2_live.setdefault(key, MpcV2State())

    def set_mpc_v2_live(self, key: str, state: MpcV2State) -> None:
        """Store the live MPC v2 state under ``key``."""
        self.mpc_v2_live[key] = state

    def get_mpc_v2_reid(self, key: str):
        """Return the persisted re-identification result (none in these tests)."""
        return None

    def get_mpc_v2_reid_runtime(self, key: str) -> MpcV2ReidRuntime:
        """Return the in-memory re-ID collection state for ``key``."""
        return self.mpc_v2_reid_runtime.setdefault(key, MpcV2ReidRuntime())


def _make_bt(state_mgr: _StateStub, trv_temperature: float | None) -> ThermostatStandIn:
    """Return a BetterThermostat mock in SENSOR_FALLBACK with a dead room sensor."""
    bt = ThermostatStandIn()
    bt.device_name = "Test BT"
    bt.unique_id = "uid"
    bt.heat_target_temperature = 22.0
    bt.room_temperature = None
    bt.room_temperature_filtered = None
    bt.temperature_slope = 0.0
    bt.tolerance = 0.0
    bt.window_open = False
    bt.door_open = False
    bt.contact_open = False
    bt.bt_hvac_mode = "heat"
    bt.outdoor_sensor_entity_id = None
    bt.weather_entity_id = None
    bt.heating_power = None
    bt.heat_loss_rate = None
    bt.clock = FakeClock(monotonic_value=100.0)
    # The TRV itself is reachable and reports its temperature; every other
    # entity is absent.
    bt.hass.states.get.side_effect = lambda entity_id: (
        State(
            entity_id,
            "heat",
            {} if trv_temperature is None else {"current_temperature": trv_temperature},
        )
        if entity_id == "climate.trv"
        else None
    )
    bt.hass.config.units.temperature_unit = "°C"
    bt.kernel_state = make_state(
        control_mode=ControlModeState(mode=ControlMode.SENSOR_FALLBACK)
    )
    bt.real_trvs = {
        "climate.trv": Trv(
            entity_id="climate.trv",
            advanced={},
            current_temperature=trv_temperature,
            min_temp=5.0,
            max_temp=30.0,
        )
    }
    bt.state_mgr = state_mgr
    return bt


def test_mpc_balance_uses_trv_temperature_when_room_sensor_is_dead() -> None:
    """MPC keeps computing on the TRV-internal temperature under SENSOR_FALLBACK."""
    state_mgr = _StateStub()
    bt = _make_bt(state_mgr, trv_temperature=21.0)

    payload, _ = _compute_mpc_balance(bt, "climate.trv")

    assert payload is not None
    assert bt.real_trvs["climate.trv"].calibration_balance is not None
    assert build_mpc_key(bt, "climate.trv") in state_mgr.mpc


def test_tpi_balance_uses_trv_temperature_when_room_sensor_is_dead() -> None:
    """TPI keeps computing on the TRV-internal temperature under SENSOR_FALLBACK."""
    state_mgr = _StateStub()
    bt = _make_bt(state_mgr, trv_temperature=21.0)

    payload, _ = _compute_tpi_balance(bt, "climate.trv")

    assert payload is not None
    assert bt.real_trvs["climate.trv"].calibration_balance is not None
    # error = 22.0 - 21.0 = 1.0 K, duty = coef_int * 1.0 * 100 = 60
    assert state_mgr.tpi[build_tpi_key(bt, "climate.trv")].last_percent == 60.0


def test_pid_balance_uses_trv_temperature_when_room_sensor_is_dead() -> None:
    """PID keeps computing on the TRV-internal temperature under SENSOR_FALLBACK."""
    state_mgr = _StateStub()
    bt = _make_bt(state_mgr, trv_temperature=21.0)

    percent, _ = _compute_pid_balance(bt, "climate.trv")

    assert percent is not None
    assert bt.real_trvs["climate.trv"].calibration_balance is not None
    # error = |target - trv temperature| = |22.0 - 21.0|
    assert state_mgr.pid[build_pid_key(bt, "climate.trv")].last_abs_error == 1.0


def test_mpc_balance_skips_when_no_temperature_is_available() -> None:
    """MPC still bails out when neither room nor TRV temperature exists."""
    state_mgr = _StateStub()
    bt = _make_bt(state_mgr, trv_temperature=None)

    payload, supports_valve = _compute_mpc_balance(bt, "climate.trv")

    assert payload is None
    assert supports_valve is False
    assert bt.real_trvs["climate.trv"].calibration_balance is None


def test_tpi_balance_skips_when_no_temperature_is_available() -> None:
    """TPI still bails out when neither room nor TRV temperature exists."""
    state_mgr = _StateStub()
    bt = _make_bt(state_mgr, trv_temperature=None)

    payload, supports_valve = _compute_tpi_balance(bt, "climate.trv")

    assert payload is None
    assert supports_valve is False
    assert bt.real_trvs["climate.trv"].calibration_balance is None


def test_pid_balance_skips_when_no_temperature_is_available() -> None:
    """PID still bails out when neither room nor TRV temperature exists."""
    state_mgr = _StateStub()
    bt = _make_bt(state_mgr, trv_temperature=None)

    percent, supports_valve = _compute_pid_balance(bt, "climate.trv")

    assert percent is None
    assert supports_valve is False
    assert bt.real_trvs["climate.trv"].calibration_balance is None


def test_mpc_v2_balance_uses_trv_temperature_when_room_sensor_is_dead() -> None:
    """MPC v2 keeps computing on the TRV-internal temperature under SENSOR_FALLBACK."""
    state_mgr = _StateStub()
    bt = _make_bt(state_mgr, trv_temperature=21.0)

    payload, _ = _compute_mpc_v2_balance(bt, "climate.trv")

    assert payload is not None
    assert bt.real_trvs["climate.trv"].calibration_balance is not None


def test_mpc_v2_balance_skips_when_no_temperature_is_available() -> None:
    """MPC v2 still bails out when neither room nor TRV temperature exists."""
    state_mgr = _StateStub()
    bt = _make_bt(state_mgr, trv_temperature=None)

    payload, supports_valve = _compute_mpc_v2_balance(bt, "climate.trv")

    assert payload is None
    assert supports_valve is False
    assert bt.real_trvs["climate.trv"].calibration_balance is None


def test_mpc_v2_compute_treats_open_door_as_open_contact() -> None:
    """The controller input reports an open contact when only the door is open."""
    state_mgr = _StateStub()
    bt = _make_bt(state_mgr, trv_temperature=21.0)
    bt.window_open = False
    bt.door_open = True
    bt.contact_open = bool(bt.window_open) or bool(bt.door_open)

    captured: dict[str, object] = {}

    def _fake_compute(inp, params, state=None, **kwargs):
        captured["window_open"] = inp.window_open
        return None, MpcV2State()

    with patch(
        "custom_components.better_thermostat.calibration.compute_mpc_v2",
        side_effect=_fake_compute,
    ):
        _compute_mpc_v2_balance(bt, "climate.trv")

    assert captured["window_open"] is True


def test_reid_sample_records_open_door_as_open_contact() -> None:
    """A door-open sample is flagged so the fit excludes the segment.

    Free-cooldown fitting relies on the ``window_open`` flag to cut
    segments; a door open with the window shut must set it as well,
    while ``T_room`` keeps coming from the real room sensor.
    """
    state_mgr = _StateStub()
    bt = _make_bt(state_mgr, trv_temperature=21.0)
    # Sampling is gated to the OPTIMAL rung; the door flag is orthogonal.
    bt.kernel_state = make_state()
    bt.room_temperature = 20.5
    bt.window_open = False
    bt.door_open = True
    bt.contact_open = bool(bt.window_open) or bool(bt.door_open)

    _record_mpc_v2_reid_sample(
        bt,
        "key",
        applied_valve_percent=40.0,
        trv_temperature=21.0,
        outdoor_temperature=5.0,
    )

    samples = state_mgr.get_mpc_v2_reid_runtime("key").buffer.samples
    assert len(samples) == 1
    assert samples[0].window_open is True
    assert samples[0].T_room == 20.5


def test_reid_sample_without_a_confirmed_valve_reading_records_nothing() -> None:
    """A cycle whose valve position was never confirmed leaves the buffer empty.

    The re-identification fit reads ``u_frac`` as the valve input the room
    actually saw. A group pass and an unconfirmed write both leave the
    confirmed position at ``None``, and the proposal that was computed for
    that cycle is not evidence the valve reached it.
    """
    state_mgr = _StateStub()
    bt = _make_bt(state_mgr, trv_temperature=21.0)
    bt.kernel_state = make_state()
    bt.room_temperature = 20.5

    _record_mpc_v2_reid_sample(
        bt,
        "key",
        applied_valve_percent=None,
        trv_temperature=21.0,
        outdoor_temperature=5.0,
    )

    assert state_mgr.get_mpc_v2_reid_runtime("key").buffer.samples == []
