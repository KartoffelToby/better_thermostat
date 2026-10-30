"""The entity pushes its runtime state into the StateManager before saves.

The store-based restore path (_hydrate_thermal_from_state) reads
state_mgr.filters as the persistence authority; without this push the
filters would stay empty forever and the restore would silently keep
falling back to legacy entity attributes.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.utils.state_manager import (
    FilterState,
    ThermalStats,
)
from tests.factories import ThermostatStandIn

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def test_record_runtime_pushes_thermal_and_filters():
    """Both thermal stats and filters land in the StateManager."""
    bt = ThermostatStandIn()
    bt.state_mgr = MagicMock()
    bt.heating_power = 0.02
    bt.heat_loss_rate = 0.01
    bt.room_temperature_ema = 20.5
    bt.temperature_slope = 0.0012
    bt.clock = FakeClock(now_value=_NOW, monotonic_value=1000.0)
    bt._room_temperature_ema_monotonic = 960.0

    BetterThermostat._record_runtime_to_state(bt)

    bt.state_mgr.record_thermal.assert_called_once_with(
        ThermalStats(heating_power=0.02, heat_loss_rate=0.01)
    )
    bt.state_mgr.record_filters.assert_called_once_with(
        FilterState(
            room_temperature_ema=20.5,
            temperature_slope=0.0012,
            room_temperature_ema_recorded_at=_NOW.timestamp() - 40.0,
        )
    )


def test_a_filter_never_updated_is_saved_without_a_time():
    """An EMA with no update behind it carries no wall-clock time."""
    bt = ThermostatStandIn()
    bt.state_mgr = MagicMock()
    bt.heating_power = 0.02
    bt.heat_loss_rate = 0.01
    bt.room_temperature_ema = None
    bt.temperature_slope = None
    bt.clock = FakeClock(now_value=_NOW, monotonic_value=1000.0)
    bt._room_temperature_ema_monotonic = None

    BetterThermostat._record_runtime_to_state(bt)

    bt.state_mgr.record_filters.assert_called_once_with(FilterState())


def test_no_save_is_scheduled_before_the_stored_state_is_loaded():
    """A save before the load would write the empty state over the store."""
    bt = ThermostatStandIn()
    bt.state_mgr = MagicMock()
    bt.state_loaded = False

    BetterThermostat.schedule_save_state(bt)

    bt.state_mgr.schedule_delay_save.assert_not_called()


def test_a_save_is_scheduled_once_the_stored_state_is_loaded():
    """Once loaded, a save records the runtime values when it writes."""
    bt = ThermostatStandIn()
    bt.state_mgr = MagicMock()
    bt.state_loaded = True

    BetterThermostat.schedule_save_state(bt, delay_seconds=3.0)

    bt.state_mgr.schedule_delay_save.assert_called_once_with(
        pre_save=bt._record_runtime_to_state, delay_seconds=3.0
    )
