"""Tests for the config-entry diagnostics, including the flight recorder."""

from unittest.mock import MagicMock, Mock

import pytest

from custom_components.better_thermostat.core.decide import decide, running_kernel_state
from custom_components.better_thermostat.core.recorder import FlightRecorder
from custom_components.better_thermostat.core.snapshot import (
    HvacMode,
    TrvReported,
    WorldSnapshot,
)
from custom_components.better_thermostat.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.better_thermostat.utils.const import CONF_HEATER, CONF_SENSOR


def _snapshot() -> WorldSnapshot:
    from datetime import UTC, datetime

    return WorldSnapshot(
        now=datetime(2026, 1, 10, 7, 0, tzinfo=UTC),
        now_monotonic=1000.0,
        target_temp=21.0,
        hvac_mode=HvacMode.HEAT,
        room_temp=19.0,
        call_for_heat=True,
        trvs={
            "climate.trv": TrvReported(
                entity_id="climate.trv", available=True, current_temp=20.0
            )
        },
    )


def _config_entry():
    entry = MagicMock()
    entry.entry_id = "entry-1"
    entry.data = {
        CONF_HEATER: [
            {
                "trv": "climate.trv",
                "integration": "mqtt",
                "advanced": {"calibration": 0},
                "model": "TRVZB",
            }
        ],
        CONF_SENSOR: "sensor.room",
    }
    return entry


def _hass(bt=None):
    hass = MagicMock()
    trv_state = Mock()
    trv_state.name = "TRV"
    trv_state.state = "heat"
    trv_state.attributes = {"temperature": 21.0}
    hass.states.get.return_value = trv_state
    hass.data = {"better_thermostat": {"entry-1": {"climate": bt}}}
    return hass


@pytest.mark.asyncio
async def test_diagnostics_contains_the_basic_sections():
    """The download carries config info, TRV state, and the sensors."""
    diagnostics = await async_get_config_entry_diagnostics(
        _hass(bt=None), _config_entry()
    )
    assert "info" in diagnostics
    assert CONF_HEATER not in diagnostics["info"]
    assert diagnostics["thermostat"]["climate.trv"]["model"] == "TRVZB"
    assert diagnostics["thermostat"]["climate.trv"]["bt_integration"] == "mqtt"
    assert "external_temperature_sensor" in diagnostics
    assert "window_sensor" in diagnostics


@pytest.mark.asyncio
async def test_diagnostics_exports_the_flight_recorder():
    """With a live entity, the recorder buffer lands in the download."""
    recorder = FlightRecorder()
    desired, _ = decide(_snapshot(), running_kernel_state())
    recorder.record(_snapshot(), running_kernel_state(), desired)

    bt = MagicMock()
    bt.flight_recorder = recorder

    diagnostics = await async_get_config_entry_diagnostics(_hass(bt), _config_entry())
    exported = diagnostics["flight_recorder"]
    assert len(exported) == 1
    assert exported[0]["snapshot"]["trvs"]["climate.trv"]["current_temp"] == 20.0
    assert exported[0]["desired"]["call_for_heat"] is True


@pytest.mark.asyncio
async def test_diagnostics_without_entity_has_no_recorder_section():
    """Without a climate entity (e.g. before setup) the key is absent."""
    hass = _hass(bt=None)
    hass.data = {}
    diagnostics = await async_get_config_entry_diagnostics(hass, _config_entry())
    assert "flight_recorder" not in diagnostics


@pytest.mark.asyncio
async def test_diagnostics_skips_unknown_trvs():
    """A TRV without a hass state is left out of the thermostat section."""
    hass = _hass(bt=None)
    hass.states.get.return_value = None
    diagnostics = await async_get_config_entry_diagnostics(hass, _config_entry())
    assert diagnostics["thermostat"] == {}


@pytest.mark.asyncio
async def test_missing_integration_falls_back_to_unknown_adapter():
    """A TRV without integration reports adapter 'unknown'."""
    entry = _config_entry()
    entry.data[CONF_HEATER][0]["integration"] = None
    diagnostics = await async_get_config_entry_diagnostics(_hass(None), entry)
    assert diagnostics["thermostat"]["climate.trv"]["bt_adapter"] == "unknown"


@pytest.mark.asyncio
async def test_window_sensor_state_is_included_when_configured():
    """With a window sensor configured, its state lands in the download."""
    from custom_components.better_thermostat.utils.const import CONF_SENSOR_WINDOW

    entry = _config_entry()
    entry.data[CONF_SENSOR_WINDOW] = "binary_sensor.window"
    hass = _hass(None)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["window_sensor"] is hass.states.get.return_value


@pytest.mark.asyncio
async def test_window_sensor_lookup_error_is_swallowed():
    """A failing window state lookup leaves the placeholder in place."""
    from custom_components.better_thermostat.utils.const import CONF_SENSOR_WINDOW

    entry = _config_entry()
    entry.data[CONF_SENSOR_WINDOW] = "binary_sensor.window"
    hass = _hass(None)
    trv_state = hass.states.get.return_value

    def _get(entity_id):
        if entity_id == "binary_sensor.window":
            raise KeyError(entity_id)
        return trv_state

    hass.states.get.side_effect = _get
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["window_sensor"] == "-"
