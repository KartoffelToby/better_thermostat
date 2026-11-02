"""A TRV that regulates on a sensor bound to it keeps its own control loop.

A Matter thermostat bound to a room temperature sensor takes its
LocalTemperature from that sensor. Home Assistant's Matter integration
publishes this as a diagnostic binary sensor on the thermostat's device.
Its local temperature calibration then shifts the room temperature the
thermostat controls on, and a calibration on top of it would be a second
control loop.
"""

from dataclasses import replace
from unittest.mock import patch

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
import pytest

from custom_components.better_thermostat.utils.const import (
    CONF_CALIBRATION,
    CONF_CALIBRATION_MODE,
    CalibrationMode,
    CalibrationOutput,
)
from custom_components.better_thermostat.utils.helpers import (
    REMOTE_SENSING_LOCAL_TEMPERATURE_KEY,
)

from .conftest import (
    BT_ENTITY,
    DOMAIN,
    SENSOR_ID,
    WRITE_BUDGET,
    form_default,
    make_entry,
    profile_id,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import TRV_ID, DeviceProfile, OffsetChannel

REMOTE_SENSING_ID = "binary_sensor.fake_trv_remote_sensing"

# The user's offset on the device. It differs from zero, which a startup
# reset writes, and from what a calibration would compute for the room.
USER_OFFSET = -1.5

MATTER_OFFSET_TRV = DeviceProfile(
    name="matter_offset_trv",
    integration="matter",
    calibration=CalibrationOutput.LOCAL_BASED,
    has_device_registry_entry=True,
    current_temperature=19.0,
    offset_channel=OffsetChannel.NUMBER_ENTITY,
    offset_translation_key="temperature_offset",
)
"""A Matter thermostat whose calibration is the offset number on its device."""

MATTER_SETPOINT_TRV = replace(
    MATTER_OFFSET_TRV,
    name="matter_setpoint_trv",
    calibration=CalibrationOutput.TARGET_TEMP_BASED,
)
"""The same thermostat, calibrated through its setpoint."""


@pytest.fixture(autouse=True)
def immediate_writes():
    """Write every change at once instead of spacing writes per channel."""
    with patch(WRITE_BUDGET, 0.0):
        yield


def bind_remote_sensing(hass, state: str) -> None:
    """Put the Matter remote-sensing binary sensor on the TRV's device."""
    registry = er.async_get(hass)
    climate_entry = registry.async_get(TRV_ID)
    assert climate_entry is not None and climate_entry.device_id is not None
    registry.async_get_or_create(
        "binary_sensor",
        "matter",
        "fake_trv_remote_sensing_local_temperature",
        device_id=climate_entry.device_id,
        translation_key=REMOTE_SENSING_LOCAL_TEMPERATURE_KEY,
        suggested_object_id="fake_trv_remote_sensing",
    )
    hass.states.async_set(REMOTE_SENSING_ID, state)


def set_user_offset(fake_trv) -> None:
    """Put the user's offset on the device before Better Thermostat starts."""
    fake_trv.offset_number._attr_native_value = USER_OFFSET
    fake_trv.offset_number.async_write_ha_state()


async def _set_target(hass, target: float) -> None:
    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": BT_ENTITY, "temperature": target, "hvac_mode": "heat"},
        blocking=True,
    )


@pytest.mark.parametrize("fake_trv", [MATTER_OFFSET_TRV], indirect=True, ids=profile_id)
@pytest.mark.parametrize("remote_sensing", [STATE_ON, STATE_UNAVAILABLE])
async def test_the_offset_of_a_bound_trv_stays_as_the_user_set_it(
    hass, fake_trv, remote_sensing
):
    """Better Thermostat writes no offset to a TRV that may regulate on a bound sensor.

    That covers the reset on startup and the calibration afterwards, while
    the remote-sensing sensor says the TRV regulates on a bound sensor and
    while it reports nothing that rules this out.
    """
    bind_remote_sensing(hass, remote_sensing)
    set_user_offset(fake_trv)
    set_room_sensor(hass, 21.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)

    await _set_target(hass, 22.0)
    assert await wait_for(
        hass,
        lambda: (
            fake_trv.set_temperature_calls
            and fake_trv.set_temperature_calls[-1] == pytest.approx(22.0)
        ),
    ), fake_trv.set_temperature_calls

    assert fake_trv.offset_number.set_value_calls == []
    assert float(
        hass.states.get(fake_trv.offset_number.entity_id).state
    ) == pytest.approx(USER_OFFSET)


@pytest.mark.parametrize("fake_trv", [MATTER_OFFSET_TRV], indirect=True, ids=profile_id)
async def test_the_offset_of_a_trv_on_its_own_sensor_is_calibrated(hass, fake_trv):
    """A TRV whose remote-sensing sensor is off is calibrated by offset as usual."""
    bind_remote_sensing(hass, STATE_OFF)
    set_user_offset(fake_trv)
    set_room_sensor(hass, 21.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)

    assert await wait_for(
        hass,
        lambda: (
            fake_trv.offset_number.set_value_calls
            and fake_trv.offset_number.set_value_calls[-1] == pytest.approx(2.0)
        ),
    ), fake_trv.offset_number.set_value_calls
    assert fake_trv.offset_number.set_value_calls[0] == pytest.approx(0.0)


