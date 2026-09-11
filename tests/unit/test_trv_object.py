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
        assert trv.last_calibration_requested is None
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

    def test_from_legacy_dict_maps_requested_calibration_onto_the_field(self):
        """``last_calibration_requested`` is a typed field, not an extra."""
        trv = Trv.from_legacy_dict(
            "climate.trv",
            {"last_calibration": -3.0, "last_calibration_requested": -5.0},
        )
        assert trv.last_calibration == -3.0
        assert trv.last_calibration_requested == -5.0
        assert trv.extra == {}

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


class TestEchoSetpoints:
    """The setpoints a device report may carry as BT's own value."""

    def test_a_fresh_trv_remembers_no_setpoint(self):
        """Nothing has been written or confirmed on a fresh Trv."""
        trv = _make()
        assert trv.echo_setpoints == []
        assert trv.confirmed_setpoint is None

    def test_from_legacy_dict_carries_the_list(self):
        """``echo_setpoints`` is a typed field, not an extra."""
        trv = Trv.from_legacy_dict("climate.trv", {"echo_setpoints": [21.0, 22.0]})
        assert trv.echo_setpoints == [21.0, 22.0]
        assert trv.extra == {}

    def test_a_written_setpoint_is_appended(self):
        """Each write joins the list behind the values already there."""
        trv = _make()
        trv.remember_setpoint_written(21.0)
        trv.remember_setpoint_written(22.0)
        assert trv.echo_setpoints == [21.0, 22.0]

    def test_a_repeated_write_is_kept_once(self):
        """Writing a value the list already holds does not duplicate it."""
        trv = _make()
        trv.remember_setpoint_written(21.0)
        trv.remember_setpoint_written(22.0)
        trv.remember_setpoint_written(21.0)
        assert sorted(trv.echo_setpoints) == [21.0, 22.0]

    def test_a_repeated_write_becomes_the_newest_again(self):
        """The command last on the wire is the last one the bound gives up."""
        trv = _make()
        trv.remember_setpoint_written(21.0)
        trv.remember_setpoint_written(22.0)
        trv.remember_setpoint_written(21.0)
        assert trv.echo_setpoints == [22.0, 21.0]

    def test_a_repeated_write_outlives_an_older_one_under_the_bound(self):
        """Re-sending 21.0 makes 22.0 the oldest, so 22.0 goes first."""
        trv = _make()
        for value in (21.0, 22.0, 23.0, 24.0, 25.0, 26.0, 27.0, 28.0):
            trv.remember_setpoint_written(value)
        trv.remember_setpoint_written(21.0)
        trv.remember_setpoint_written(29.0)
        assert 21.0 in trv.echo_setpoints
        assert 22.0 not in trv.echo_setpoints

    def test_a_full_list_drops_the_oldest_write(self):
        """The bound counts writes since the confirmation and drops the oldest."""
        trv = _make()
        trv.remember_setpoint_confirmed(20.0)
        for value in (21.0, 22.0, 23.0, 24.0, 25.0, 26.0, 27.0, 28.0, 29.0):
            trv.remember_setpoint_written(value)
        assert trv.echo_setpoints == [22.0, 23.0, 24.0, 25.0, 26.0, 27.0, 28.0, 29.0]

    def test_the_confirmed_setpoint_survives_the_bound(self):
        """The confirmed setpoint is held apart and is never evicted."""
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
        assert 21.0 not in trv.echo_setpoints
        assert trv.echo_setpoints[-1] == 29.0

    def test_a_confirmation_records_the_setpoint_and_retires_the_writes_before_it(self):
        """Once the device confirms, the writes before it can no longer come back."""
        trv = _make()
        trv.echo_setpoints = [20.0, 26.0, 25.0]
        trv.remember_setpoint_confirmed(25.0)
        assert trv.confirmed_setpoint == 25.0
        assert trv.echo_setpoints == []

    def test_a_confirmation_keeps_the_writes_issued_after_the_command(self):
        """Only one write is watched, so 24.0 and 25.0 are still in flight."""
        trv = _make()
        for value in (23.0, 24.0, 25.0):
            trv.remember_setpoint_written(value)
        trv.remember_setpoint_confirmed(23.0)
        assert trv.confirmed_setpoint == 23.0
        assert trv.echo_setpoints == [24.0, 25.0]

    def test_a_confirmation_of_an_evicted_command_retires_nothing(self):
        """Without the command in the list there is no boundary to cut at."""
        trv = _make()
        for value in (24.0, 25.0):
            trv.remember_setpoint_written(value)
        trv.remember_setpoint_confirmed(23.0)
        assert trv.confirmed_setpoint == 23.0
        assert trv.echo_setpoints == [24.0, 25.0]

    def test_a_confirmation_without_a_setpoint_retires_nothing(self):
        """A report with no setpoint confirms nothing, so nothing retires."""
        trv = _make()
        trv.echo_setpoints = [20.0]
        trv.remember_setpoint_confirmed(None)
        assert trv.confirmed_setpoint is None
        assert trv.echo_setpoints == [20.0]

    def test_a_confirmation_ignores_a_setpoint_another_task_moved_on_to(self):
        """The caller passes the command it waited on, not ``last_temperature``."""
        trv = _make()
        trv.remember_setpoint_written(23.0)
        trv.last_temperature = 8.0
        trv.remember_setpoint_confirmed(23.0)
        assert trv.confirmed_setpoint == 23.0
