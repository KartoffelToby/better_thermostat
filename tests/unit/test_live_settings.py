"""The outdoor threshold and the tolerance apply to a running thermostat.

The entry's settings are the one source of both. The update listener hands a
change of them alone to the thermostat instead of reloading the entry, and
the thermostat reads them the way setup does.
"""

import asyncio
from dataclasses import dataclass
import math
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.climate import HVACMode
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import UnitOfTemperature
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.better_thermostat import (
    BetterThermostatData,
    config_entry_update_listener,
    store_live_setting,
)
from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.core.containers import BtConfig
from custom_components.better_thermostat.core.decide import KernelState
from custom_components.better_thermostat.core.fsm.lifecycle import (
    LifecyclePhase,
    LifecycleState,
)
from custom_components.better_thermostat.number import (
    BetterThermostatOffTemperatureNumber,
    BetterThermostatToleranceNumber,
)
from custom_components.better_thermostat.utils.const import DOMAIN
from custom_components.better_thermostat.utils.entry_schema import (
    BtSettings,
    parse_settings,
)
from tests.factories import ThermostatStandIn

_STORED: dict[str, object] = {
    "name": "Living room",
    "thermostat": [{"trv": "climate.trv", "integration": "zha"}],
    "outdoor_sensor": "sensor.outdoor",
    "off_temperature": 20,
    "tolerance": 0.3,
}


def _settings(**changes: object) -> BtSettings:
    return parse_settings({**_STORED, **changes})


@dataclass(frozen=True)
class _Shell:
    """A thermostat running on the stored settings, and what it calls out to."""

    bt: BetterThermostat
    write_state: MagicMock
    spawn: MagicMock
    check_weather: MagicMock
    clock: MagicMock


def _thermostat(
    unit: UnitOfTemperature = UnitOfTemperature.CELSIUS,
    *,
    phase: LifecyclePhase = LifecyclePhase.RUNNING,
    hvac_mode: HVACMode = HVACMode.HEAT,
) -> _Shell:
    """Return a thermostat shell running on the stored settings."""
    bt = object.__new__(BetterThermostat)
    bt.config = BtConfig(device_name="Living room", off_temperature=20.0, tolerance=0.3)
    bt._unit = unit
    bt.kernel_state = KernelState(lifecycle=LifecycleState(phase=phase))
    bt.bt_hvac_mode = hvac_mode
    bt.control_queue_task = asyncio.Queue(maxsize=1)
    shell = _Shell(
        bt=bt,
        write_state=MagicMock(),
        spawn=MagicMock(side_effect=lambda coro, name: coro.close()),
        check_weather=MagicMock(side_effect=lambda when: AsyncMock()()),
        clock=MagicMock(),
    )
    bt.async_write_ha_state = shell.write_state
    bt._spawn_owned = shell.spawn
    bt._trigger_check_weather = shell.check_weather
    bt.clock = shell.clock
    return shell


