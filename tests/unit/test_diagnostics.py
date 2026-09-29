"""Tests for the config-entry diagnostics."""

import copy
from unittest.mock import MagicMock, Mock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.better_thermostat import DOMAIN
from custom_components.better_thermostat.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.better_thermostat.utils.const import CONF_HEATER, CONF_SENSOR


def _hass():
    hass = MagicMock()
    trv_state = Mock()
    trv_state.name = "TRV"
    trv_state.state = "heat"
    trv_state.attributes = {"temperature": 21.0}
    hass.states.get.return_value = trv_state
    return hass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("integration", "expected_adapter"), [("mqtt", "mqtt"), (None, "unknown")]
)
async def test_diagnostics_leave_the_entry_data_untouched(
    integration, expected_adapter
):
    """A diagnostics download leaves the stored entry configuration as it was.

    The TRV dicts inside ``entry.data`` are the stored configuration; a key
    added to them here would be persisted with the next entry update.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="entry-1",
        data={
            CONF_HEATER: [
                {
                    "trv": "climate.trv",
                    "integration": integration,
                    "advanced": {"calibration": 0},
                    "model": "TRVZB",
                }
            ],
            CONF_SENSOR: "sensor.room",
        },
    )
    before = copy.deepcopy(dict(entry.data))

    diagnostics = await async_get_config_entry_diagnostics(_hass(), entry)

    assert dict(entry.data) == before
    assert "adapter" not in entry.data[CONF_HEATER][0]
    assert diagnostics["thermostat"]["climate.trv"]["bt_adapter"] == expected_adapter
