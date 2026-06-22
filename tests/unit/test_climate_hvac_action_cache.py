"""The hvac_action property computes at most once between commits.

Every control cycle commits a fresh action; the property only has to
bridge the gap until the first commit. Recomputing on every state read
rebuilds the TRV snapshot list each time for a result that is thrown
away.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

from homeassistant.components.climate.const import HVACAction

from custom_components.better_thermostat.climate import BetterThermostat


def _bt():
    bt = MagicMock()
    bt.attr_hvac_action = None
    bt._compute_hvac_action_pure = Mock(
        return_value=SimpleNamespace(action=HVACAction.IDLE)
    )
    return bt


def test_first_read_computes_once_and_caches():
    """Repeated reads before the first commit compute only once."""
    bt = _bt()
    first = BetterThermostat.hvac_action.fget(bt)
    second = BetterThermostat.hvac_action.fget(bt)

    assert first is HVACAction.IDLE
    assert second is HVACAction.IDLE
    bt._compute_hvac_action_pure.assert_called_once()


def test_committed_action_wins_over_the_cache():
    """A committed action replaces whatever the property cached."""
    bt = _bt()
    BetterThermostat.hvac_action.fget(bt)
    bt.attr_hvac_action = HVACAction.HEATING

    assert BetterThermostat.hvac_action.fget(bt) is HVACAction.HEATING
    bt._compute_hvac_action_pure.assert_called_once()
