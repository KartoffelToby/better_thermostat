"""Tests for the Trv domain object."""

import importlib

import pytest

from custom_components.better_thermostat.model_fixes import default as default_quirk
from custom_components.better_thermostat.trv import (
    ECHO_SETPOINTS_LIMIT,
    PendingSetpoint,
    Trv,
)


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
        assert trv.last_calibration is None
        assert trv.last_calibration_requested is None
        assert trv.advanced == {}
        assert trv.extra == {}

    def test_attribute_writes(self):
        """Fields are plain mutable attributes."""
        trv = _make()
        trv.current_temperature = 21.5
        trv.ignore_trv_states = True
        trv.hvac_action = "heating"
        assert trv.current_temperature == 21.5
        assert trv.ignore_trv_states is True
        assert trv.hvac_action == "heating"


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

    def test_from_legacy_dict_maps_the_requested_calibration(self):
        """The pre-clamp offset intent is a typed field, not a scratch key."""
        trv = Trv.from_legacy_dict(
            "climate.trv",
            {"last_calibration": -3.0, "last_calibration_requested": -5.0},
        )
        assert trv.last_calibration == -3.0
        assert trv.last_calibration_requested == -5.0
        assert trv.extra == {}

    def test_from_legacy_dict_explicit_entity_id_wins(self):
        """An ``entity_id`` key in the dict yields to the explicit argument."""
        trv = Trv.from_legacy_dict(
            "climate.trv", {"entity_id": "climate.stale", "current_temperature": 21.0}
        )
        assert trv.entity_id == "climate.trv"
        assert trv.current_temperature == 21.0
        assert trv.extra == {}

    def test_from_legacy_dict_merges_extra_key(self):
        """An ``extra`` key is merged into the scratchpad, not nested under it."""
        trv = Trv.from_legacy_dict(
            "climate.trv", {"extra": {"_quirk_scratch": 3}, "_other_scratch": 7}
        )
        assert trv.extra == {"_quirk_scratch": 3, "_other_scratch": 7}

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