class TestThermostatTakesLiveSettings:
    def test_unchanged_settings_change_nothing(self):
        shell = _thermostat()
        bt = shell.bt
        before = bt.config

        bt.apply_live_settings(_settings())

        assert bt.config is before
        shell.write_state.assert_not_called()
        shell.spawn.assert_not_called()
        assert bt.control_queue_task.empty()

    def test_a_new_tolerance_requests_a_cycle_and_no_outdoor_check(self):
        shell = _thermostat()
        bt = shell.bt

        bt.apply_live_settings(_settings(tolerance=0.6))

        assert bt.tolerance == 0.6
        assert bt.off_temperature == 20.0
        assert bt.control_queue_task.qsize() == 1
        shell.spawn.assert_not_called()
        shell.write_state.assert_called_once()

    def test_a_new_threshold_runs_the_outdoor_check_and_no_cycle(self):
        shell = _thermostat()
        bt = shell.bt

        bt.apply_live_settings(_settings(off_temperature=12))

        assert bt.off_temperature == 12.0
        shell.check_weather.assert_called_once_with(shell.clock.utcnow.return_value)
        shell.spawn.assert_called_once()
        assert bt.control_queue_task.empty()

    def test_both_changed_run_both(self):
        shell = _thermostat()
        bt = shell.bt

        bt.apply_live_settings(_settings(off_temperature=12, tolerance=0.5))

        shell.spawn.assert_called_once()
        assert bt.control_queue_task.qsize() == 1

    def test_fahrenheit_values_are_read_as_setup_reads_them(self):
        shell = _thermostat(UnitOfTemperature.FAHRENHEIT)
        bt = shell.bt

        bt.apply_live_settings(_settings(off_temperature=50, tolerance=0.9))

        assert bt.off_temperature == pytest.approx(10.0)
        assert bt.tolerance == pytest.approx(0.5)

    def test_during_startup_the_values_wait_for_the_startup_checks(self):
        shell = _thermostat(phase=LifecyclePhase.INITIALISING)
        bt = shell.bt

        bt.apply_live_settings(_settings(off_temperature=12, tolerance=0.6))

        assert bt.off_temperature == 12.0
        assert bt.tolerance == 0.6
        shell.spawn.assert_not_called()
        assert bt.control_queue_task.empty()

    def test_a_room_switched_off_gets_no_cycle(self):
        shell = _thermostat(hvac_mode=HVACMode.OFF)
        bt = shell.bt

        bt.apply_live_settings(_settings(tolerance=0.6))

        assert bt.tolerance == 0.6
        assert bt.control_queue_task.empty()

    def test_an_unreadable_value_falls_back_as_at_setup(self):
        shell = _thermostat()
        bt = shell.bt

        bt.apply_live_settings(_settings(off_temperature=500, tolerance="wide"))

        assert bt.off_temperature is None
        assert bt.tolerance == 0.0


def _loaded_entry(hass, climate, **stored: object) -> MockConfigEntry:
    """Return a loaded entry whose thermostat runs on ``stored``."""
    options = {**_STORED, **stored}
    entry = MockConfigEntry(domain=DOMAIN, options=options, title="Living room")
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    entry.runtime_data = BetterThermostatData(
        climate=climate, settings=parse_settings(options)
    )
    return entry


@pytest.fixture
def reload(hass):
    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload:
        yield reload


class TestUpdateListener:
    async def test_a_live_setting_alone_applies_without_a_reload(self, hass, reload):
        climate = ThermostatStandIn(spec=BetterThermostat)
        entry = _loaded_entry(hass, climate)
        hass.config_entries.async_update_entry(
            entry, options={**_STORED, "tolerance": 0.6}
        )

        await config_entry_update_listener(hass, entry)

        reload.assert_not_called()
        climate.apply_live_settings.assert_called_once_with(_settings(tolerance=0.6))
        assert entry.runtime_data.settings == _settings(tolerance=0.6)

    async def test_any_other_change_reloads(self, hass, reload):
        climate = ThermostatStandIn(spec=BetterThermostat)
        entry = _loaded_entry(hass, climate)
        hass.config_entries.async_update_entry(
            entry, options={**_STORED, "tolerance": 0.6, "weather": "weather.home"}
        )

        await config_entry_update_listener(hass, entry)

        reload.assert_awaited_once_with(entry.entry_id)
        climate.apply_live_settings.assert_not_called()
        assert entry.runtime_data.settings == _settings()

    async def test_settings_that_do_not_parse_reload(self, hass, reload):
        climate = ThermostatStandIn(spec=BetterThermostat)
        entry = _loaded_entry(hass, climate)
        hass.config_entries.async_update_entry(
            entry, options={**_STORED, "thermostat": "climate.trv"}
        )

        await config_entry_update_listener(hass, entry)

        reload.assert_awaited_once_with(entry.entry_id)
        climate.apply_live_settings.assert_not_called()

    async def test_an_entry_without_a_thermostat_reloads(self, hass, reload):
        entry = _loaded_entry(hass, None)
        hass.config_entries.async_update_entry(
            entry, options={**_STORED, "tolerance": 0.6}
        )

        await config_entry_update_listener(hass, entry)

        reload.assert_awaited_once_with(entry.entry_id)

    async def test_an_entry_that_is_not_loaded_reloads(self, hass, reload):
        climate = ThermostatStandIn(spec=BetterThermostat)
        entry = _loaded_entry(hass, climate)
        entry.mock_state(hass, ConfigEntryState.SETUP_ERROR)
        hass.config_entries.async_update_entry(
            entry, options={**_STORED, "tolerance": 0.6}
        )

        await config_entry_update_listener(hass, entry)

        reload.assert_awaited_once_with(entry.entry_id)
        climate.apply_live_settings.assert_not_called()


