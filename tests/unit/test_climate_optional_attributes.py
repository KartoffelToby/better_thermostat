"""Optional entries of BetterThermostat.extra_state_attributes.

``next_valve_maintenance`` and ``valve_method`` are published only when the
entity actually holds the data behind them: a scheduled maintenance run, and
at least one TRV that has recorded how its valve was last written. A schedule
stamp that is not a timestamp surfaces as an error rather than as a silently
missing attribute.
"""

from datetime import UTC, datetime

import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.trv import Trv
from tests.factories import make_state_attributes_bt


class TestNextValveMaintenance:
    """The schedule stamp is published as ISO8601 when it is a timestamp."""

    def test_absent_without_a_schedule(self):
        """No schedule means no key."""
        bt = make_state_attributes_bt(next_valve_maintenance=None)
        attrs = BetterThermostat.extra_state_attributes.fget(bt)
        assert "next_valve_maintenance" not in attrs

    def test_timestamp_is_published_as_iso8601(self):
        """A datetime schedule is rendered in ISO8601."""
        due = datetime(2026, 1, 8, 12, 0, tzinfo=UTC)
        bt = make_state_attributes_bt(next_valve_maintenance=due)
        attrs = BetterThermostat.extra_state_attributes.fget(bt)
        assert attrs["next_valve_maintenance"] == due.isoformat()

    def test_a_schedule_that_is_not_a_timestamp_surfaces(self):
        """A stamp without ``isoformat`` raises rather than dropping the key."""
        bt = make_state_attributes_bt(next_valve_maintenance="2026-01-08T12:00:00")
        with pytest.raises(AttributeError):
            BetterThermostat.extra_state_attributes.fget(bt)


class TestValveMethod:
    """The per-TRV valve-method summary is published when a TRV reports one."""

    def test_absent_without_reported_methods(self):
        """No TRV reported a method, so no key."""
        bt = make_state_attributes_bt(
            real_trvs={"climate.trv": Trv(entity_id="climate.trv")}
        )
        attrs = BetterThermostat.extra_state_attributes.fget(bt)
        assert "valve_method" not in attrs

    def test_reported_methods_are_summarized(self):
        """Each TRV that reports a method appears in the summary."""
        trv = Trv(entity_id="climate.trv", last_valve_method="adapter")
        bt = make_state_attributes_bt(real_trvs={"climate.trv": trv})
        attrs = BetterThermostat.extra_state_attributes.fget(bt)
        assert attrs["valve_method"] == {"climate.trv": "adapter"}

    def test_only_reporting_trvs_appear(self):
        """A TRV without a recorded method is left out of the summary."""
        reporting = Trv(entity_id="climate.a", last_valve_method="adapter")
        silent = Trv(entity_id="climate.b")
        bt = make_state_attributes_bt(
            real_trvs={"climate.a": reporting, "climate.b": silent}
        )
        attrs = BetterThermostat.extra_state_attributes.fget(bt)
        assert attrs["valve_method"] == {"climate.a": "adapter"}