class TestEchoSetpoints:
    """The setpoints a report may carry as BT's own write."""

    def test_a_fresh_trv_expects_no_echo(self):
        """Nothing has been written or confirmed, so nothing can echo."""
        trv = _make()
        assert trv.echo_setpoint_values() == []
        assert trv.confirmed_setpoint is None
        assert trv.last_setpoint_write_id == 0

    def test_each_trv_keeps_its_own_list(self):
        """A write remembered on one TRV is unknown to another."""
        first = _make()
        first.remember_setpoint_written(26.0)
        assert _make().echo_setpoint_values() == []

    def test_a_written_setpoint_is_remembered(self):
        """A write joins the values the device may echo."""
        trv = _make()
        trv.remember_setpoint_written(26.0)
        assert trv.echo_setpoint_values() == [26.0]

    def test_remembering_no_setpoint_adds_nothing(self):
        """A TRV with no setpoint on record has nothing to remember."""
        trv = _make()
        trv.remember_setpoint_written(26.0)
        trv.remember_setpoint_written(None)
        assert trv.echo_setpoint_values() == [26.0]

    def test_remembering_no_setpoint_does_not_spend_an_id(self):
        """An id stands for a command on the wire; nothing went out here."""
        trv = _make()
        written = trv.remember_setpoint_written(26.0)
        assert trv.remember_setpoint_written(None) == written

    def test_each_write_takes_the_next_id(self):
        """The id says when the command went out, so it only ever rises."""
        trv = _make()
        assert trv.remember_setpoint_written(26.0) == 1
        assert trv.remember_setpoint_written(25.0) == 2
        assert trv.remember_setpoint_written(26.0) == 3

    def test_a_repeated_write_is_remembered_once(self):
        """Writing a value that is already remembered adds no entry."""
        trv = _make()
        trv.remember_setpoint_written(26.0)
        trv.remember_setpoint_written(25.0)
        trv.remember_setpoint_written(26.0)
        assert sorted(trv.echo_setpoint_values()) == [25.0, 26.0]

    def test_a_repeated_write_becomes_the_newest_again(self):
        """The command last on the wire is the last one the bound gives up."""
        trv = _make()
        trv.remember_setpoint_written(26.0)
        trv.remember_setpoint_written(25.0)
        trv.remember_setpoint_written(26.0)
        assert trv.echo_setpoint_values() == [25.0, 26.0]

    def test_a_repeated_write_outlives_an_older_one_under_the_bound(self):
        """Re-sending 21.0 makes 22.0 the oldest, so 22.0 goes first."""
        trv = _make()
        for value in (21.0, 22.0, 23.0, 24.0, 25.0, 26.0, 27.0, 28.0):
            trv.remember_setpoint_written(value)
        trv.remember_setpoint_written(21.0)
        trv.remember_setpoint_written(29.0)
        assert 21.0 in trv.echo_setpoint_values()
        assert 22.0 not in trv.echo_setpoint_values()

    def test_the_bound_drops_the_oldest_write(self):
        """The bound counts the writes since the confirmation, oldest first."""
        trv = _make()
        trv.remember_setpoint_confirmed(20.0)
        for value in (21.0, 22.0, 23.0, 24.0, 25.0, 26.0, 27.0, 28.0, 29.0):
            trv.remember_setpoint_written(value)
        assert len(trv.echo_setpoint_values()) == ECHO_SETPOINTS_LIMIT
        assert trv.echo_setpoint_values() == [
            22.0,
            23.0,
            24.0,
            25.0,
            26.0,
            27.0,
            28.0,
            29.0,
        ]

    def test_the_confirmed_setpoint_outlives_the_bound(self):
        """The bound counts the writes alone, so the confirmed setpoint outlives it."""
        trv = _make()
        trv.remember_setpoint_confirmed(20.0)
        for value in (21.0, 22.0, 23.0, 24.0, 25.0, 26.0, 27.0, 28.0, 29.0):
            trv.remember_setpoint_written(value)
        assert trv.confirmed_setpoint == 20.0

    def test_an_unconfirmed_write_does_not_pin_the_oldest_value(self):
        """With no confirmation yet, the first write is evicted like any other."""
        trv = _make()
        for value in (21.0, 22.0, 23.0, 24.0, 25.0, 26.0, 27.0, 28.0, 29.0):
            trv.remember_setpoint_written(value)
        assert 21.0 not in trv.echo_setpoint_values()
        assert trv.echo_setpoint_values()[-1] == 29.0

    def test_a_confirmation_retires_the_writes_it_covers(self):
        """The awaited command and the writes before it cannot come back."""
        trv = _make()
        trv.remember_setpoint_written(26.0)
        awaited = trv.remember_setpoint_written(25.0)
        trv.remember_setpoint_confirmed(25.0, awaited)
        assert trv.confirmed_setpoint == 25.0
        assert trv.echo_setpoint_values() == []

    def test_a_confirmation_older_than_one_already_recorded_is_dropped(self):
        """Handing a shared device over lets two watchdogs answer out of order.

        The heating channel's watchdog is released without being stopped, so
        a second one can start and confirm first. The older answer must not
        put its command back as the one the device holds.
        """
        trv = _make()
        older = trv.remember_setpoint_written(23.0)
        newer = trv.remember_setpoint_written(25.0)
        trv.remember_setpoint_confirmed(25.0, newer)
        trv.remember_setpoint_confirmed(23.0, older)
        assert trv.confirmed_setpoint == 25.0

    def test_a_confirmation_at_the_recorded_id_still_applies(self):
        """Only an older answer is dropped; the same watchdog may answer once."""
        trv = _make()
        awaited = trv.remember_setpoint_written(23.0)
        trv.remember_setpoint_confirmed(23.0, awaited)
        assert trv.confirmed_setpoint == 23.0
        assert trv.confirmed_write_id == awaited

    def test_a_confirmation_keeps_the_writes_issued_after_the_command(self):
        """Only one write is watched, so 24.0 and 25.0 are still in flight."""
        trv = _make()
        awaited = trv.remember_setpoint_written(23.0)
        trv.remember_setpoint_written(24.0)
        trv.remember_setpoint_written(25.0)
        trv.remember_setpoint_confirmed(23.0, awaited)
        assert trv.confirmed_setpoint == 23.0
        assert trv.echo_setpoint_values() == [24.0, 25.0]

    def test_a_command_sent_again_does_not_retire_the_writes_between(self):
        """Confirming the first 23.0 must not take 24.0 with it.

        The device may be reporting either 23.0, and 24.0 went out after the
        one that was awaited, so it can still come back.
        """
        trv = _make()
        awaited = trv.remember_setpoint_written(23.0)
        trv.remember_setpoint_written(24.0)
        trv.remember_setpoint_written(23.0)
        trv.remember_setpoint_confirmed(23.0, awaited)
        assert trv.confirmed_setpoint == 23.0
        assert trv.echo_setpoint_values() == [24.0, 23.0]

    def test_a_confirmation_without_a_write_id_retires_nothing(self):
        """A caller that confirms without waiting has no boundary to retire against."""
        trv = _make()
        trv.remember_setpoint_written(26.0)
        trv.remember_setpoint_confirmed(26.0)
        assert trv.confirmed_setpoint == 26.0
        assert trv.echo_setpoint_values() == [26.0]

    def test_a_confirmation_without_a_command_retires_nothing(self):
        """A report with no setpoint confirms nothing, so nothing retires."""
        trv = _make()
        trv.remember_setpoint_written(26.0)
        trv.remember_setpoint_confirmed(None)
        assert trv.confirmed_setpoint is None
        assert trv.echo_setpoint_values() == [26.0]

    def test_from_legacy_dict_fills_the_writes_from_the_dict(self):
        """The list is a typed field like the rest, with its own default."""
        seeded = Trv.from_legacy_dict(
            "climate.trv", {"pending_setpoints": [PendingSetpoint(26.0, 1)]}
        )
        bare = Trv.from_legacy_dict("climate.trv", {})
        assert seeded.echo_setpoint_values() == [26.0]
        assert bare.echo_setpoint_values() == []


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

    def test_unknown_hvac_modes_disable_off(self):
        """A TRV that never reported its modes is conservatively no-off.

        BT then sends min temp instead of an OFF the device may not
        support.
        """
        trv = _make()
        assert trv.hvac_modes is None
        assert trv.capabilities().supports_off_mode is False

    def test_off_in_hvac_modes_enables_off(self):
        """A reported mode list containing off keeps the OFF capability."""
        trv = _make()
        trv.hvac_modes = ["heat", "off"]
        assert trv.capabilities().supports_off_mode is True

    def test_hvac_modes_without_off_disable_off(self):
        """A reported mode list without off yields no OFF capability."""
        trv = _make()
        trv.hvac_modes = ["heat", "auto"]
        assert trv.capabilities().supports_off_mode is False

    def test_off_offered_in_the_device_spelling_enables_off(self):
        """A list naming its modes ``HVACMode.OFF`` still offers OFF.

        The cached list holds the device's own spelling, so the capability
        is decided on the normalized list.
        """
        trv = _make()
        trv.hvac_modes = ["HVACMode.HEAT", "HVACMode.OFF"]
        assert trv.capabilities().supports_off_mode is True

    def test_no_off_in_the_device_spelling_disables_off(self):
        """A device genuinely without OFF still yields no OFF capability."""
        trv = _make()
        trv.hvac_modes = ["HVACMode.HEAT", "HVACMode.AUTO"]
        assert trv.capabilities().supports_off_mode is False

    def test_no_off_system_mode_config_disables_off(self):
        """The explicit no_off_system_mode config wins over the mode list."""
        trv = _make()
        trv.hvac_modes = ["heat", "off"]
        trv.advanced = {"no_off_system_mode": True}
        assert trv.capabilities().supports_off_mode is False

    def test_valve_capability_from_quirk_override(self):
        """A quirk-provided override_set_valve enables valve writes."""

        class _Quirk:
            @staticmethod
            async def override_set_valve(bt, entity_id, pct):
                return True

        trv = _make()
        trv.model_quirks = _Quirk()
        assert trv.capabilities().supports_valve_write is True

    def test_the_default_quirks_claim_no_valve_support(self):
        """A model without a quirk file of its own gets the default module.

        Every device whose model has no ``model_fixes/<model>.py`` loads
        this module, so a valve override living in it would report valve
        support for the entire long tail of TRVs.
        """
        trv = _make()
        trv.model_quirks = default_quirk
        trv.valve_position_entity = None
        assert trv.capabilities().supports_valve_write is False

    @pytest.mark.parametrize("model", ["TRVZB", "ZWA021"])
    def test_a_model_that_drives_its_valve_keeps_the_capability(self, model):
        """The modules that do command a valve still report one."""
        trv = _make()
        trv.model_quirks = importlib.import_module(
            f"custom_components.better_thermostat.model_fixes.{model}"
        )
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