class TestStoreLiveSetting:
    async def test_writes_the_options_and_applies_them_at_once(self, hass, reload):
        climate = ThermostatStandIn(spec=BetterThermostat)
        entry = _loaded_entry(hass, climate)

        store_live_setting(hass, entry, "off_temperature", 15)

        assert entry.options["off_temperature"] == 15
        assert entry.options["tolerance"] == 0.3
        assert entry.data == {}
        climate.apply_live_settings.assert_called_once_with(
            _settings(off_temperature=15)
        )
        await hass.async_block_till_done()
        reload.assert_not_called()


def _number_thermostat(
    off_temperature: float | None, tolerance: float
) -> ThermostatStandIn:
    climate = ThermostatStandIn()
    climate._unique_id = "entry"
    climate.device_name = "Living room"
    climate.off_temperature = off_temperature
    climate.tolerance = tolerance
    return climate


class TestNumbers:
    def test_the_threshold_shows_in_the_system_unit(self, hass):
        number = BetterThermostatOffTemperatureNumber(
            _number_thermostat(10.0, 0.0),
            MagicMock(spec=MockConfigEntry),
            UnitOfTemperature.FAHRENHEIT,
        )

        assert number.native_value == 50.0
        assert number.native_unit_of_measurement == UnitOfTemperature.FAHRENHEIT

    def test_an_unreadable_threshold_shows_as_unknown(self):
        number = BetterThermostatOffTemperatureNumber(
            _number_thermostat(None, 0.0),
            MagicMock(spec=MockConfigEntry),
            UnitOfTemperature.CELSIUS,
        )

        assert number.native_value is None

    @pytest.mark.parametrize(
        ("unit", "kelvin", "shown", "maximum"),
        [
            (UnitOfTemperature.CELSIUS, 0.3, 0.3, 10.0),
            (UnitOfTemperature.FAHRENHEIT, 0.5, 0.9, 18.0),
        ],
    )
    def test_the_tolerance_shows_as_a_difference_in_the_system_unit(
        self, unit, kelvin, shown, maximum
    ):
        number = BetterThermostatToleranceNumber(
            _number_thermostat(None, kelvin), MagicMock(spec=MockConfigEntry), unit
        )

        assert number.native_value == shown
        assert number.native_max_value == maximum
        assert number.native_min_value == 0.0

    @pytest.mark.parametrize(
        ("number_type", "value", "stored"),
        [
            (BetterThermostatOffTemperatureNumber, 17.6, 18),
            (BetterThermostatToleranceNumber, 0.30000000000000004, 0.3),
        ],
    )
    async def test_a_set_value_is_stored_rounded(self, number_type, value, stored):
        entry = MagicMock(spec=MockConfigEntry)
        number = number_type(
            _number_thermostat(20.0, 0.3), entry, UnitOfTemperature.CELSIUS
        )
        number.hass = MagicMock()
        number.async_write_ha_state = MagicMock()
        with patch(
            "custom_components.better_thermostat.number.store_live_setting"
        ) as store:
            await number.async_set_native_value(value)

        ((_, written_entry, _, written),) = [call.args for call in store.mock_calls]
        assert written_entry is entry
        assert written == stored
        number.async_write_ha_state.assert_called_once()

    @pytest.mark.parametrize(
        "number_type",
        [BetterThermostatOffTemperatureNumber, BetterThermostatToleranceNumber],
    )
    async def test_nan_is_refused(self, number_type):
        number = number_type(
            _number_thermostat(20.0, 0.3),
            MagicMock(spec=MockConfigEntry),
            UnitOfTemperature.CELSIUS,
        )
        number.hass = MagicMock()
        number.async_write_ha_state = MagicMock()
        with patch(
            "custom_components.better_thermostat.number.store_live_setting"
        ) as store:
            await number.async_set_native_value(math.nan)

        store.assert_not_called()
