"""Tests for the Trv domain object and its transitional dict bridge."""

import pytest

from custom_components.better_thermostat.trv import Trv


def _make() -> Trv:
    return Trv(entity_id="climate.trv", integration="mqtt", model="TRVZB")


class TestTypedAccess:
    """Typed attribute access is the primary interface."""

    def test_construction_defaults(self):
        """A fresh Trv carries the documented defaults."""
        trv = _make()
        assert trv.entity_id == "climate.trv"
        assert trv.valve_max_opening == 100.0
        assert trv.local_calibration_min == -7
        assert trv.calibration_received is True
        assert trv.ignore_trv_states is False
        assert trv.current_temperature is None
        assert trv.advanced == {}
        assert trv.extra == {}

    def test_attribute_writes(self):
        """Fields are plain mutable attributes."""
        trv = _make()
        trv.current_temperature = 21.5
        trv.ignore_trv_states = True
        assert trv.current_temperature == 21.5
        assert trv.ignore_trv_states is True


class TestDictBridge:
    """The transitional dict protocol mirrors attribute access."""

    def test_getitem_reads_fields(self):
        """trv["key"] reads the typed field."""
        trv = _make()
        trv.current_temperature = 20.0
        assert trv["current_temperature"] == 20.0
        assert trv["valve_max_opening"] == 100.0

    def test_setitem_writes_fields(self):
        """trv["key"] = value writes the typed field."""
        trv = _make()
        trv["last_hvac_mode"] = "heat"
        assert trv.last_hvac_mode == "heat"

    def test_get_with_default(self):
        """get() returns the stored field value, not the default."""
        trv = _make()
        assert trv.get("min_temp") is None
        assert trv.get("valve_max_opening", 55.0) == 100.0

    def test_contains(self):
        """Membership covers fields and extras."""
        trv = _make()
        assert "current_temperature" in trv
        assert "nonexistent" not in trv
        trv["_quirk_seq"] = 3
        assert "_quirk_seq" in trv

    def test_unknown_keys_land_in_extra(self):
        """Quirk scratchpad keys are bridged through ``extra``."""
        trv = _make()
        trv["_trvzb_valve_bump_seq"] = 7
        assert trv.extra == {"_trvzb_valve_bump_seq": 7}
        assert trv["_trvzb_valve_bump_seq"] == 7
        assert trv.get("_trvzb_valve_bump_seq") == 7

    def test_missing_extra_key_raises(self):
        """Reading an unknown key without default raises KeyError like a dict."""
        trv = _make()
        with pytest.raises(KeyError):
            trv["does_not_exist"]

    def test_pop_resets_field_to_none(self):
        """pop() on a field returns the value and clears it."""
        trv = _make()
        trv.calibration_balance = {"valve_percent": 40}
        assert trv.pop("calibration_balance", None) == {"valve_percent": 40}
        assert trv.calibration_balance is None

    def test_pop_removes_extra_key(self):
        """pop() on an extra key behaves like dict.pop."""
        trv = _make()
        trv["_scratch"] = 1
        assert trv.pop("_scratch") == 1
        assert trv.pop("_scratch", "gone") == "gone"
        with pytest.raises(KeyError):
            trv.pop("_scratch")

    def test_truthiness(self):
        """A Trv instance is truthy (callers use ``entry or {}``)."""
        assert bool(_make()) is True
