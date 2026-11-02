"""Whether a TRV regulates on a room sensor bound to it.

Home Assistant's Matter integration publishes a thermostat's RemoteSensing
attribute as a diagnostic binary sensor on the thermostat's device. It is on
while the thermostat takes its LocalTemperature from a remote sensor.
"""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
import pytest

from custom_components.better_thermostat.adapters import base, delegate, generic
from custom_components.better_thermostat.model_fixes import default as default_quirk
from custom_components.better_thermostat.utils import helpers
from custom_components.better_thermostat.utils.const import CalibrationOutput
from custom_components.better_thermostat.utils.helpers import (
    REMOTE_SENSING_LOCAL_TEMPERATURE_KEY,
    defers_to_bound_sensor,
    regulates_on_bound_sensor,
    remote_sensing_entity,
)
from tests.factories import (
    ThermostatStandIn,
    make_entity_registry,
    make_registry_entry,
    trv_record,
)

TRV_ID = "climate.eve_thermo"
REMOTE_SENSING_ID = "binary_sensor.eve_thermo_remote_sensing"
TRV_DEVICE = "device_eve"


def _climate_registry_entry(device_id: str | None = TRV_DEVICE) -> er.RegistryEntry:
    return make_registry_entry(TRV_ID, platform="matter", device_id=device_id)


def _remote_sensing_entry(
    *,
    platform: str = "matter",
    device_id: str = TRV_DEVICE,
    translation_key: str = REMOTE_SENSING_LOCAL_TEMPERATURE_KEY,
    disabled_by: er.RegistryEntryDisabler | None = None,
    entity_id: str = REMOTE_SENSING_ID,
) -> er.RegistryEntry:
    return make_registry_entry(
        entity_id,
        platform=platform,
        device_id=device_id,
        translation_key=translation_key,
        disabled_by=disabled_by,
    )


@pytest.fixture
def world() -> Generator[tuple[MagicMock, list[er.RegistryEntry], dict[str, State]]]:
    """Return a Home Assistant whose registry and states the test fills in."""
    entries: list[er.RegistryEntry] = []
    states: dict[str, State] = {}
    hass = MagicMock()
    hass.states.get.side_effect = states.get
    with patch.object(
        helpers.er, "async_get", side_effect=lambda _: make_entity_registry(*entries)
    ):
        yield hass, entries, states


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (STATE_ON, True),
        (STATE_OFF, False),
        (STATE_UNAVAILABLE, None),
        (STATE_UNKNOWN, None),
        (None, None),
    ],
)
def test_the_remote_sensing_sensor_on_the_trv_device_decides(world, state, expected):
    """The TRV regulates on a bound sensor while the sensor on its device is on.

    A sensor that reports no state tells neither way.
    """
    hass, entries, states = world
    entries.extend([_climate_registry_entry(), _remote_sensing_entry()])
    if state is not None:
        states[REMOTE_SENSING_ID] = State(REMOTE_SENSING_ID, state)

    assert regulates_on_bound_sensor(hass, TRV_ID) is expected
    assert defers_to_bound_sensor(hass, TRV_ID) is (expected is not False)


@pytest.mark.parametrize(
    "sibling",
    [
        pytest.param(None, id="no-sensor"),
        pytest.param(_remote_sensing_entry(platform="mqtt"), id="other-platform"),
        pytest.param(
            _remote_sensing_entry(translation_key="other_key"), id="other-key"
        ),
        pytest.param(
            _remote_sensing_entry(device_id="device_other"), id="other-device"
        ),
        pytest.param(
            _remote_sensing_entry(disabled_by=er.RegistryEntryDisabler.USER),
            id="disabled",
        ),
        pytest.param(
            make_registry_entry(
                "sensor.eve_thermo_remote_sensing",
                platform="matter",
                device_id=TRV_DEVICE,
                translation_key=REMOTE_SENSING_LOCAL_TEMPERATURE_KEY,
            ),
            id="other-domain",
        ),
    ],
)
def test_a_trv_without_the_remote_sensing_sensor_regulates_on_its_own(world, sibling):
    """Only the enabled Matter binary sensor on the TRV's own device counts.

    Without it, the TRV regulates on its own sensor even while some other
    entity that resembles it is on.
    """
    hass, entries, states = world
    entries.append(_climate_registry_entry())
    if sibling is not None:
        entries.append(sibling)
        states[sibling.entity_id] = State(sibling.entity_id, STATE_ON)

    assert remote_sensing_entity(hass, TRV_ID) is None
    assert regulates_on_bound_sensor(hass, TRV_ID) is False
    assert defers_to_bound_sensor(hass, TRV_ID) is False


