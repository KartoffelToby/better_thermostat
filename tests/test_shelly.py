"""Tests for the Shelly Better Thermostat adapter."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.better_thermostat.adapters import shelly
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.const import CalibrationType

ENTITY_ID = "climate.test_shelly_trv"
VALVE_ENTITY_ID = "number.test_shelly_trv_valve"
RENAMED_VALVE_ENTITY_ID = "number.test_shelly_trv_valve_renamed"


def _valve_discovery(entity_id=VALVE_ENTITY_ID, *, writable=True, domain="number"):
    return {
        "entity_id": entity_id,
        "writable": writable,
        "domain": domain,
        "reason": "valve_position",
    }


def _state(value="0", *, minimum=0, maximum=100, step=1, temperature=None):
    return SimpleNamespace(
        state=str(value),
        attributes={
            "min": minimum,
            "max": maximum,
            "step": step,
            "temperature": temperature,
        },
    )


def _bt(*, direct=True, writable=True, valve_entity=VALVE_ENTITY_ID):
    trv = Trv(entity_id=ENTITY_ID)
    trv.advanced = {
        "calibration": (
            CalibrationType.DIRECT_VALVE_BASED
            if direct
            else CalibrationType.TARGET_TEMP_BASED
        )
    }
    trv.valve_position_entity = valve_entity
    trv.valve_position_writable = writable

    bt = SimpleNamespace()
    bt.device_name = "Test BT"
    bt.context = object()
    bt.real_trvs = {ENTITY_ID: trv}
    bt.hass = SimpleNamespace()
    bt.hass.states = MagicMock()
    bt.hass.services = SimpleNamespace(async_call=AsyncMock())
    return bt, trv


def test_writable_native_number_is_supported():
    """A writable native Number is a supported Shelly valve actuator."""
    assert shelly._is_writable_number(_valve_discovery()) is True


def test_read_only_valve_is_not_supported():
    """A read-only valve entity must not enable Direct Valve support."""
    assert shelly._is_writable_number(_valve_discovery(writable=False)) is False


def test_input_number_is_not_accepted_as_native_shelly_actuator():
    """Only the native Shelly Number is accepted, not an input_number helper."""
    assert shelly._is_writable_number(_valve_discovery(domain="input_number")) is False


@pytest.mark.asyncio
async def test_get_info_reports_writable_shelly_valve(monkeypatch):
    """A writable Shelly Number enables Direct Valve support."""
    bt, _ = _bt()
    monkeypatch.setattr(
        shelly,
        "generic_get_info",
        AsyncMock(return_value={"support_offset": False, "support_valve": False}),
    )
    monkeypatch.setattr(
        shelly, "find_valve_entity", AsyncMock(return_value=_valve_discovery())
    )

    info = await shelly.get_info(bt, ENTITY_ID)

    assert info == {"support_offset": False, "support_valve": True}


@pytest.mark.asyncio
async def test_get_info_rejects_read_only_shelly_valve(monkeypatch):
    """A read-only valve sensor does not advertise Direct Valve support."""
    bt, _ = _bt()
    monkeypatch.setattr(
        shelly,
        "generic_get_info",
        AsyncMock(return_value={"support_offset": False, "support_valve": False}),
    )
    monkeypatch.setattr(
        shelly,
        "find_valve_entity",
        AsyncMock(return_value=_valve_discovery(writable=False, domain="sensor")),
    )

    info = await shelly.get_info(bt, ENTITY_ID)

    assert info["support_valve"] is False


@pytest.mark.asyncio
async def test_get_info_discovery_failure_reports_no_valve(monkeypatch):
    """Capability discovery failure falls back to generic no-valve support."""
    bt, _ = _bt()
    monkeypatch.setattr(
        shelly,
        "generic_get_info",
        AsyncMock(return_value={"support_offset": False, "support_valve": False}),
    )
    monkeypatch.setattr(
        shelly,
        "find_valve_entity",
        AsyncMock(side_effect=RuntimeError("registry unavailable")),
    )

    info = await shelly.get_info(bt, ENTITY_ID)

    assert info == {"support_offset": False, "support_valve": False}


@pytest.mark.asyncio
async def test_init_stores_discovered_valve_on_real_trv(monkeypatch):
    """Initialization stores discovery on the typed Trv object."""
    bt, trv = _bt(writable=False, valve_entity=None)
    monkeypatch.setattr(
        shelly, "find_valve_entity", AsyncMock(return_value=_valve_discovery())
    )
    generic_init = AsyncMock()
    monkeypatch.setattr(shelly, "generic_init", generic_init)

    await shelly.init(bt, ENTITY_ID)

    assert trv.valve_position_entity == VALVE_ENTITY_ID
    assert trv.valve_position_writable is True
    generic_init.assert_awaited_once_with(bt, ENTITY_ID)


@pytest.mark.asyncio
async def test_init_discovery_failure_keeps_generic_behavior(monkeypatch):
    """A discovery exception disables Direct Valve without breaking init."""
    bt, trv = _bt()
    monkeypatch.setattr(
        shelly,
        "find_valve_entity",
        AsyncMock(side_effect=RuntimeError("registry unavailable")),
    )
    generic_init = AsyncMock()
    monkeypatch.setattr(shelly, "generic_init", generic_init)

    await shelly.init(bt, ENTITY_ID)

    assert trv.valve_position_entity is None
    assert trv.valve_position_writable is False
    generic_init.assert_awaited_once_with(bt, ENTITY_ID)


def test_scale_valve_target_uses_full_range():
    """BT percentages map onto a non-zero Number range."""
    assert shelly._scale_valve_target(50, 10, 90, 5) == 50


def test_scale_valve_target_step_is_anchored_at_minimum():
    """Number step snapping is anchored at the entity minimum."""
    assert shelly._scale_valve_target(50, 10, 90, 7) == 52


def test_scale_valve_target_clamps_both_endstops():
    """Targets outside 0..100 cannot escape native Number bounds."""
    assert shelly._scale_valve_target(-10, 10, 90, 5) == 10
    assert shelly._scale_valve_target(150, 10, 90, 5) == 90


@pytest.mark.asyncio
async def test_set_valve_writes_intermediate_percentage():
    """Intermediate Direct Valve requests are sent to the native Number."""
    bt, _ = _bt()
    bt.hass.states.get.return_value = _state(0)

    await shelly.set_valve(bt, ENTITY_ID, 37)

    bt.hass.services.async_call.assert_awaited_once_with(
        "number",
        "set_value",
        {"entity_id": VALVE_ENTITY_ID, "value": 37},
        blocking=True,
        context=bt.context,
    )


@pytest.mark.asyncio
async def test_set_valve_scales_nonzero_minimum_and_step():
    """A valve command respects native minimum, maximum and step."""
    bt, _ = _bt()
    bt.hass.states.get.return_value = _state(10, minimum=10, maximum=90, step=7)

    await shelly.set_valve(bt, ENTITY_ID, 50)

    assert bt.hass.services.async_call.await_args.args[2]["value"] == 52


@pytest.mark.asyncio
async def test_set_valve_skips_confirmed_duplicate():
    """Confirmed duplicate commands do not generate another BLE write."""
    bt, trv = _bt()
    trv.last_valve_percent = 40
    bt.hass.states.get.return_value = _state(40)

    await shelly.set_valve(bt, ENTITY_ID, 40)

    bt.hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_valve_skips_matching_readback_after_adapter_start():
    """Matching native readback may suppress the first duplicate command."""
    bt, trv = _bt()
    trv.last_valve_percent = None
    bt.hass.states.get.return_value = _state(0)

    await shelly.set_valve(bt, ENTITY_ID, 0)

    bt.hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_readback_does_not_suppress_reversal():
    """A stale 0 readback cannot suppress a new 100 -> 0 reversal."""
    bt, trv = _bt()
    trv.last_valve_percent = 100
    bt.hass.states.get.return_value = _state(0)

    await shelly.set_valve(bt, ENTITY_ID, 0)

    bt.hass.services.async_call.assert_awaited_once()
    assert bt.hass.services.async_call.await_args.args[2]["value"] == 0


@pytest.mark.asyncio
async def test_external_divergence_is_corrected():
    """A changed native Number is corrected even if the desired target is unchanged."""
    bt, trv = _bt()
    trv.last_valve_percent = 40
    bt.hass.states.get.return_value = _state(20)

    await shelly.set_valve(bt, ENTITY_ID, 40)

    assert bt.hass.services.async_call.await_args.args[2]["value"] == 40


@pytest.mark.asyncio
async def test_set_valve_propagates_service_failure():
    """A failed Number write must not look successful to the delegate."""
    bt, _ = _bt()
    bt.hass.states.get.return_value = _state(0)
    bt.hass.services.async_call.side_effect = RuntimeError("write failed")

    with pytest.raises(RuntimeError, match="write failed"):
        await shelly.set_valve(bt, ENTITY_ID, 50)


@pytest.mark.asyncio
async def test_set_valve_raises_when_number_unavailable():
    """Unavailable valve readback fails closed instead of recording success."""
    bt, _ = _bt()
    bt.hass.states.get.return_value = _state("unavailable")

    with pytest.raises(RuntimeError, match="unavailable"):
        await shelly.set_valve(bt, ENTITY_ID, 50)

    bt.hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_runtime_rediscovery_handles_entity_id_change(monkeypatch):
    """A missing cached entity can be replaced by a newly discovered Number."""
    bt, trv = _bt(valve_entity=VALVE_ENTITY_ID)

    def state_get(entity_id):
        if entity_id == VALVE_ENTITY_ID:
            return None
        if entity_id == RENAMED_VALVE_ENTITY_ID:
            return _state(0)
        return None

    bt.hass.states.get.side_effect = state_get
    monkeypatch.setattr(
        shelly,
        "find_valve_entity",
        AsyncMock(return_value=_valve_discovery(RENAMED_VALVE_ENTITY_ID)),
    )

    await shelly.set_valve(bt, ENTITY_ID, 50)

    assert trv.valve_position_entity == RENAMED_VALVE_ENTITY_ID
    assert bt.hass.services.async_call.await_args.args[2] == {
        "entity_id": RENAMED_VALVE_ENTITY_ID,
        "value": 50,
    }


@pytest.mark.asyncio
async def test_runtime_rediscovery_miss_preserves_cached_valve(monkeypatch):
    """A transient registry miss does not disable all later delegate retries."""
    bt, trv = _bt()
    monkeypatch.setattr(shelly, "find_valve_entity", AsyncMock(return_value=None))

    discovered = await shelly._discover_valve(
        bt, ENTITY_ID, clear_on_miss=False
    )

    assert discovered is None
    assert trv.valve_position_entity == VALVE_ENTITY_ID
    assert trv.valve_position_writable is True


@pytest.mark.asyncio
async def test_set_temperature_is_skipped_only_in_direct_valve_mode(monkeypatch):
    """Valve-only Shelly mode does not receive redundant climate setpoints."""
    bt, _ = _bt(direct=True)
    bt.hass.states.get.return_value = _state(0, temperature=None)
    generic_set_temperature = AsyncMock()
    monkeypatch.setattr(shelly, "generic_set_temperature", generic_set_temperature)

    await shelly.set_temperature(bt, ENTITY_ID, 21.5)

    generic_set_temperature.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_temperature_uses_generic_when_native_target_exists(monkeypatch):
    """Shelly thermostat mode retains generic target-temperature behavior."""
    bt, _ = _bt(direct=True)
    bt.hass.states.get.return_value = _state(0, temperature=20.0)
    generic_set_temperature = AsyncMock(return_value=None)
    monkeypatch.setattr(shelly, "generic_set_temperature", generic_set_temperature)

    await shelly.set_temperature(bt, ENTITY_ID, 21.5)

    generic_set_temperature.assert_awaited_once_with(bt, ENTITY_ID, 21.5)


@pytest.mark.asyncio
async def test_set_temperature_uses_generic_outside_direct_valve(monkeypatch):
    """Other calibration types are unchanged even if temperature is None."""
    bt, _ = _bt(direct=False)
    bt.hass.states.get.return_value = _state(0, temperature=None)
    generic_set_temperature = AsyncMock(return_value=None)
    monkeypatch.setattr(shelly, "generic_set_temperature", generic_set_temperature)

    await shelly.set_temperature(bt, ENTITY_ID, 21.5)

    generic_set_temperature.assert_awaited_once_with(bt, ENTITY_ID, 21.5)


@pytest.mark.asyncio
async def test_set_temperature_uses_generic_without_writable_valve(monkeypatch):
    """A non-writable valve cannot suppress the native setpoint path."""
    bt, _ = _bt(direct=True, writable=False)
    bt.hass.states.get.return_value = _state(0, temperature=None)
    generic_set_temperature = AsyncMock(return_value=None)
    monkeypatch.setattr(shelly, "generic_set_temperature", generic_set_temperature)

    await shelly.set_temperature(bt, ENTITY_ID, 21.5)

    generic_set_temperature.assert_awaited_once_with(bt, ENTITY_ID, 21.5)


@pytest.mark.asyncio
async def test_set_valve_raises_without_writable_number(monkeypatch):
    """Direct Valve cannot silently succeed without an actuator Number."""
    bt, trv = _bt(writable=False, valve_entity=None)
    monkeypatch.setattr(shelly, "find_valve_entity", AsyncMock(return_value=None))

    with pytest.raises(RuntimeError, match="No writable Shelly valve Number"):
        await shelly.set_valve(bt, ENTITY_ID, 50)

    assert trv.valve_position_entity is None
    bt.hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_valve_rejects_invalid_number_bounds():
    """Malformed native Number bounds fail instead of producing a bad command."""
    bt, _ = _bt()
    bt.hass.states.get.return_value = _state(0, minimum=100, maximum=0, step=1)

    with pytest.raises(RuntimeError, match="Invalid Shelly valve state/bounds"):
        await shelly.set_valve(bt, ENTITY_ID, 50)

    bt.hass.services.async_call.assert_not_awaited()
