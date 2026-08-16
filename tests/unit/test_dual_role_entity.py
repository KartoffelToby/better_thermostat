"""Tests for the dual-role recognition and ownership helpers.

A configuration may name one climate entity as both a controlled thermostat
and the cooler. These helpers are what tells that topology apart from every
other one and answers which channel owns the shared device.
"""

from unittest.mock import MagicMock

from homeassistant.components.climate.const import HVACMode
from homeassistant.core import State

from custom_components.better_thermostat.utils.helpers import (
    cooling_owns_dual_role_device,
    cooling_owns_dual_role_report,
    dual_role_entity_id,
)

SHARED_ID = "climate.reversible_ac"


def _make_bt(*, cooler_entity_id, real_trvs, hvac_mode_decided=None):
    """Build a minimal stand-in carrying only what the helpers read."""
    bt = MagicMock()
    bt.cooler_entity_id = cooler_entity_id
    bt.real_trvs = real_trvs
    bt._cooler_last_sent = {"hvac_mode_decided": hvac_mode_decided}
    bt.hass = MagicMock()
    bt.hass.states.get.return_value = None
    return bt


class TestDualRoleEntityId:
    """Recognition of the shared entity."""

    def test_dual_role_entity_id_names_the_shared_entity(self):
        """A cooler that is also a controlled thermostat is reported by id."""
        bt = _make_bt(cooler_entity_id=SHARED_ID, real_trvs={SHARED_ID: MagicMock()})
        assert dual_role_entity_id(bt) == SHARED_ID

    def test_dual_role_entity_id_is_none_for_a_distinct_cooler_and_for_no_cooler(self):
        """Every other topology is left without a dual-role entity."""
        distinct = _make_bt(
            cooler_entity_id="climate.split_unit",
            real_trvs={"climate.radiator": MagicMock()},
        )
        assert dual_role_entity_id(distinct) is None

        no_cooler = _make_bt(
            cooler_entity_id=None, real_trvs={"climate.radiator": MagicMock()}
        )
        assert dual_role_entity_id(no_cooler) is None


class TestCoolingOwnsDualRoleDevice:
    """The dispatch question the control cycle asks."""

    def test_cooling_owns_dual_role_device_follows_the_latched_decision(self):
        """The decision control_cooler latched this cycle decides ownership."""
        bt = _make_bt(
            cooler_entity_id=SHARED_ID,
            real_trvs={SHARED_ID: MagicMock()},
            hvac_mode_decided=HVACMode.COOL,
        )
        assert cooling_owns_dual_role_device(bt, SHARED_ID) is True

        bt._cooler_last_sent["hvac_mode_decided"] = HVACMode.OFF
        assert cooling_owns_dual_role_device(bt, SHARED_ID) is False

    def test_cooling_owns_dual_role_device_falls_back_to_the_reported_mode_while_unlatched(
        self,
    ):
        """Before the first decision the device's own mode answers instead."""
        bt = _make_bt(cooler_entity_id=SHARED_ID, real_trvs={SHARED_ID: MagicMock()})
        bt.hass.states.get.return_value = State(SHARED_ID, HVACMode.COOL)
        assert cooling_owns_dual_role_device(bt, SHARED_ID) is True

        bt.hass.states.get.return_value = State(SHARED_ID, HVACMode.HEAT)
        assert cooling_owns_dual_role_device(bt, SHARED_ID) is False

        bt.hass.states.get.return_value = None
        assert cooling_owns_dual_role_device(bt, SHARED_ID) is False

    def test_cooling_owns_dual_role_device_reads_a_cache_no_cycle_has_written(self):
        """An instance whose cooler never ran yet carries no cache at all."""
        bt = _make_bt(cooler_entity_id=SHARED_ID, real_trvs={SHARED_ID: MagicMock()})
        bt._cooler_last_sent = None
        bt.hass.states.get.return_value = State(SHARED_ID, HVACMode.COOL)

        assert cooling_owns_dual_role_device(bt, SHARED_ID) is True

    def test_cooling_owns_dual_role_device_is_false_for_a_distinct_cooler(self):
        """An installation without the overlap takes no new branch."""
        bt = _make_bt(
            cooler_entity_id="climate.split_unit",
            real_trvs={"climate.radiator": MagicMock()},
            hvac_mode_decided=HVACMode.COOL,
        )
        assert cooling_owns_dual_role_device(bt, "climate.split_unit") is False
        assert cooling_owns_dual_role_device(bt, "climate.radiator") is False


class TestCoolingOwnsDualRoleReport:
    """The routing question the inbound handler asks."""

    def test_cooling_owns_dual_role_report_takes_a_reported_cool_mode_or_the_latch(
        self,
    ):
        """Either the device reporting cool or the latch files a report as cooling."""
        bt = _make_bt(cooler_entity_id=SHARED_ID, real_trvs={SHARED_ID: MagicMock()})
        assert cooling_owns_dual_role_report(bt, SHARED_ID, HVACMode.COOL) is True

        assert cooling_owns_dual_role_report(bt, SHARED_ID, HVACMode.HEAT) is False

        bt._cooler_last_sent["hvac_mode_decided"] = HVACMode.COOL
        assert cooling_owns_dual_role_report(bt, SHARED_ID, HVACMode.HEAT) is True

    def test_cooling_owns_dual_role_report_is_false_for_a_distinct_cooler(self):
        """A report from a device carrying one role is never rerouted."""
        bt = _make_bt(
            cooler_entity_id="climate.split_unit",
            real_trvs={"climate.radiator": MagicMock()},
            hvac_mode_decided=HVACMode.COOL,
        )
        assert cooling_owns_dual_role_report(bt, "climate.radiator", HVACMode.COOL) is (
            False
        )
