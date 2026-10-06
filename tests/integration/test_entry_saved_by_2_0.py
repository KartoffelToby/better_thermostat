"""An entry last saved by Better Thermostat 2.0 runs here unchanged.

2.0 stores an entry at minor version 2, with its settings in the entry's
options and its data empty. Home Assistant loads such an entry here, because
the major version is the same, and this version reads the options over the
data, so a user who goes back from 2.0 keeps every setting. Saving the
settings here writes them to the data and empties the options, which is how
2.0 recognises the entry when it is installed again.
"""

from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.better_thermostat.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.better_thermostat.utils.const import (
    CONF_HEATER,
    CONF_SENSOR,
    CONF_TOLERANCE,
)

from .conftest import (
    DOMAIN,
    SENSOR_ID,
    TRV_ID,
    make_entry,
    setup_entry,
    wait_for_startup,
)

# Not the value make_entry stores, so a test that passes cannot have read it
# from anywhere but the options.
TOLERANCE = 0.7


def _entry_saved_by_2_0() -> MockConfigEntry:
    settings = dict(make_entry().data) | {CONF_TOLERANCE: TOLERANCE}
    return MockConfigEntry(
        domain=DOMAIN,
        version=18,
        minor_version=2,
        data={},
        options=settings,
        title="BT Test",
    )


async def _started(hass):
    hass.states.async_set(SENSOR_ID, "19.0", {"unit_of_measurement": "°C"})
    entry = _entry_saved_by_2_0()
    await setup_entry(hass, entry)
    return entry, await wait_for_startup(hass, entry)


async def test_the_thermostat_runs_on_the_settings_in_the_options(hass, fake_trv):
    entry, bt = await _started(hass)

    assert entry.state is ConfigEntryState.LOADED
    assert bt.tolerance == TOLERANCE
    assert list(bt.real_trvs) == [TRV_ID]
    assert entry.options[CONF_TOLERANCE] == TOLERANCE


async def test_the_diagnostics_report_the_settings_in_the_options(hass, fake_trv):
    entry, _bt = await _started(hass)

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert list(diagnostics["thermostat"]) == [TRV_ID]
    assert diagnostics["info"][CONF_TOLERANCE] == TOLERANCE


async def test_the_settings_form_offers_the_settings_in_the_options(hass, fake_trv):
    entry, _bt = await _started(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    defaults = {
        str(marker): marker.default()
        for marker in result["data_schema"].schema
        if callable(getattr(marker, "default", None))
    }
    assert defaults[CONF_TOLERANCE] == TOLERANCE


async def test_saving_the_settings_moves_them_into_the_data(hass, fake_trv):
    """The saved entry reads the same here and tells 2.0 where its settings are."""
    entry, _bt = await _started(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"name": entry.title, CONF_HEATER: [TRV_ID], CONF_SENSOR: SENSOR_ID},
    )
    assert result["step_id"] == "advanced", result
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    await hass.async_block_till_done()

    assert entry.options == {}
    assert entry.data[CONF_TOLERANCE] == TOLERANCE
    assert [trv["trv"] for trv in entry.data[CONF_HEATER]] == [TRV_ID]
    bt = await wait_for_startup(hass, entry)
    assert bt.tolerance == TOLERANCE
