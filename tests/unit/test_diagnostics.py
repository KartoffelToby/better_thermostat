"""Tests for the config-entry diagnostics, including the flight recorder."""

import copy
from unittest.mock import MagicMock, patch

from homeassistant.core import Context, State
from homeassistant.helpers import device_registry as dr
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.better_thermostat import DOMAIN, BetterThermostatData
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
from custom_components.better_thermostat.utils.const import (
    CONF_TEMPERATURE_SENSOR,
    CONF_THERMOSTAT,
)
from tests.factories import ThermostatStandIn, make_entity_registry, make_registry_entry

_DIAGNOSTICS = "custom_components.better_thermostat.diagnostics"


def _snapshot() -> WorldSnapshot:
    from datetime import UTC, datetime

    return WorldSnapshot(
        now=datetime(2026, 1, 10, 7, 0, tzinfo=UTC),
        now_monotonic=1000.0,
        heat_target_temperature=21.0,
        hvac_mode=HvacMode.HEAT,
        room_temperature=19.0,
        call_for_heat=True,
        trvs={
            "climate.trv": TrvReported(
                entity_id="climate.trv", available=True, current_temperature=20.0
            )
        },
    )


def _config_entry(bt=None, *, loaded=True):
    entry = MagicMock()
    entry.entry_id = "entry-1"
    if loaded:
        entry.runtime_data = BetterThermostatData(climate=bt)
    else:
        # Home Assistant drops the runtime data when the entry unloads.
        del entry.runtime_data
    entry.data = {
        CONF_THERMOSTAT: [
            {
                "trv": "climate.trv",
                "integration": "mqtt",
                "advanced": {"calibration": 0},
                "model": "TRVZB",
            }
        ],
        CONF_TEMPERATURE_SENSOR: "sensor.room",
    }
    return entry


@pytest.fixture
def _empty_registries():
    """Registries that know no entity: the mocked hass carries none."""
    devices = MagicMock(spec=dr.DeviceRegistry)
    devices.async_get.return_value = None
    with (
        patch(f"{_DIAGNOSTICS}.er.async_get", return_value=make_entity_registry()),
        patch(f"{_DIAGNOSTICS}.dr.async_get", return_value=devices),
    ):
        yield


def _hass():
    hass = MagicMock()
    trv_state = State(
        "climate.trv", "heat", {"temperature": 21.0, "friendly_name": "TRV"}
    )
    hass.states.get.return_value = trv_state
    return hass


@pytest.mark.asyncio
@pytest.mark.usefixtures("_empty_registries")
async def test_diagnostics_contains_the_basic_sections():
    """The download carries config info, TRV state, and the sensors."""
    diagnostics = await async_get_config_entry_diagnostics(_hass(), _config_entry())
    assert "info" in diagnostics
    assert CONF_THERMOSTAT not in diagnostics["info"]
    assert diagnostics["thermostat"]["climate.trv"]["model"] == "TRVZB"
    assert diagnostics["thermostat"]["climate.trv"]["bt_integration"] == "mqtt"
    assert "external_temperature_sensor" in diagnostics
    assert "window_sensor" in diagnostics


@pytest.mark.asyncio
@pytest.mark.usefixtures("_empty_registries")
async def test_diagnostics_exports_the_flight_recorder():
    """With a live entity, the recorder buffer lands in the download."""
    recorder = FlightRecorder()
    desired, _ = decide(_snapshot(), running_kernel_state())
    recorder.record(_snapshot(), running_kernel_state(), desired)

    bt = ThermostatStandIn()
    bt.flight_recorder = recorder

    diagnostics = await async_get_config_entry_diagnostics(_hass(), _config_entry(bt))
    exported = diagnostics["flight_recorder"]
    assert len(exported) == 1
    assert exported[0]["snapshot"]["trvs"]["climate.trv"]["current_temperature"] == 20.0
    assert exported[0]["desired"]["call_for_heat"] is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("_empty_registries")