@pytest.mark.parametrize(
    "fake_trv", [MATTER_SETPOINT_TRV], indirect=True, ids=profile_id
)
@pytest.mark.parametrize(
    ("remote_sensing", "receives_target"), [(STATE_ON, True), (STATE_OFF, False)]
)
async def test_a_bound_trv_receives_the_target_temperature_unchanged(
    hass, fake_trv, remote_sensing, receives_target
):
    """A TRV that regulates on a bound sensor gets the target, not a calibrated setpoint.

    Its entry still names a calibration mode, which applies to a TRV on its
    own sensor only.
    """
    bind_remote_sensing(hass, remote_sensing)
    set_room_sensor(hass, 21.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)
    baseline = len(fake_trv.set_temperature_calls)

    await _set_target(hass, 22.0)
    assert await wait_for(
        hass, lambda: len(fake_trv.set_temperature_calls) > baseline
    ), fake_trv.set_temperature_calls

    written = fake_trv.set_temperature_calls[-1]
    assert (written == pytest.approx(22.0)) is receives_target, written


async def _flow_at_the_advanced_step(hass, fake_trv, options: bool):
    if options:
        entry = make_entry(fake_trv.profile)
        await setup_entry(hass, entry)
        await wait_for_startup(hass, entry)
        manager = hass.config_entries.options
        result = await manager.async_init(entry.entry_id)
    else:
        manager = hass.config_entries.flow
        result = await manager.async_init(DOMAIN, context={"source": "user"})
    result = await manager.async_configure(
        result["flow_id"],
        {"name": "Bound room", "thermostat": [TRV_ID], "temperature_sensor": SENSOR_ID},
    )
    assert result["step_id"] == "advanced"
    return manager, result


@pytest.mark.parametrize("fake_trv", [MATTER_OFFSET_TRV], indirect=True, ids=profile_id)
@pytest.mark.parametrize("options", [False, True], ids=["create", "options"])
async def test_the_flow_accepts_only_no_calibration_for_a_bound_trv(
    hass, fake_trv, options
):
    """A TRV that regulates on a bound sensor is configured without calibration.

    The advanced step offers no calibration on the setpoint output, refuses
    any calibration mode, and accepts no calibration.
    """
    bind_remote_sensing(hass, STATE_ON)
    set_room_sensor(hass, 21.0)
    manager, result = await _flow_at_the_advanced_step(hass, fake_trv, options)

    assert form_default(result, CONF_CALIBRATION_MODE) == (
        CalibrationMode.NO_CALIBRATION
    )
    if not options:
        assert form_default(result, CONF_CALIBRATION) == (
            CalibrationOutput.TARGET_TEMP_BASED
        )

    result = await manager.async_configure(
        result["flow_id"],
        {
            CONF_CALIBRATION: CalibrationOutput.TARGET_TEMP_BASED,
            CONF_CALIBRATION_MODE: CalibrationMode.MPC_V2_CALIBRATION,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "advanced"
    assert result["errors"] == {CONF_CALIBRATION_MODE: "bound_sensor_calibration_mode"}

    result = await manager.async_configure(
        result["flow_id"],
        {
            CONF_CALIBRATION: CalibrationOutput.TARGET_TEMP_BASED,
            CONF_CALIBRATION_MODE: CalibrationMode.NO_CALIBRATION,
        },
    )
    assert not result.get("errors")
    assert result["step_id" if not options else "type"] == (
        "confirm" if not options else FlowResultType.CREATE_ENTRY
    )


@pytest.mark.parametrize("fake_trv", [MATTER_OFFSET_TRV], indirect=True, ids=profile_id)
@pytest.mark.parametrize("remote_sensing", [STATE_OFF, STATE_UNAVAILABLE])
async def test_the_flow_keeps_every_calibration_for_a_trv_not_known_to_be_bound(
    hass, fake_trv, remote_sensing
):
    """A TRV not known to regulate on a bound sensor keeps the full choice."""
    bind_remote_sensing(hass, remote_sensing)
    set_room_sensor(hass, 21.0)
    manager, result = await _flow_at_the_advanced_step(hass, fake_trv, False)

    assert form_default(result, CONF_CALIBRATION) == CalibrationOutput.LOCAL_BASED
    result = await manager.async_configure(
        result["flow_id"],
        {
            CONF_CALIBRATION: CalibrationOutput.LOCAL_BASED,
            CONF_CALIBRATION_MODE: CalibrationMode.MPC_V2_CALIBRATION,
        },
    )
    assert not result.get("errors")
    assert result["step_id"] == "confirm"
