"""The entity pushes its runtime state into the StateManager before saves.

The store-based restore path (_hydrate_thermal_from_state) reads
state_mgr.filters as the persistence authority; without this push the
filters would stay empty forever and the restore would silently keep
falling back to legacy entity attributes.
"""

from unittest.mock import MagicMock

from custom_components.better_thermostat.climate import BetterThermostat


def test_record_runtime_pushes_thermal_and_filters():
    """Both thermal stats and filters land in the StateManager."""
    bt = MagicMock()
    bt.state_mgr = MagicMock()
    bt.heating_power = 0.02
    bt.heat_loss_rate = 0.01
    bt.external_temp_ema = 20.5
    bt.temp_slope = 0.0012

    BetterThermostat._record_runtime_to_state(bt)

    bt.state_mgr.record_thermal.assert_called_once_with(0.02, 0.01)
    bt.state_mgr.record_filters.assert_called_once_with(20.5, 0.0012)


def test_record_runtime_without_store_is_a_noop():
    """Without a StateManager the record step does nothing."""
    bt = MagicMock()
    bt.state_mgr = None
    BetterThermostat._record_runtime_to_state(bt)
