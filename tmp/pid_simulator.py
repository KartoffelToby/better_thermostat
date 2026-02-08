"""PID controller simulator for testing the PID calibration logic.

Simulates room temperature dynamics and runs PID cycles to verify controller behavior.
"""

import logging
import os
import sys
from unittest.mock import patch

from custom_components.better_thermostat.utils.calibration.pid import (
    PIDParams,
    compute_pid,
    reset_pid_state,
)

# Add workspace root to path
sys.path.append(os.getcwd())


# Setup logging
logging.basicConfig(level=logging.INFO, format="%(message)s")


def simulate_pid():
    """Simulate PID."""
    print("--- Starting PID Simulation (Realistic Physics) ---")

    # Parameters (Defaults)
    params = PIDParams(
        kp=60.0,
        ki=0.01,
        kd=2000.0,
        min_hold_time_s=300.0,  # 5 min
        big_change_threshold_pct=33.0,
        auto_tune=False,  # Disable auto-tune to test raw params
        steady_state_band_K=0.1,
    )

    # Simulation State
    room_temp = 19.0
    target_temp = 21.0
    outdoor_temp = 5.0
    valve_position = 0.0

    # EMA State
    ema_temp = room_temp
    ema_alpha = 0.1

    # Time
    sim_time = 10000.0
    dt = 60  # 1 minute steps

    # Physics Constants
    # Max heating: ~3 deg/hour = 0.05 deg/min
    heating_power_deg_per_sec = 3.0 / 3600.0
    # Loss at 15K delta: ~1 deg/hour
    insulation_loss_per_sec_per_k = (1.0 / 3600.0) / 15.0

    key = "sim_room_real"
    reset_pid_state(key)

    print(
        f"{'Time(h)':>7} | {'T_Room':>6} | {'Target':>6} | {'Valve':>5} | {'P':>5} | "
        f"{'I':>5} | {'D':>5} | {'Kp':>5} | {'Ki':>5} | {'Kd':>5} | {'Note'}"
    )
    print("-" * 115)

    for step in range(721):  # 12 hours
        # Scenario: Open window at 2 hours (120 mins) for 15 mins
        window_open = 120 <= step < 135

        if step == 120:
            print("--- WINDOW OPEN (15 min airing) ---")
        if step == 135:
            print("--- WINDOW CLOSED ---")

        # 1. Update Physics
        if window_open:
            effective_valve = 0.0
            # High heat loss (open window)
            heat_loss = (
                (room_temp - outdoor_temp) * (insulation_loss_per_sec_per_k * dt) * 8.0
            )
        else:
            effective_valve = max(0, valve_position - 5)
            heat_loss = (room_temp - outdoor_temp) * (
                insulation_loss_per_sec_per_k * dt
            )

        heat_gain = (effective_valve / 100.0) * (heating_power_deg_per_sec * dt)
        prev_room_temp = room_temp
        room_temp += heat_gain - heat_loss

        # Update EMA
        ema_temp = ema_temp + ema_alpha * (room_temp - ema_temp)

        slope = (room_temp - prev_room_temp) / (dt / 60.0)  # K/min

        # 2. Run PID
        with patch(
            "custom_components.better_thermostat.utils.calibration.pid.monotonic"
        ) as mock_time:
            mock_time.return_value = sim_time

            percent, debug = compute_pid(
                params=params,
                inp_target_temp_C=target_temp,
                inp_current_temp_C=room_temp,
                inp_trv_temp_C=room_temp,
                inp_temp_slope_K_per_min=slope,
                key=key,
                inp_current_temp_ema_C=ema_temp,
            )

        # System Override: Window Open -> Valve 0
        if window_open:
            percent = 0.0

        valve_position = percent

        # Log every 30 mins or on events
        if step % 30 == 0 or step in (120, 135, 360):
            note = ""
            if debug.get("anti_windup_blocked"):
                note += "AW-Block "
            if debug.get("i_relief"):
                note += "I-Relief "

            hours = step / 60.0
            print(
                f"{hours:7.1f} | {room_temp:6.2f} | {target_temp:6.2f} | {valve_position:5.1f} | {debug.get('p', 0):5.1f} | "
                f"{debug.get('i', 0):5.1f} | {debug.get('d', 0):5.1f} | {debug.get('kp', 0):5.1f} | {debug.get('ki', 0):5.3f} | "
                f"{debug.get('kd', 0):5.1f} | {note}"
            )

        sim_time += dt

        # Scenario: Change Target at 6 hours (360 mins)
        # if step == 360:
        #    print(f"--- TARGET CHANGE (21 -> 22) ---")
        #    target_temp = 22.0


if __name__ == "__main__":
    simulate_pid()
