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

    bt.state_mgr.record_thermal.assert_called_once_with(0.02, 0.01)
    bt.state_mgr.record_filters.assert_called_once_with(
        20.5, 0.0012, _NOW.timestamp() - 40.0
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

    bt.state_mgr.record_filters.assert_called_once_with(None, None, None)


def test_record_runtime_without_store_is_a_noop():
    """Without a StateManager the record step does nothing."""
    bt = ThermostatStandIn()
    bt.state_mgr = None
    BetterThermostat._record_runtime_to_state(bt)
