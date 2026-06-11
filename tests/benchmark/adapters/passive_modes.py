"""BT's offset-family calibration modes — passive (no smart controller).

These three modes do not run an inner controller (no MPC, PID, TPI or
heating-power learner). They configure the TRV's own sensor-offset and
then let the TRV's internal P-loop close the loop.

In production:

* **DEFAULT** — BT pushes ``bt_target`` to the TRV unchanged and sends a
  calibration offset of ``(external - trv_internal)``. The TRV's
  internal regulator then effectively tracks the external sensor:
  ``valve = p_gain · (bt_target - external)``.
* **AGGRESIVE_CALIBRATION** (UI: *Fix Calibration*) — same as DEFAULT
  plus a −2.5 K bias on the offset while heating, so the TRV thinks
  it's 2.5 K colder than it is and opens the valve more aggressively.
* **NO_CALIBRATION** — BT pushes only ``bt_target``, no offset. The
  TRV's regulator tracks **its own internal sensor**, not the external
  one: ``valve = p_gain · (bt_target - trv_internal_temp)``. Performs
  badly whenever the TRV's body temperature diverges from the room
  (radiator backsplash, sensor offsets, multi-TRV rooms).

The benchmark models these directly as P-controllers without an
``IndirectTrvAdapter`` wrapper because the *adapter itself* already
encodes the TRV-side P-loop that these modes rely on. Wrapping would
double-apply the regulator.

The default ``p_gain = 30 %/K`` matches the Tado/Sonoff family preset
in :mod:`adapters.indirect_trv`. Override via the ``p_gain`` constructor
argument to model softer (Tuya, Bosch) hardware.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .base import BenchmarkContext, BenchmarkOutput, ControllerFamily

# Production constants from ``utils/calibration.py``.
_AGGRESSIVE_BOOST_K: float = 2.5

# Representative TRV-internal P-gain (mirrors ``TADO_PARAMS.internal_p_gain``).
_DEFAULT_TRV_P_GAIN: float = 30.0


@dataclass
class PassiveModeParams:
    """Parameters shared across the offset-family adapters."""

    p_gain: float = _DEFAULT_TRV_P_GAIN
    clamp_min_pct: float = 0.0
    clamp_max_pct: float = 100.0


def _proportional(error_K: float, params: PassiveModeParams) -> float:
    """Clamp ``p_gain · error_K`` to the saturation band."""
    return max(params.clamp_min_pct, min(params.clamp_max_pct, params.p_gain * error_K))


class DefaultCalibrationAdapter:
    """``CalibrationMode.DEFAULT`` — TRV tracks external sensor via offset calibration."""

    name: str = "default"
    family: ControllerFamily = "valve"

    def __init__(self, params: PassiveModeParams | None = None) -> None:
        self._params = params if params is not None else PassiveModeParams()

    def reset(self, prior: dict[str, Any] | None = None) -> None:
        """Stateless — nothing to reset."""
        _ = prior

    def step(self, ctx: BenchmarkContext) -> BenchmarkOutput:
        """Output ``p_gain · (target - external)`` clamped to [0, 100] %."""
        error_K = ctx.target_temp_C - ctx.current_temp_C
        valve = _proportional(error_K, self._params)
        return BenchmarkOutput(
            valve_percent=valve,
            diagnostics={"error_K": round(error_K, 3), "p_gain": self._params.p_gain},
        )

    def export_state(self) -> dict[str, Any]:
        """Return an empty state (stateless)."""
        return {}


class AggressiveCalibrationAdapter:
    """``CalibrationMode.AGGRESIVE_CALIBRATION`` — DEFAULT + −2.5 K boost while heating.

    The boost is latched: it kicks in once the previous step commanded
    a non-zero valve and stays active until the room reaches the target.
    """

    name: str = "aggressive"
    family: ControllerFamily = "valve"

    def __init__(self, params: PassiveModeParams | None = None) -> None:
        self._params = params if params is not None else PassiveModeParams()
        self._was_heating: bool = False

    def reset(self, prior: dict[str, Any] | None = None) -> None:
        """Forget whether we were heating last step."""
        _ = prior
        self._was_heating = False

    def step(self, ctx: BenchmarkContext) -> BenchmarkOutput:
        """Add ``p_gain · 2.5`` to the proportional output while heating."""
        error_K = ctx.target_temp_C - ctx.current_temp_C
        # The boost is active whenever BT considers itself "heating" — we
        # use ``error_K > 0`` (room below target) as the proxy for the
        # production ``HVACAction.HEATING`` trigger.
        boost = self._params.p_gain * _AGGRESSIVE_BOOST_K if error_K > 0.0 else 0.0
        raw_pct = self._params.p_gain * error_K + boost
        valve = max(
            self._params.clamp_min_pct, min(self._params.clamp_max_pct, raw_pct)
        )
        self._was_heating = valve > 0.0
        return BenchmarkOutput(
            valve_percent=valve,
            diagnostics={"error_K": round(error_K, 3), "boost_pct": round(boost, 2)},
        )

    def export_state(self) -> dict[str, Any]:
        """Return whether the boost is currently latched."""
        return {"was_heating": self._was_heating}


class NoCalibrationAdapter:
    """``CalibrationMode.NO_CALIBRATION`` — TRV tracks its own internal sensor.

    BT pushes ``bt_target`` straight through with no offset. The TRV's
    P-loop closes against ``trv_temp_C`` (the radiator-mounted sensor),
    not against the room sensor — so the controller's error reference is
    whatever the TRV body is reading, not what the room actually is.
    """

    name: str = "no_calibration"
    family: ControllerFamily = "valve"

    def __init__(self, params: PassiveModeParams | None = None) -> None:
        self._params = params if params is not None else PassiveModeParams()

    def reset(self, prior: dict[str, Any] | None = None) -> None:
        """Stateless — nothing to reset."""
        _ = prior

    def step(self, ctx: BenchmarkContext) -> BenchmarkOutput:
        """Output ``p_gain · (target - trv_internal)``, clamped."""
        # Fall back to the external sensor only when the scenario lacks
        # an explicit TRV temperature (sensorless plant variants).
        trv_T = ctx.trv_temp_C if ctx.trv_temp_C is not None else ctx.current_temp_C
        error_K = ctx.target_temp_C - trv_T
        valve = _proportional(error_K, self._params)
        return BenchmarkOutput(
            valve_percent=valve,
            diagnostics={
                "error_K": round(error_K, 3),
                "trv_temp_used_C": round(trv_T, 3),
            },
        )

    def export_state(self) -> dict[str, Any]:
        """Return an empty state (stateless)."""
        return {}