@pytest.mark.parametrize(
    "trv_entries",
    [pytest.param([], id="unregistered"), pytest.param([None], id="no-device")],
)
def test_a_trv_without_a_device_regulates_on_its_own(world, trv_entries):
    """A TRV that belongs to no device has no remote-sensing sensor."""
    hass, entries, states = world
    entries.extend(_climate_registry_entry(device_id=None) for _ in trv_entries)
    states[REMOTE_SENSING_ID] = State(REMOTE_SENSING_ID, STATE_ON)

    assert regulates_on_bound_sensor(hass, TRV_ID) is False


CALIBRATION_ID = "number.eve_thermo_temperature_offset"


def _offset_host(remote_sensing: str, calibration_state: str) -> ThermostatStandIn:
    """A running thermostat that adopted the TRV's calibration number."""
    host = ThermostatStandIn()
    host.device_name = "Test BT"
    host.context = None
    host.hass.services.async_call = AsyncMock(return_value=None)
    states = {
        REMOTE_SENSING_ID: State(REMOTE_SENSING_ID, remote_sensing),
        CALIBRATION_ID: State(
            CALIBRATION_ID, calibration_state, {"min": -5, "max": 5, "step": 0.1}
        ),
    }
    host.hass.states.get = states.get
    record = trv_record(
        entity_id=TRV_ID, calibration=CalibrationOutput.LOCAL_BASED, advanced={}
    )
    record.model_quirks = MagicMock(spec=[])
    record.adapter = generic
    record.local_temperature_calibration_entity = CALIBRATION_ID
    host.real_trvs = {TRV_ID: record}
    return host


def _offset_registry() -> MagicMock:
    return make_entity_registry(
        _climate_registry_entry(),
        _remote_sensing_entry(),
        make_registry_entry(CALIBRATION_ID, platform="matter", device_id=TRV_DEVICE),
    )


def _written(host: ThermostatStandIn) -> list[str]:
    return [
        call.args[2]["entity_id"]
        for call in host.hass.services.async_call.await_args_list
    ]


WRITES_THE_OFFSET = [(STATE_ON, False), (STATE_UNAVAILABLE, False), (STATE_OFF, True)]


@pytest.mark.parametrize(("remote_sensing", "written"), WRITES_THE_OFFSET)
@pytest.mark.asyncio
async def test_an_offset_reaches_only_a_trv_known_to_regulate_on_its_own(
    remote_sensing, written
):
    """The offset write answers False and sends nothing to a possibly bound TRV.

    False keeps the caller from waiting on a confirmation that never comes.
    """
    host = _offset_host(remote_sensing, "-1.5")

    with patch.object(helpers.er, "async_get", return_value=_offset_registry()):
        assert await delegate.set_calibration_offset(host, TRV_ID, 2.0) is written

    assert (CALIBRATION_ID in _written(host)) is written


@pytest.mark.parametrize(("remote_sensing", "written"), WRITES_THE_OFFSET)
@pytest.mark.asyncio
async def test_the_startup_zero_reaches_only_a_trv_known_to_regulate_on_its_own(
    remote_sensing, written
):
    """A calibration number that stays unavailable is zeroed on an unbound TRV only."""
    host = _offset_host(remote_sensing, STATE_UNAVAILABLE)

    with (
        patch.object(helpers.er, "async_get", return_value=_offset_registry()),
        patch.object(base.asyncio, "sleep", AsyncMock()),
    ):
        await base.wait_for_calibration_entity_or_timeout(host, TRV_ID, CALIBRATION_ID)

    assert (CALIBRATION_ID in _written(host)) is written


@pytest.mark.parametrize(("remote_sensing", "written"), WRITES_THE_OFFSET)
@pytest.mark.asyncio
async def test_the_initial_reset_reaches_only_a_trv_known_to_regulate_on_its_own(
    remote_sensing, written
):
    """The startup reset of the calibration leaves a possibly bound TRV's offset."""
    host = _offset_host(remote_sensing, "-1.5")
    registry = make_entity_registry(
        _climate_registry_entry(),
        _remote_sensing_entry(),
        make_registry_entry(
            "number.eve_thermo_local_temperature_calibration",
            platform="matter",
            device_id=TRV_DEVICE,
        ),
    )

    with (
        patch.object(helpers.er, "async_get", return_value=registry),
        patch.object(default_quirk.er, "async_get", return_value=registry),
    ):
        await default_quirk.initial_tweak(host, TRV_ID)

    assert (
        "number.eve_thermo_local_temperature_calibration" in _written(host)
    ) is written
