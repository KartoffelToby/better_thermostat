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

    def test_no_dict_protocol(self):
        """Trv does not speak the dict protocol: attribute access only."""
        trv = _make()
        with pytest.raises(TypeError):
            trv["current_temperature"]
        assert not hasattr(trv, "get")

    def test_truthiness(self):
        """A Trv instance is truthy (callers use ``entry or default``)."""
        assert bool(_make()) is True


class TestTrvCapabilities:
    """Capabilities derive from the discovered device surface."""

    def test_bare_trv_has_no_write_capabilities(self):
        """Without entities or quirks nothing is writable."""
        caps = _make().capabilities()
        assert caps.supports_offset_write is False
        assert caps.supports_valve_write is False

    def test_offset_capability_follows_the_calibration_entity(self):
        """A local calibration entity enables offset writes."""
        trv = _make()
        trv.local_temperature_calibration_entity = "number.cal"
        assert trv.capabilities().supports_offset_write is True

    def test_valve_capability_from_writable_entity(self):
        """A writable valve position entity enables valve writes."""
        trv = _make()
        trv.valve_position_entity = "number.valve"
        trv.valve_position_writable = True
        assert trv.capabilities().supports_valve_write is True

    def test_readonly_valve_entity_is_not_enough(self):
        """A read-only valve entity does not enable valve writes."""
        trv = _make()
        trv.valve_position_entity = "number.valve"
        trv.valve_position_writable = False
        assert trv.capabilities().supports_valve_write is False

    def test_valve_capability_from_quirk_override(self):
        """A quirk-provided override_set_valve enables valve writes."""

        class _Quirk:
            @staticmethod
            async def override_set_valve(bt, entity_id, pct):
                return True

        trv = _make()
        trv.model_quirks = _Quirk()
        assert trv.capabilities().supports_valve_write is True


class TestModelQuirksProtocol:
    """Every quirk module satisfies the structural quirk contract."""

    def test_all_quirk_modules_satisfy_the_protocol(self):
        """Each model_fixes module provides the full required surface."""
        import importlib
        import pkgutil

        from custom_components.better_thermostat import model_fixes
        from custom_components.better_thermostat.trv import ModelQuirks

        checked = []
        for info in pkgutil.iter_modules(model_fixes.__path__):
            if info.name in ("model_quirks", "types"):
                continue
            module = importlib.import_module(
                f"custom_components.better_thermostat.model_fixes.{info.name}"
            )
            assert isinstance(module, ModelQuirks), (
                f"{info.name} is missing part of the quirk surface"
            )
            checked.append(info.name)
        assert "default" in checked