async def test_diagnostics_without_entity_has_no_recorder_section():
    """An entry that is not loaded has no climate entity and no recorder."""
    diagnostics = await async_get_config_entry_diagnostics(
        _hass(), _config_entry(loaded=False)
    )
    assert "flight_recorder" not in diagnostics


@pytest.mark.asyncio
@pytest.mark.usefixtures("_empty_registries")
async def test_diagnostics_skips_unknown_trvs():
    """A TRV without a hass state is left out of the thermostat section."""
    hass = _hass()
    hass.states.get.return_value = None
    diagnostics = await async_get_config_entry_diagnostics(hass, _config_entry())
    assert diagnostics["thermostat"] == {}


@pytest.mark.asyncio
@pytest.mark.usefixtures("_empty_registries")
async def test_missing_integration_falls_back_to_unknown_adapter():
    """A TRV without integration reports adapter 'unknown'."""
    entry = _config_entry()
    entry.data[CONF_THERMOSTAT][0]["integration"] = None
    diagnostics = await async_get_config_entry_diagnostics(_hass(), entry)
    assert diagnostics["thermostat"]["climate.trv"]["bt_adapter"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_empty_registries")
async def test_window_sensor_state_is_included_when_configured():
    """With a window sensor configured, its state lands in the download."""
    from custom_components.better_thermostat.utils.const import CONF_WINDOW_SENSORS

    entry = _config_entry()
    entry.data[CONF_WINDOW_SENSORS] = "binary_sensor.window"
    hass = _hass()
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    expected = dict(hass.states.get.return_value.as_dict())
    del expected["context"]
    assert diagnostics["window_sensor"] == expected


@pytest.mark.asyncio
@pytest.mark.usefixtures("_empty_registries")
async def test_no_state_in_the_download_carries_its_context():
    """A user-triggered change leaves no user or logbook id in the download."""
    from custom_components.better_thermostat.utils.const import CONF_WINDOW_SENSORS

    context = Context(user_id="user-4711", parent_id="parent-4711", id="context-4711")
    bt = ThermostatStandIn()
    bt.entity_id = "climate.bt"
    entry = _config_entry(bt)
    entry.data[CONF_WINDOW_SENSORS] = "binary_sensor.window"
    hass = _hass()
    hass.states.get.return_value = State(
        "climate.trv", "heat", {"temperature": 21.0}, context=context
    )

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    for section in ("external_temperature_sensor", "window_sensor", "climate"):
        assert "context" not in diagnostics[section]
    assert "4711" not in repr(diagnostics)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_empty_registries")
async def test_an_unconfigured_window_sensor_reads_none():
    """Without a window sensor the key is None, like every other sensor."""
    diagnostics = await async_get_config_entry_diagnostics(_hass(), _config_entry())
    assert diagnostics["window_sensor"] is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("_empty_registries")
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
            CONF_THERMOSTAT: [
                {
                    "trv": "climate.trv",
                    "integration": integration,
                    "advanced": {"calibration": 0},
                    "model": "TRVZB",
                }
            ],
            CONF_TEMPERATURE_SENSOR: "sensor.room",
        },
    )
    before = copy.deepcopy(dict(entry.data))

    diagnostics = await async_get_config_entry_diagnostics(_hass(), entry)

    assert dict(entry.data) == before
    assert "adapter" not in entry.data[CONF_THERMOSTAT][0]
    assert diagnostics["thermostat"]["climate.trv"]["bt_adapter"] == expected_adapter


@pytest.mark.asyncio
async def test_an_entry_without_a_room_sensor_still_downloads(hass):
    """An entry whose room sensor was cleared yields a download, not an error."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_THERMOSTAT: [
                {
                    "trv": "climate.trv",
                    "integration": "mqtt",
                    "advanced": {},
                    "model": "TRVZB",
                }
            ],
            CONF_TEMPERATURE_SENSOR: None,
        },
    )
    hass.states.async_set("climate.trv", "heat")

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["external_temperature_sensor"] is None
    assert "climate.trv" in diagnostics["thermostat"]


@pytest.mark.asyncio
async def test_a_device_bundle_without_integration_or_model_still_downloads(hass):
    """A bundle that lacks the detected integration and model reports them unknown."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_THERMOSTAT: [{"trv": "climate.trv", "advanced": {}}],
            CONF_TEMPERATURE_SENSOR: "sensor.room",
        },
    )
    hass.states.async_set("climate.trv", "heat")

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    trv = diagnostics["thermostat"]["climate.trv"]
    assert trv["bt_adapter"] == "unknown"
    assert trv["bt_integration"] is None
    assert trv["model"] is None


