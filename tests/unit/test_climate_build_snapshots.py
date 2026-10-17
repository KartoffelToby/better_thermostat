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
from tests.factories import ThermostatStandIn


@pytest.fixture
def bt():
    """Minimal BetterThermostat mock for snapshot building."""
    mock = ThermostatStandIn()
    mock.device_name = "Test BT"
    mock.real_trvs = dict[str, Trv]()
    mock.hass = MagicMock()
    mock.hass.states.get.return_value = State("climate.trv", "heat")
    return mock


def _snaps(bt):
    return BetterThermostat._build_trv_snapshots(bt)


def test_non_trv_entry_skipped(bt):
    """A real_trvs entry that is not a Trv is ignored."""
    bt.real_trvs = {"climate.trv": "not-a-trv"}
    assert _snaps(bt) == []


def test_cached_action_used(bt):
    """A cached hvac_action is used directly, without reading the live state."""
    bt.real_trvs = {
        "climate.trv": Trv(entity_id="climate.trv", hvac_action=HVACAction.HEATING)
    }
    bt.hass.states.get.return_value = State(
        "climate.trv", "heat", attributes={"hvac_action": "idle"}
    )
    snaps = _snaps(bt)
    assert len(snaps) == 1
    assert snaps[0].hvac_action is HVACAction.HEATING


def test_fallback_to_hass_hvac_action_and_caches(bt):
    """Without a cached value, the live hvac_action is read and cached."""
    info = Trv(entity_id="climate.trv")
    bt.real_trvs = {"climate.trv": info}
    bt.hass.states.get.return_value = State(
        "climate.trv", "heat", attributes={"hvac_action": "idle"}
    )
    snaps = _snaps(bt)
    assert snaps[0].hvac_action is HVACAction.IDLE
    assert info.hvac_action is HVACAction.IDLE  # cached back


def test_fallback_to_legacy_action_attribute(bt):
    """The legacy 'action' attribute is used when 'hvac_action' is absent."""
    bt.real_trvs = {"climate.trv": Trv(entity_id="climate.trv")}
    bt.hass.states.get.return_value = State(
        "climate.trv", "heat", attributes={"action": "heating"}
    )
    assert _snaps(bt)[0].hvac_action is HVACAction.HEATING


@pytest.mark.parametrize("reported", ["HEATING", " heating "])
def test_live_action_is_matched_regardless_of_case_and_whitespace(bt, reported):
    """A live action in another spelling still names the HVAC action."""
    bt.real_trvs = {"climate.trv": Trv(entity_id="climate.trv")}
    bt.hass.states.get.return_value = State(
        "climate.trv", "heat", attributes={"hvac_action": reported}
    )
    assert _snaps(bt)[0].hvac_action is HVACAction.HEATING


def test_live_value_that_names_no_action_yields_none(bt):
    """A live action attribute carrying no HVAC action leaves the action unknown."""
    info = Trv(entity_id="climate.trv")
    bt.real_trvs = {"climate.trv": info}
    bt.hass.states.get.return_value = State(
        "climate.trv", "heat", attributes={"action": "lock"}
    )
    assert _snaps(bt)[0].hvac_action is None
    assert info.hvac_action is None


def test_no_reported_action_yields_none_action(bt):
    """No cached value and a live state without an action -> hvac_action None."""
    bt.real_trvs = {"climate.trv": Trv(entity_id="climate.trv")}
    assert _snaps(bt)[0].hvac_action is None


def test_snapshot_carries_valve_fields(bt):
    """Valve fields pass through to the snapshot."""
    bt.real_trvs = {
        "climate.trv": Trv(
            entity_id="climate.trv",
            hvac_action=HVACAction.IDLE,
            ignore_trv_states=True,
            valve_position=42,
            last_valve_percent=17,
        )
    }
    snap = _snaps(bt)[0]
    assert snap.ignore_trv_states is True
    assert snap.valve_position == 42
    assert snap.last_valve_percent == 17


@pytest.mark.parametrize("gone_state", ["unavailable", "unknown", None])
def test_a_trv_that_is_gone_leaves_no_snapshot(bt, gone_state):
    """A TRV whose state is missing or reads as gone does not speak for the room.

    Its cached action and valve position stay on the record for when it
    returns, but the room's action is built only from TRVs that report.
    """
    gone = Trv(
        entity_id="climate.gone",
        hvac_action=HVACAction.HEATING,
        valve_position=40,
        last_valve_percent=40,
    )
    present = Trv(entity_id="climate.present", hvac_action=HVACAction.IDLE)
    bt.real_trvs = {"climate.gone": gone, "climate.present": present}
    states = {"climate.present": State("climate.present", "heat")}
    if gone_state is not None:
        states["climate.gone"] = State("climate.gone", gone_state)
    bt.hass.states.get.side_effect = states.get

    snaps = _snaps(bt)

    assert [snap.entity_id for snap in snaps] == ["climate.present"]
    assert gone.hvac_action is HVACAction.HEATING
    assert gone.valve_position == 40
