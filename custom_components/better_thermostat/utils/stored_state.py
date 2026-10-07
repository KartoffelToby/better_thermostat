"""On-disk shape of the unified runtime state store.

Each section of the store is described by a TypedDict whose keys are the
names the store file carries. Several of them predate the field names of
the in-memory dataclasses (``last_target_C``, ``rmse_prior_K``,
``external_temp_ema``, ``temp_slope``) and must stay as they are, because
every release that reads the file, including the 1.9 line after a
downgrade, looks them up by these strings. The functional syntax keeps
them as strings rather than identifiers, so the naming checks never see a
legacy store name as a new attribute.

The writers in :mod:`.state_manager` build these mappings from the
dataclasses; the readers there accept any ``Mapping[str, object]``.
"""

from __future__ import annotations

from typing import TypedDict

StoredMpcState = TypedDict(  # noqa: UP013
    "StoredMpcState",
    {
        "last_percent": float | None,
        "last_update_ts": float,
        "ema_slope": float | None,
        "gain_est": float | None,
        "loss_est": float | None,
        "ka_est": float | None,
        "solar_gain_est": float | None,
        "last_temp": float | None,
        "last_time": float,
        "last_trv_temp": float | None,
        "last_trv_temp_ts": float,
        "last_window_open_ts": float,
        "dead_zone_hits": int,
        "min_effective_percent": float | None,
        "last_learn_time": float | None,
        "last_learn_temp": float | None,
        "last_residual_time": float | None,
        "virtual_temp": float | None,
        "virtual_temp_ts": float,
        "last_room_temp_ts": float,
        "perf_curve": dict[str, dict[str, float | int]],
        "trv_profile": str,
        "profile_confidence": float,
        "profile_samples": int,
        "u_integral": float,
        "time_integral": float,
        "last_integration_ts": float,
        "created_ts": float,
        "loss_learn_count": int,
        "gain_learn_count": int,
        "is_calibration_active": bool,
        "recent_errors": list[float],
        "regime_boost_active": bool,
        "consecutive_insufficient_heat": int,
        "kalman_P": float,
        "tolerance_hold_active": bool,
        "last_target_C": float | None,
        "last_sensor_temp_C": float | None,
        "last_room_temp_C": float | None,
    },
)

StoredMpcV2State = TypedDict(  # noqa: UP013
    "StoredMpcV2State",
    {
        "last_percent": float | None,
        "last_compute_ts": float,
        "created_ts": float,
        "outdoor_fallback_logged": bool,
        # The controller snapshot is stored as it is held: a snapshot of a
        # version this release cannot read is written back unchanged.
        "snapshot": dict[str, object],
    },
)

StoredMpcV2Reid = TypedDict(  # noqa: UP013
    "StoredMpcV2Reid",
    {
        "tau_room_min": float,
        "gain_heater": float,
        "fitted_ts": float,
        "n_segments": int,
        "rmse_prior_K": float,
        "rmse_fit_K": float,
    },
)

StoredPidState = TypedDict(  # noqa: UP013
    "StoredPidState",
    {
        "pid_integral": float,
        "pid_last_meas": float | None,
        "pid_last_error": float | None,
        "pid_last_time": float,
        "pid_kp": float | None,
        "pid_ki": float | None,
        "pid_kd": float | None,
        "auto_tune": bool | None,
        "last_tune_ts": float,
        "last_delta_sign": int | None,
        "last_error_sign": int | None,
        "previous_abs_error": float | None,
        "last_abs_error": float | None,
        "ema_slope": float | None,
        "last_percent": float,
        "last_output_change_ts": float,
        "last_target_temp": float | None,
    },
)

StoredTpiState = TypedDict(  # noqa: UP013
    "StoredTpiState", {"last_percent": float | None, "last_update_ts": float}
)

StoredThermalStats = TypedDict(  # noqa: UP013
    "StoredThermalStats",
    {"heating_power": float | None, "heat_loss_rate": float | None},
)

StoredFilterState = TypedDict(  # noqa: UP013
    "StoredFilterState", {"external_temp_ema": float | None, "temp_slope": float | None}
)

StoredRuntimeState = TypedDict(  # noqa: UP013
    "StoredRuntimeState",
    {
        "version": int,
        "mpc": dict[str, StoredMpcState],
        "mpc_v2": dict[str, StoredMpcV2State],
        "mpc_v2_reid": dict[str, StoredMpcV2Reid],
        "pid": dict[str, StoredPidState],
        "tpi": dict[str, StoredTpiState],
        "thermal": StoredThermalStats,
        "filters": StoredFilterState,
    },
)