def _devices(device=None):
    """A device registry answering ``device`` for every id."""
    devices = MagicMock(spec=dr.DeviceRegistry)
    devices.async_get.return_value = device
    return devices


@pytest.mark.asyncio
@pytest.mark.parametrize("device_id", [None, "gone"])
async def test_a_valve_without_a_device_reports_its_integration_only(device_id):
    """No device, or none the registry returns, leaves the integration alone.

    The registry answers a main device only; a child or composite id reads as
    none, like an entity registered without a device.
    """
    entities = make_entity_registry(
        make_registry_entry("climate.trv", platform="zha", device_id=device_id)
    )
    with (
        patch(f"{_DIAGNOSTICS}.er.async_get", return_value=entities),
        patch(f"{_DIAGNOSTICS}.dr.async_get", return_value=_devices(None)),
    ):
        diagnostics = await async_get_config_entry_diagnostics(_hass(), _config_entry())
    assert diagnostics["thermostat"]["climate.trv"]["device"] == {"integration": "zha"}


@pytest.mark.asyncio
@pytest.mark.usefixtures("_empty_registries")
@pytest.mark.parametrize("key", ["ip", "ip_address"])
async def test_no_network_address_reaches_the_download(key):
    """An address a valve publishes under either key is redacted."""
    hass = _hass()
    hass.states.get.return_value = State(
        "climate.trv", "heat", {"temperature": 21.0, key: "192.0.2.17"}
    )

    diagnostics = await async_get_config_entry_diagnostics(hass, _config_entry())

    assert diagnostics["thermostat"]["climate.trv"]["attributes"][key] == "**REDACTED**"
    assert "192.0.2.17" not in repr(diagnostics)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored",
    [
        pytest.param({}, id="no-thermostat-key"),
        pytest.param({CONF_THERMOSTAT: None}, id="thermostat-none"),
        pytest.param({CONF_THERMOSTAT: 3}, id="thermostat-number"),
        pytest.param({CONF_THERMOSTAT: [{"integration": "mqtt"}]}, id="no-trv-key"),
        pytest.param({CONF_THERMOSTAT: [{"trv": 7}]}, id="trv-not-a-string"),
        pytest.param({CONF_THERMOSTAT: ["climate.trv"]}, id="element-a-string"),
    ],
)
async def test_an_entry_whose_thermostat_list_does_not_parse_still_downloads(
    hass, stored
):
    """Settings setup refuses still download, with every stored key in info.

    An entry whose settings fail to parse is the one support asks a
    download for, so the thermostat section is left empty instead of
    raising, and the other keys are dumped as stored.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, data={**stored, CONF_TEMPERATURE_SENSOR: "sensor.room"}
    )
    hass.states.async_set("climate.trv", "heat")

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["thermostat"] == {}
    assert diagnostics["info"] == {CONF_TEMPERATURE_SENSOR: "sensor.room"}


@pytest.mark.asyncio
async def test_unknown_keys_stay_in_the_info_section(hass):
    """Keys no reader knows are part of the download, unchanged."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_THERMOSTAT: [
                {"trv": "climate.trv", "integration": "mqtt", "legacy": [1, 2]}
            ],
            "balance_mode": 0,
            "fix_calibration": "yes",
        },
    )
    hass.states.async_set("climate.trv", "heat")

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["info"] == {"balance_mode": 0, "fix_calibration": "yes"}
    assert diagnostics["thermostat"]["climate.trv"]["bt_integration"] == "mqtt"
