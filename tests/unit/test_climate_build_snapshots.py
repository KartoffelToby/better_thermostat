"""Branch coverage for BetterThermostat._build_trv_snapshots.

Resolves each TRV's hvac_action from the cached info dict, falling back to the
live hass state ("hvac_action" or legacy "action" attribute) and caching the
result.  Non-dict entries are skipped.
"""

from unittest.mock import MagicMock

from homeassistant.components.climate.const import HVACAction
from homeassistant.core import State
import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.trv import Trv
from tests.factories import ThermostatStandIn, trv_from_legacy_dict


@pytest.fixture
def bt():
    """Minimal BetterThermostat mock for snapshot building."""
    mock = ThermostatStandIn()
    mock.device_name = "Test BT"
    mock.real_trvs = dict[str, Trv]()
    mock.hass = MagicMock()
    mock.hass.states.get.return_value = None
    return mock


def _snaps(bt):
    return BetterThermostat._build_trv_snapshots(bt)


def test_non_trv_entry_skipped(bt):
    """A real_trvs entry that is not a Trv is ignored."""
    bt.real_trvs = {"climate.trv": "not-a-trv"}
    assert _snaps(bt) == []


def test_cached_action_used(bt):
    """A cached hvac_action is used directly (lowercased)."""
    bt.real_trvs = {
        "climate.trv": trv_from_legacy_dict("climate.trv", {"hvac_action": "HEATING"})
    }
    snaps = _snaps(bt)
    assert len(snaps) == 1
    assert snaps[0].hvac_action == "heating"


def test_fallback_to_hass_hvac_action_and_caches(bt):
    """Without a cached value, the live hvac_action is read and cached."""
    info = Trv(entity_id="climate.trv")
    bt.real_trvs = {"climate.trv": info}
    bt.hass.states.get.return_value = State(
        "climate.trv", "heat", attributes={"hvac_action": "idle"}
    )
    snaps = _snaps(bt)
    assert snaps[0].hvac_action == "idle"
    assert info.hvac_action == "idle"  # cached back


def test_fallback_to_legacy_action_attribute(bt):
    """The legacy 'action' attribute is used when 'hvac_action' is absent."""
    bt.real_trvs = {"climate.trv": trv_from_legacy_dict("climate.trv", {})}
    bt.hass.states.get.return_value = State(
        "climate.trv", "heat", attributes={"action": "heating"}
    )
    assert _snaps(bt)[0].hvac_action == "heating"


def test_no_state_yields_none_action(bt):
    """No cached value and no live state -> hvac_action None."""
    bt.real_trvs = {"climate.trv": trv_from_legacy_dict("climate.trv", {})}
    bt.hass.states.get.return_value = None
    assert _snaps(bt)[0].hvac_action is None


def test_heating_enum_normalized(bt):
    """A cached HVACAction.HEATING enum resolves to the 'heating' string."""
    bt.real_trvs = {
        "climate.trv": trv_from_legacy_dict(
            "climate.trv", {"hvac_action": HVACAction.HEATING}
        )
    }
    assert _snaps(bt)[0].hvac_action == "heating"


def test_snapshot_carries_valve_fields(bt):
    """Valve fields pass through to the snapshot."""
    bt.real_trvs = {
        "climate.trv": trv_from_legacy_dict(
            "climate.trv",
            {
                "hvac_action": "idle",
                "ignore_trv_states": True,
                "valve_position": 42,
                "last_valve_percent": 17,
            },
        )
    }
    snap = _snaps(bt)[0]
    assert snap.ignore_trv_states is True
    assert snap.valve_position == 42
    assert snap.last_valve_percent == 17


@pytest.mark.parametrize("gone_state", ["unavailable", "unknown"])
def test_a_trv_that_is_gone_leaves_no_snapshot(bt, gone_state):
    """A TRV whose state reads as the device being gone does not speak for the room.

    Its cached action and valve position stay on the record for when it
    returns, but the room's action is built only from TRVs that report.
    """
    gone = trv_from_legacy_dict(
        "climate.gone",
        {"hvac_action": "heating", "valve_position": 40, "last_valve_percent": 40},
    )
    present = trv_from_legacy_dict("climate.present", {"hvac_action": "idle"})
    bt.real_trvs = {"climate.gone": gone, "climate.present": present}
    states = {
        "climate.gone": State("climate.gone", gone_state),
        "climate.present": State("climate.present", "heat"),
    }
    bt.hass.states.get.side_effect = states.get

    snaps = _snaps(bt)

    assert [snap.entity_id for snap in snaps] == ["climate.present"]
    assert gone.hvac_action == "heating"
    assert gone.valve_position == 40
