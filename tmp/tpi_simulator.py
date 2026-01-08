#!/usr/bin/env python3
"""Simple TPI simulator for testing the controller logic.

Simulates room temperature dynamics and runs TPI cycles to verify behavior.
"""

import logging
from pathlib import Path
import sys

from custom_components.better_thermostat.utils.tpi import (
    TpiInput,
    TpiParams,
    build_tpi_key,
    compute_tpi,
)

# Add the project root to sys.path
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


# Mock BT object for key building


class MockBT:
    """Mock BT object."""

    def __init__(self, target_temp):
        self.bt_target_temp = target_temp
        self.unique_id = "test_bt"


logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")


def simulate_room(
    temp: float,
    duty_pct: float,
    dt_min: float,
    room_gain: float = 0.5,
    ambient_loss: float = 0.1,
) -> float:
    """Simulate a simple room model where heating adds heat and ambient cools."""
    heating_effect = duty_pct / 100.0 * room_gain * dt_min  # K increase
    cooling_effect = ambient_loss * dt_min  # K decrease
    return temp + heating_effect - cooling_effect


def main():
    """Run TPI simulation for 10 cycles."""
    print("Starting TPI Simulator...")

    bt = MockBT(21.0)
    entity_id = "climate.test_trv"
    key = build_tpi_key(bt, entity_id)

    # Start at 19°C, target 21°C
    room_temp = 19.0
    outdoor_temp = 5.0

    # 5-minute cycles (300 seconds)
    cycle_duration_s = 300.0
    cycle_duration_min = cycle_duration_s / 60.0

    for cycle in range(1, 11):
        print(f"\n--- Cycle {cycle} ---")

        # Run TPI computation
        inp = TpiInput(
            key=key,
            current_temp_C=room_temp,
            target_temp_C=bt.bt_target_temp,
            outdoor_temp_C=outdoor_temp,
            window_open=False,
            heating_allowed=True,
            bt_name="TestBT",
            entity_id=entity_id,
        )

        output = compute_tpi(inp, TpiParams())
        if output is None:
            print("TPI computation failed")
            continue

        duty_pct = output.duty_cycle_pct

        print(f"Current temp: {room_temp:.1f}°C, Target: {bt.bt_target_temp:.1f}°C")
        print(f"TPI Output: Duty {duty_pct:.1f}%")

        if output.debug:
            print(f"Debug: {output.debug}")

        # Simulate room heating for this cycle
        room_temp = simulate_room(room_temp, duty_pct, cycle_duration_min)
        room_temp = round(room_temp, 3)  # Round to 3 decimal places

    print("\nSimulation complete.")


if __name__ == "__main__":
    main()
