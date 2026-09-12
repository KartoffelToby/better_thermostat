"""An open window and an open door must suppress every calibration mode.

Each controller dispatcher in :mod:`calibration` reads the contact state off
the BT entity itself, so nothing forces the modes to consult the same flag.
A per-mode test cannot see that: a mode gated on the window alone still
passes every window case put to it. Only holding the modes side by side
against one scenario catches one that reads a narrower flag than its
siblings.

The assertion is on the valve percentage the control cycle would hand to
``set_valve``, not on the dispatcher's return value — the modes disagree on
how they express "suppressed" (``None`` versus a zero-percent command), and
the valve is what reaches the device either way.
"""

from __future__ import annotations

import importlib.util
from types import SimpleNamespace
from typing import Any

from homeassistant.components.climate.const import HVACMode
import pytest

from custom_components.better_thermostat.calibration import (
    _compute_mpc_balance,
    _compute_mpc_v2_balance,
    _compute_tpi_balance,
)
from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.fsm.control_mode import ControlMode
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.calibration.mpc import MpcState
from custom_components.better_thermostat.utils.calibration.mpc_v2 import (
    MpcV2Params,
    MpcV2State,
)
from custom_components.better_thermostat.utils.calibration.tpi import TpiState
from custom_components.better_thermostat.utils.const import (
    CalibrationMode,
    CalibrationType,
    MpcV2PlantPreset,
)
from custom_components.better_thermostat.utils.state_manager import MpcV2ReidRuntime

_HAS_DAQP = importlib.util.find_spec("daqp") is not None


class _InMemoryStateManager:
    """Stand-in for StateManager holding controller state per key in memory."""

    def __init__(self) -> None:
        """Start with empty per-key stores for every controller family."""
        self._mpc: dict[str, MpcState] = {}
        self._mpc_v2: dict[str, MpcV2State] = {}
        self._tpi: dict[str, TpiState] = {}
        self._mpc_v2_reid: dict[str, MpcV2ReidRuntime] = {}
        self.state = SimpleNamespace(mpc=self._mpc, mpc_v2_reid={})

    def get_mpc(self, key: str) -> MpcState:
        """Return the MPC v1 state for key, creating it on first use."""
        return self._mpc.setdefault(key, MpcState())

    def set_mpc(self, key: str, state: MpcState) -> None:
        """Store the MPC v1 state for key."""
        self._mpc[key] = state

    def get_mpc_v2_live(self, key: str, params: MpcV2Params) -> MpcV2State:
        """Return the live MPC v2 state for key, creating it on first use."""
        return self._mpc_v2.setdefault(key, MpcV2State())

    def set_mpc_v2_live(self, key: str, state: MpcV2State) -> None:
        """Store the live MPC v2 state for key."""
        self._mpc_v2[key] = state

    def get_mpc_v2_reid(self, key: str) -> None:
        """Report no persisted re-identification result."""
        return None

    def get_mpc_v2_reid_runtime(self, key: str) -> MpcV2ReidRuntime:
        """Return the re-ID collection state for key, creating it on first use."""
        return self._mpc_v2_reid.setdefault(key, MpcV2ReidRuntime())

    def get_tpi(self, key: str) -> TpiState:
        """Return the TPI state for key, creating it on first use."""
        return self._tpi.setdefault(key, TpiState())

    def set_tpi(self, key: str, state: TpiState) -> None:
        """Store the TPI state for key."""
        self._tpi[key] = state


def _valve_trv(entity_id: str, mode: CalibrationMode) -> Trv:
    """Build a direct-valve TRV configured for one calibration mode."""
    return Trv(
        entity_id=entity_id,
        current_temperature=19.0,
        valve_max_opening=100.0,
        advanced={
            "calibration": CalibrationType.DIRECT_VALVE_BASED,
            "calibration_mode": mode,
            "mpc_v2_plant_preset": MpcV2PlantPreset.AUTO,
        },
        valve_position_writable=True,
        valve_position_entity="number.trv_valve",
        max_temp=30.0,
        model_quirks=None,
    )


