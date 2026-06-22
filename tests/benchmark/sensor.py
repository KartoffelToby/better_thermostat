"""Sensor model: thermal lag + sampling + EMA filter + optional noise/dropout.

Two distinct time-domain effects are modelled here:

1. **Thermal lag** — the sensor body has its own thermal mass, so its
   reading approaches the air temperature with a first-order continuous
   filter (time constant ``thermal_lag_s``). This is the dominant
   physical lag in residential temperature sensors (typ. 30 s – 3 min,
   see DESIGN.md §8 (sensor modelling)).

2. **Sampling** — sensors only emit values every ``sample_interval_s``
   (typical 1–5 min for Zigbee). Between samples the previous reading
   is returned. An optional EMA filter smooths the sample stream
   (matches BT's own periodic EMA in `events/temperature.py`).

Noise is deterministic (LCG seeded fixed) for reproducible benchmarks.
"""

from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass
class SensorParams:
    """Parameters describing the sampled-temperature sensor."""

    sample_interval_s: float = 60.0
    ema_alpha: float = 1.0  # 1.0 = no EMA filter; <1.0 = smoothed toward filtered
    noise_std_K: float = 0.0
    # Dropout window: the sensor returns None while
    # ``dropout_from_t_s <= t_s < dropout_until_t_s``. The defaults
    # (0.0 / -1.0) describe an empty window, i.e. no dropout.
    dropout_from_t_s: float = 0.0
    dropout_until_t_s: float = -1.0
    # Sensor's own thermal time constant. ``0`` disables the lag;
    # typical residential values are 60–180 s.
    thermal_lag_s: float = 0.0
    # Calibration faults: constant bias plus a linear drift against the
    # true room temperature. The reported value at time ``t`` is
    # ``T_true + bias_K + drift_K_per_h * t / 3600``.
    bias_K: float = 0.0
    drift_K_per_h: float = 0.0
    # Sample-jitter: when ``> 0``, the effective time between samples is
    # drawn from a deterministic LCG with mean ``sample_interval_s`` and
    # standard deviation ``jitter_std_s`` (clipped to ≥ 5 s). Models the
    # async sensor-report behaviour of Zigbee TRVs that report on change.
    jitter_std_s: float = 0.0


class Sensor:
    """Sampled-output sensor with optional thermal lag and EMA filtering.

    The lag is applied continuously (every call), the sampling and EMA
    operate on the lagged value.
    """

    def __init__(self, params: SensorParams, seed: int = 12345) -> None:
        self.params = params
        self._filtered: float | None = None
        self._last_sample_t: float = -1.0
        # Noise/jitter RNG seed. Defaults to a fixed value for standalone
        # use; the runner derives a per-scenario seed so noise realisations
        # are decorrelated across scenarios rather than identical.
        self._rng_state: int = seed & 0x7FFFFFFF
        # Thermal-lag state: continuously updated regardless of sampling.
        self._lag_state: float | None = None
        self._last_lag_update_t: float = -1.0
        # Next sample's effective interval (jitter mode).
        self._next_sample_interval_s: float = params.sample_interval_s

    def _noise(self) -> float:
        if self.params.noise_std_K <= 0.0:
            return 0.0
        self._rng_state = (self._rng_state * 1103515245 + 12345) & 0x7FFFFFFF
        u = (self._rng_state / 0x7FFFFFFF) * 2.0 - 1.0  # [-1, 1]
        return u * u * u * self.params.noise_std_K

    def _apply_thermal_lag(self, t_s: float, T_true_C: float) -> float:
        """Return the lagged sensor temperature for the current step.

        Advances the sensor's internal thermal state.
        """
        if self.params.thermal_lag_s <= 0.0:
            return T_true_C

        if self._lag_state is None or self._last_lag_update_t < 0.0:
            self._lag_state = T_true_C
            self._last_lag_update_t = t_s
            return T_true_C

        dt = t_s - self._last_lag_update_t
        if dt > 0.0:
            alpha = 1.0 - math.exp(-dt / self.params.thermal_lag_s)
            self._lag_state += alpha * (T_true_C - self._lag_state)
            self._last_lag_update_t = t_s
        return self._lag_state

    def read(self, t_s: float, T_true_C: float) -> float | None:
        """Return the currently observed temperature, or None on dropout."""
        # The sensor body keeps tracking the room even while reporting is
        # down, so the lag state must advance through the outage.
        T_lagged = self._apply_thermal_lag(t_s, T_true_C)
        if self.params.dropout_from_t_s <= t_s < self.params.dropout_until_t_s:
            return None

        T_lagged += self.params.bias_K + self.params.drift_K_per_h * (t_s / 3600.0)

        should_sample = (
            self._last_sample_t < 0.0
            or (t_s - self._last_sample_t) >= self._next_sample_interval_s
        )
        if should_sample:
            raw = T_lagged + self._noise()
            if self.params.jitter_std_s > 0.0:
                # Roll a new effective interval using a Gaussian-ish kick
                # from the cubic-noise generator (same RNG as _noise).
                self._rng_state = (self._rng_state * 1103515245 + 12345) & 0x7FFFFFFF
                u = (self._rng_state / 0x7FFFFFFF) * 2.0 - 1.0
                kick = u * u * u * self.params.jitter_std_s
                self._next_sample_interval_s = max(
                    5.0, self.params.sample_interval_s + kick
                )
            if self._filtered is None:
                self._filtered = raw
            else:
                a = self.params.ema_alpha
                self._filtered = a * raw + (1.0 - a) * self._filtered
            self._last_sample_t = t_s
        return self._filtered
