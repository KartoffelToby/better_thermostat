"""MPC (Model Predictive Control) simulator for testing the MPC calibration logic.

Simulates room temperature dynamics and runs MPC cycles to verify controller behavior.
"""

from dataclasses import dataclass
import logging
import os
import sys
from unittest.mock import patch

from custom_components.better_thermostat.utils.calibration.mpc import (
    _MPC_STATES,
    MpcInput,
    MpcParams,
    compute_mpc,
)

# Add workspace root to sys.path
sys.path.append(os.getcwd())


# Setup logging
logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("mpc_sim")


@dataclass
class SimCase:
    """Simulation case."""

    name: str
    gain: float
    loss: float


cases = [
    SimCase("Case 1", 0.07, 0.0185),
    SimCase("Case 2", 0.0348, 0.0119),
    SimCase("Case 3", 0.0852, 0.007),
    SimCase("Case 4", 0.05, 0.03),
]


def run_simulation():
    """Run simulation."""
    target_temp = 20.0

    start_temp = 19.8
    end_temp = 20.2
    step_size = 0.01

    print(
        f"{'Temp':<15} | {'Case 1 (%)':<12} | {'Case 2 (%)':<12} | {'Case 3 (%)':<12} | {'Case 4 (%)':<12}"
    )
    print("-" * 76)

    results = {}  # case_name -> list of valves

    temps = []
    # Up
    curr = start_temp
    while curr <= end_temp + 0.0001:
        temps.append(round(curr, 3))
        curr += step_size
    # Down
    curr = end_temp - step_size
    while curr >= start_temp - 0.0001:
        temps.append(round(curr, 3))
        curr -= step_size

    # Mock time
    current_time = 1000.0

    with patch(
        "custom_components.better_thermostat.utils.calibration.mpc.monotonic"
    ) as mock_time:
        for case in cases:
            _MPC_STATES.clear()

            # Reset time for each case
            current_time = 1000.0
            mock_time.return_value = current_time

            params = MpcParams(
                mpc_thermal_gain=case.gain,
                mpc_loss_coeff=case.loss,
                mpc_adapt=False,
                min_percent_hold_time_s=0,
                big_change_force_open_pct=101,
                min_update_interval_s=0,  # Allow updates every step
            )

            case_vals = []

            for i, temp in enumerate(temps):
                mock_time.return_value = current_time

                # Calculate slope
                if i == 0:
                    slope = 0.002
                else:
                    slope = (temp - temps[i - 1]) / 5.0

                inp = MpcInput(
                    key=f"sim_{case.name}",
                    target_temp_C=target_temp,
                    current_temp_C=temp,
                    trv_temp_C=temp,
                    temp_slope_K_per_min=slope,
                    heating_allowed=True,
                )

                output = compute_mpc(inp, params)
                valve = output.valve_percent if output else 0
                case_vals.append(valve)

                # Advance time by 5 minutes (300s)
                current_time += 300.0

            results[case.name] = case_vals

    # Print table
    target_points = [19.8, 19.9, 20.0, 20.1, 20.2]

    # Helper to detect direction change
    peak_idx = temps.index(20.2)

    for i, temp in enumerate(temps):
        if any(abs(temp - t) < 0.001 for t in target_points):
            c1 = results["Case 1"][i]
            c2 = results["Case 2"][i]
            c3 = results["Case 3"][i]
            c4 = results["Case 4"][i]

            direction = " (Up)" if i <= peak_idx else " (Down)"
            temp_str = f"{temp:.1f}{direction}"

            print(f"{temp_str:<15} | {c1:<12} | {c2:<12} | {c3:<12} | {c4:<12}")


if __name__ == "__main__":
    run_simulation()
