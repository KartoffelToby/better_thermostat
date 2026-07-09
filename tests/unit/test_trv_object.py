"""Tests for the Trv domain object."""

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


class TestExtraScratchpad:
    """Quirk-private bookkeeping lives in the ``extra`` dict."""

    def test_extra_starts_empty(self):
        """A fresh Trv has no scratchpad entries."""
        assert _make().extra == {}

    def test_extra_holds_quirk_keys(self):
        """Quirk keys are plain dict entries on ``extra``."""
        trv = _make()
        trv.extra["_trvzb_valve_bump_seq"] = 7
        assert trv.extra.get("_trvzb_valve_bump_seq") == 7

    def test_from_legacy_dict_splits_fields_and_extras(self):
        """Known keys become fields; unknown keys land in ``extra``."""
        trv = Trv.from_legacy_dict(
            "climate.trv",
            {
                "current_temperature": 21.0,
                "_quirk_scratch": 3,
                "advanced": {"child_lock": True},
            },
        )
        assert trv.current_temperature == 21.0
        assert trv.advanced == {"child_lock": True}
        assert trv.extra == {"_quirk_scratch": 3}

    def test_from_legacy_dict_ignores_entity_id_key(self):
        """A legacy ``entity_id`` key never collides with the argument."""
        trv = Trv.from_legacy_dict(
            "climate.trv", {"entity_id": "climate.stale", "model": "TRVZB"}
        )
        assert trv.entity_id == "climate.trv"
        assert trv.model == "TRVZB"
        assert "entity_id" not in trv.extra

    def test_from_legacy_dict_merges_extra_dict(self):
        """A legacy ``extra`` dict is flattened into ``extra``, not nested."""
        trv = Trv.from_legacy_dict(
            "climate.trv", {"extra": {"_seq": 7}, "_quirk_scratch": 3}
        )
        assert trv.extra == {"_seq": 7, "_quirk_scratch": 3}

    def test_from_legacy_dict_keeps_non_dict_extra_value(self):
        """A non-dict legacy ``extra`` value survives under the ``extra`` key."""
        trv = Trv.from_legacy_dict("climate.trv", {"extra": 42})
        assert trv.extra == {"extra": 42}

    def test_no_dict_protocol(self):
        """Trv does not speak the dict protocol: attribute access only."""
        trv = _make()
        with pytest.raises(TypeError):
            trv["current_temperature"]
        assert not hasattr(trv, "get")

    def test_truthiness(self):
        """A Trv instance is truthy (callers use ``entry or default``)."""
        assert bool(_make()) is True
