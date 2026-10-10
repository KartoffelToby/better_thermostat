"""Tests for the Shelly adapter.

The adapter offers a valve channel only through the valve number a Shelly
BLU TRV publishes while its own thermostat is switched off, and leaves out
the setpoint such a TRV has no place for.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import State
import pytest

from custom_components.better_thermostat.adapters import shelly
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.const import CalibrationOutput
from custom_components.better_thermostat.utils.entry_schema import TrvAdvanced
from tests.factories import ThermostatStandIn, make_entity_registry, make_registry_entry

ENTITY_ID = "climate.blu_trv"
VALVE_ENTITY = "number.blu_trv_valve_position"
BLU_TRV_VALVE_UID = "aabbccddeeff-blutrv:200-valve_position"
GEN1_VALVE_UID = "aabbccddeeff-device_0-valvePos"


def _registry(valve_uid=BLU_TRV_VALVE_UID, valve_entity=VALVE_ENTITY):
    """A registry holding the TRV's climate entity and its valve number."""
    entries = [make_registry_entry(ENTITY_ID, platform="shelly")]
    if valve_uid is not None:
        entries.append(
            make_registry_entry(
                valve_entity,
                unique_id=valve_uid,
                platform="shelly",
                translation_key="valve_position",
            )
        )
    return make_entity_registry(*entries)


def _thermostat(calibration=None, head_attributes=None):
    """A thermostat whose service calls are recorded, not executed."""
    bt = ThermostatStandIn()
    bt.device_name = "Test BT"
    bt.context = None
    bt.hass = MagicMock()
    bt.hass.services.async_call = AsyncMock()
    states = {
        ENTITY_ID: State(ENTITY_ID, "heat", head_attributes or {}),
        VALVE_ENTITY: State(VALVE_ENTITY, "0", {"min": 0, "max": 100, "step": 1}),
    }
    bt.hass.states.get = states.get
    advanced: TrvAdvanced = {} if calibration is None else {"calibration": calibration}
    bt.real_trvs = {ENTITY_ID: Trv(entity_id=ENTITY_ID, advanced=advanced)}
    return bt


def _registry_patch(registry):
    """Serve ``registry`` to every entity registry lookup."""
    return patch(
        "custom_components.better_thermostat.utils.helpers.er.async_get",
        return_value=registry,
    )


class TestValveCapability:
    """The valve is offered for a BLU TRV valve number and nothing else."""

    @pytest.mark.asyncio
    async def test_a_blu_trv_valve_number_is_offered(self):
        """A writable BLU TRV valve number makes the TRV valve capable."""
        with _registry_patch(_registry()):
            info = await shelly.get_info(_thermostat(), ENTITY_ID)

        assert info["support_valve"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("valve_uid", "valve_entity"),
        [
            (GEN1_VALVE_UID, VALVE_ENTITY),
            (None, VALVE_ENTITY),
            (BLU_TRV_VALVE_UID, "sensor.blu_trv_valve_position"),
        ],
        ids=["gen1-trv", "no-valve-entity", "read-only"],
    )
    async def test_no_other_valve_entity_is_offered(self, valve_uid, valve_entity):
        """A Gen1 valve, a missing one and a read-only one offer nothing."""
        with _registry_patch(_registry(valve_uid, valve_entity)):
            info = await shelly.get_info(_thermostat(), ENTITY_ID)

        assert info["support_valve"] is False

    @pytest.mark.asyncio
    async def test_init_adopts_a_blu_trv_valve(self):
        """The BLU TRV valve number becomes the TRV's valve channel."""
        bt = _thermostat()
        with _registry_patch(_registry()):
            await shelly.init(bt, ENTITY_ID)

        trv = bt.real_trvs[ENTITY_ID]
        assert trv.valve_position_entity == VALVE_ENTITY
        assert trv.valve_position_writable is True

    @pytest.mark.asyncio
    async def test_init_lets_a_gen1_valve_go(self):
        """A Gen1 valve number found by discovery is not kept."""
        bt = _thermostat()
        with _registry_patch(_registry(GEN1_VALVE_UID)):
            await shelly.init(bt, ENTITY_ID)

        trv = bt.real_trvs[ENTITY_ID]
        assert trv.valve_position_entity is None
        assert trv.valve_position_writable is None


class TestSetpoint:
    """A setpoint goes out unless the TRV runs on its valve without one."""

    @pytest.mark.asyncio
    async def test_no_setpoint_while_the_valve_runs_without_a_target(self):
        """Direct valve control on a TRV that reports no target skips the write."""
        bt = _thermostat(calibration=CalibrationOutput.DIRECT_VALVE_BASED)
        with _registry_patch(_registry()):
            await shelly.init(bt, ENTITY_ID)

        await shelly.set_temperature(bt, ENTITY_ID, 21.0)

        bt.hass.services.async_call.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("calibration", "head_attributes", "valve_entity"),
        [
            (CalibrationOutput.DIRECT_VALVE_BASED, {"temperature": 20.0}, VALVE_ENTITY),
            (CalibrationOutput.TARGET_TEMP_BASED, {}, VALVE_ENTITY),
            (CalibrationOutput.DIRECT_VALVE_BASED, {}, None),
            (CalibrationOutput.DIRECT_VALVE_BASED, {}, "sensor.blu_trv_valve_position"),
        ],
        ids=[
            "target-reported",
            "not-direct-valve",
            "no-valve-channel",
            "read-only-valve",
        ],
    )
    async def test_the_setpoint_goes_out_otherwise(
        self, calibration, head_attributes, valve_entity
    ):
        """Any one condition missing sends the setpoint as the generic adapter does."""
        bt = _thermostat(calibration=calibration, head_attributes=head_attributes)
        if valve_entity is not None:
            with _registry_patch(_registry(valve_entity=valve_entity)):
                await shelly.init(bt, ENTITY_ID)

        await shelly.set_temperature(bt, ENTITY_ID, 21.0)

        bt.hass.services.async_call.assert_awaited_once()
        domain, service, data = bt.hass.services.async_call.await_args.args[:3]
        assert (domain, service) == ("climate", "set_temperature")
        assert data["entity_id"] == ENTITY_ID


class TestValveWrite:
    """A valve position lands on the BLU TRV's valve number."""

    @pytest.mark.asyncio
    async def test_the_position_goes_to_the_valve_number(self):
        """The percentage is written to the adopted valve entity."""
        bt = _thermostat(calibration=CalibrationOutput.DIRECT_VALVE_BASED)
        with _registry_patch(_registry()):
            await shelly.init(bt, ENTITY_ID)

        await shelly.set_valve(bt, ENTITY_ID, 33)

        bt.hass.services.async_call.assert_awaited_once()
        assert bt.hass.services.async_call.await_args.args[:3] == (
            "number",
            "set_value",
            {"entity_id": VALVE_ENTITY, "value": 33.0},
        )
