"""Data-dependent entries of BetterThermostat.extra_state_attributes.

``next_valve_maintenance`` carries the maintenance schedule, which the entity
always holds; it is published only while at least one TRV has valve
maintenance enabled. ``valve_method`` is published only when at least one TRV has
recorded how its valve was last written.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from homeassistant.const import UnitOfTemperature

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.core.containers import BtConfig
from custom_components.better_thermostat.core.decide import running_kernel_state
from custom_components.better_thermostat.core.fsm.maintenance import (
    DUE_AT_FIRST_TICK,
    MaintenanceIdle,
)
from custom_components.better_thermostat.utils.state_manager import StateManager
from tests.factories import make_state_attributes_bt, trv_record


def _extra_state_attributes(bt: BetterThermostat) -> dict[str, object]:
    """Read the property off the stand-in through the class's own getter."""
    getter = BetterThermostat.extra_state_attributes.fget
    assert getter is not None
    return getter(bt)


class TestNextValveMaintenance:
    """The schedule is published as ISO8601 while a TRV has maintenance on."""

    _DUE = datetime(2026, 1, 8, 12, 0, tzinfo=UTC)

    def _bt(self, *, maintenance: bool, next_due: datetime = _DUE):
        return make_state_attributes_bt(
            kernel_state=replace(
                running_kernel_state(), maintenance=MaintenanceIdle(next_due=next_due)
            ),
            real_trvs={
                "climate.trv": trv_record(
                    entity_id="climate.trv", advanced={"valve_maintenance": maintenance}
                )
            },
        )

    def test_a_new_thermostat_holds_a_schedule_of_its_own(self):
        """The constructor schedules the first check one hour to five days out."""
        thermostat = BetterThermostat(
            config=BtConfig(sensor_entity_id="sensor.room_temperature"),
            trv_configs=[],
            min_target_temperature=None,
            max_target_temperature=None,
            target_temperature_step=None,
            enabled_presets=None,
            unit=UnitOfTemperature.CELSIUS,
            unique_id="maintenance_seed",
            state_mgr=StateManager(MagicMock(), "maintenance_seed"),
            device_class=None,
            state_class=None,
        )
        now = thermostat.clock.now()

        next_due = thermostat.kernel_state.maintenance.next_due

        assert next_due != DUE_AT_FIRST_TICK
        assert now < next_due <= now + timedelta(hours=24 * 5)

    def test_timestamp_is_published_as_iso8601(self):
        """A datetime schedule is rendered in ISO8601."""
        attrs = _extra_state_attributes(self._bt(maintenance=True))
        assert attrs["next_valve_maintenance"] == self._DUE.isoformat()

    def test_absent_when_no_trv_has_valve_maintenance_enabled(self):
        """No run happens on the date, so none is shown."""
        attrs = _extra_state_attributes(self._bt(maintenance=False))
        assert "next_valve_maintenance" not in attrs

    def test_one_enabled_trv_among_several_publishes_the_schedule(self):
        """Maintenance on any one TRV is enough for the date to hold."""
        bt = self._bt(maintenance=False)
        bt.real_trvs["climate.other"] = trv_record(
            entity_id="climate.other", advanced={"valve_maintenance": True}
        )
        attrs = _extra_state_attributes(bt)
        assert attrs["next_valve_maintenance"] == self._DUE.isoformat()


class TestValveMethod:
    """The per-TRV valve-method summary is published when a TRV reports one."""

    def test_absent_without_reported_methods(self):
        """No TRV reported a method, so no key."""
        bt = make_state_attributes_bt(
            real_trvs={"climate.trv": trv_record(entity_id="climate.trv")}
        )
        attrs = _extra_state_attributes(bt)
        assert "valve_method" not in attrs

    def test_reported_methods_are_summarized(self):
        """Each TRV that reports a method appears in the summary."""
        trv = trv_record(entity_id="climate.trv", last_valve_method="adapter")
        bt = make_state_attributes_bt(real_trvs={"climate.trv": trv})
        attrs = _extra_state_attributes(bt)
        assert attrs["valve_method"] == {"climate.trv": "adapter"}

    def test_only_reporting_trvs_appear(self):
        """A TRV without a recorded method is left out of the summary."""
        reporting = trv_record(entity_id="climate.a", last_valve_method="adapter")
        silent = trv_record(entity_id="climate.b")
        bt = make_state_attributes_bt(
            real_trvs={"climate.a": reporting, "climate.b": silent}
        )
        attrs = _extra_state_attributes(bt)
        assert attrs["valve_method"] == {"climate.a": "adapter"}