def _bt(real_trvs: dict[str, Trv], *, window_open: bool, door_open: bool) -> Any:
    """Build a BT-shaped stand-in with a cold room asking for heat."""
    return SimpleNamespace(
        real_trvs=real_trvs,
        bt_target_temp=21.0,
        cur_temp=19.0,
        cur_temp_filtered=None,
        tolerance=0.0,
        temp_slope=None,
        window_open=window_open,
        door_open=door_open,
        contact_open=bool(window_open) or bool(door_open),
        device_name="BT_TEST",
        bt_hvac_mode=HVACMode.HEAT,
        heating_power=0.04,
        heat_loss_rate=0.02,
        outdoor_sensor=None,
        weather_entity=None,
        hass=None,
        _unique_id="bt_contact_gate",
        device_id="bt_contact_gate_device",
        entry_id="bt_contact_gate_entry",
        state_mgr=_InMemoryStateManager(),
        schedule_save_state=lambda: None,
        kernel_state=SimpleNamespace(
            control_mode=SimpleNamespace(mode=ControlMode.OPTIMAL)
        ),
        clock=FakeClock(monotonic_value=1_000_000.0),
    )


def _commanded_valve_percent(output: Any, balance: Any) -> float | None:
    """Return the valve percentage the control cycle would send, if any.

    Mirrors ``_get_valve_control``: a balance dict carrying ``apply_valve``
    is what reaches ``set_valve``, so it takes precedence over the
    dispatcher's own return value.
    """
    if isinstance(balance, dict) and balance.get("apply_valve"):
        return balance.get("valve_percent")
    if output is None:
        return None
    for attribute in ("valve_percent", "duty_cycle_pct"):
        if hasattr(output, attribute):
            return getattr(output, attribute)
    return None


CONTROLLERS = [
    pytest.param(CalibrationMode.MPC_CALIBRATION, _compute_mpc_balance, id="mpc"),
    pytest.param(CalibrationMode.TPI_CALIBRATION, _compute_tpi_balance, id="tpi"),
    pytest.param(
        CalibrationMode.MPC_V2_CALIBRATION,
        _compute_mpc_v2_balance,
        id="mpc_v2",
        marks=pytest.mark.skipif(not _HAS_DAQP, reason="MPC v2 needs the daqp solver"),
    ),
]


@pytest.mark.parametrize(("mode", "dispatch"), CONTROLLERS)
@pytest.mark.parametrize("contact", ["window", "door"])
def test_open_contact_suppresses_the_valve_command(
    mode: CalibrationMode, dispatch: Any, contact: str
) -> None:
    """Neither contact may leave a calibration mode commanding heat."""
    trvs = {"climate.solo": _valve_trv("climate.solo", mode)}
    bt = _bt(trvs, window_open=contact == "window", door_open=contact == "door")

    output, _ = dispatch(bt, "climate.solo")
    commanded = _commanded_valve_percent(
        output, trvs["climate.solo"].calibration_balance
    )

    assert not commanded, (
        f"{mode} commanded {commanded}% valve opening while the {contact} was open"
    )


@pytest.mark.parametrize(("mode", "dispatch"), CONTROLLERS)
def test_closed_contacts_let_the_controller_command_heat(
    mode: CalibrationMode, dispatch: Any
) -> None:
    """Control case: the suppression above is the contact, not the scenario."""
    trvs = {"climate.solo": _valve_trv("climate.solo", mode)}
    bt = _bt(trvs, window_open=False, door_open=False)

    output, _ = dispatch(bt, "climate.solo")
    commanded = _commanded_valve_percent(
        output, trvs["climate.solo"].calibration_balance
    )

    assert commanded, (
        f"{mode} commanded no heat for a cold room with both contacts shut"
    )
