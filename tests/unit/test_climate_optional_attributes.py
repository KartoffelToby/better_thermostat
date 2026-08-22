"""Optional entries of BetterThermostat.extra_state_attributes.

``next_valve_maintenance`` and ``valve_method`` are published only when the
entity actually holds the data behind them: a scheduled maintenance run, and
at least one TRV that has recorded how its valve was last written.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

from homeassistant.components.climate.const import HVACMode

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.trv import Trv


def _make_mock_bt(**overrides):
    """Construct a mock_bt sufficient for the ``extra_state_attributes`` property."""
    bt = MagicMock()
    bt.window_open = False
    bt.call_for_heat = True
    bt.last_change = datetime(2026, 5, 18, tzinfo=UTC)
    bt._saved_temperature = None
    bt._preset_temperature = None
    bt._current_humidity = None
    bt.humidity_sensor_entity_id = None
    bt.last_main_hvac_mode = HVACMode.HEAT
    bt.off_temperature = None
    bt.tolerance = 0.5
    bt.bt_target_temp_step = 0.5
    bt.heating_power = 0.1
    bt.heat_loss_rate = 0.0
    bt.devices_errors = []
    bt.devices_states = {}
    bt.cur_temp_filtered = 20.5
    bt.degraded_mode = False
    bt.unavailable_sensors = []
    bt.real_trvs = {}
    bt.heating_cycles = []
    bt.loss_cycles = []
    bt.last_heating_power_stats = {}
    bt.last_heat_loss_stats = {}
    bt.next_valve_maintenance = None
    bt._preset_cool_temperatures = {}
    for k, v in overrides.items():
        setattr(bt, k, v)
    return bt


class TestNextValveMaintenance:
    """The schedule stamp is published as ISO8601 when it is a timestamp."""

    def test_absent_without_a_schedule(self):
        """No schedule means no key."""
        bt = _make_mock_bt(next_valve_maintenance=None)
        attrs = BetterThermostat.extra_state_attributes.fget(bt)
        assert "next_valve_maintenance" not in attrs

    def test_timestamp_is_published_as_iso8601(self):
        """A datetime schedule is rendered in ISO8601."""
        due = datetime(2026, 1, 8, 12, 0, tzinfo=UTC)
        bt = _make_mock_bt(next_valve_maintenance=due)
        attrs = BetterThermostat.extra_state_attributes.fget(bt)
        assert attrs["next_valve_maintenance"] == due.isoformat()


class TestValveMethod:
    """The per-TRV valve-method summary is published when a TRV reports one."""

    def test_absent_without_reported_methods(self):
        """No TRV reported a method, so no key."""
        bt = _make_mock_bt(real_trvs={"climate.trv": Trv(entity_id="climate.trv")})
        attrs = BetterThermostat.extra_state_attributes.fget(bt)
        assert "valve_method" not in attrs

    def test_reported_methods_are_summarized(self):
        """Each TRV that reports a method appears in the summary."""
        trv = Trv(entity_id="climate.trv", last_valve_method="adapter")
        bt = _make_mock_bt(real_trvs={"climate.trv": trv})
        attrs = BetterThermostat.extra_state_attributes.fget(bt)
        assert attrs["valve_method"] == {"climate.trv": "adapter"}

    def test_only_reporting_trvs_appear(self):
        """A TRV without a recorded method is left out of the summary."""
        reporting = Trv(entity_id="climate.a", last_valve_method="adapter")
        silent = Trv(entity_id="climate.b")
        bt = _make_mock_bt(real_trvs={"climate.a": reporting, "climate.b": silent})
        attrs = BetterThermostat.extra_state_attributes.fget(bt)
        assert attrs["valve_method"] == {"climate.a": "adapter"}
