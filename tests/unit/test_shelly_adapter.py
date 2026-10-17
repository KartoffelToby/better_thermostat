"""Shelly registry discovery, ownership, write confirmation and recovery."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.core import Context
from homeassistant.helpers.entity_registry import RegistryEntryDisabler
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.better_thermostat.adapters import delegate, generic, shelly
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.const import CalibrationType


@pytest.fixture
def registered_trv(hass, entity_registry, device_registry):
    entry = MockConfigEntry(domain="shelly")
    entry.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("shelly", "blu-one")}
    )
    climate = entity_registry.async_get_or_create(
        "climate", "shelly", "blu-one", config_entry=entry, device_id=device.id
    )
    trv = Trv(
        entity_id=climate.entity_id,
        integration="shelly",
        adapter=shelly,
        advanced={"calibration": CalibrationType.DIRECT_VALVE_BASED},
    )
    bt = SimpleNamespace(
        hass=hass,
        context=Context(),
        device_name="Studio",
        real_trvs={trv.entity_id: trv},
    )

    def add_valve(
        *,
        device_id=device.id,
        domain="number",
        platform="shelly",
        key="valve_position",
        disabled=None,
        uid="blu-one-position",
    ):
        valve = entity_registry.async_get_or_create(
            domain,
            platform,
            uid,
            config_entry=entry,
            device_id=device_id,
            translation_key=key,
            disabled_by=disabled,
            suggested_object_id="renamed",
        )
        hass.states.async_set(valve.entity_id, "0", {"min": 0, "max": 100, "step": 1})
        return valve

    return bt, trv, add_valve, entry


@pytest.mark.parametrize("key", ["valve_position", "valve"])
async def test_discovery_uses_metadata_and_correct_gateway_child(
    registered_trv, entity_registry, device_registry, key
):
    bt, trv, add, entry = registered_trv
    other = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("shelly", "blu-two")}
    )
    add(device_id=other.id, uid="other-valve")
    add(disabled=RegistryEntryDisabler.USER, uid="disabled-valve")
    valve = add(key=key)
    valve = entity_registry.async_update_entity(
        valve.entity_id, new_entity_id="number.studio"
    )
    info = await shelly.get_info(bt, trv.entity_id)
    assert info["support_valve"] is True
    assert info["direct_valve_exclusive"] is True
    await shelly.init(bt, trv.entity_id)
    assert trv.valve_position_entity == valve.entity_id
    assert trv.exclusive_direct_valve


@pytest.mark.parametrize(
    "kwargs",
    [
        {"domain": "sensor"},
        {"domain": "input_number"},
        {"platform": "mqtt"},
        {"key": "unrelated"},
        {"disabled": RegistryEntryDisabler.USER},
        {"disabled": RegistryEntryDisabler.INTEGRATION},
        {"device_id": None},
    ],
)
async def test_rejects_ineligible_entities(registered_trv, kwargs):
    bt, trv, add, _ = registered_trv
    add(**kwargs)
    assert (await shelly.get_info(bt, trv.entity_id))["support_valve"] is False
    await shelly.init(bt, trv.entity_id)
    assert trv.valve_position_entity is None
    assert trv.exclusive_direct_valve
    assert await shelly.set_valve(bt, trv.entity_id, 37) is False


async def test_missing_entity_then_recovery_and_rename(registered_trv, entity_registry):
    bt, trv, add, _ = registered_trv
    await shelly.init(bt, trv.entity_id)
    with patch(
        "homeassistant.core.ServiceRegistry.async_call", new=AsyncMock()
    ) as write:
        assert await delegate.set_valve(bt, trv.entity_id, 37) is False
        assert trv.last_valve_percent is None
        valve = add()
        assert await delegate.set_valve(bt, trv.entity_id, 37) is True
        write.assert_awaited_once_with(
            "number",
            "set_value",
            {"entity_id": valve.entity_id, "value": 37},
            blocking=True,
            context=bt.context,
        )
        assert trv.last_valve_percent == 37
        assert trv.last_valve_method == "adapter"
        entity_registry.async_remove(valve.entity_id)
        assert await delegate.set_valve(bt, trv.entity_id, 90) is False
        assert trv.last_valve_percent == 37
        replacement = add(uid="replacement")
        replacement = entity_registry.async_update_entity(
            replacement.entity_id, new_entity_id="number.new_name"
        )
        bt.hass.states.async_set(replacement.entity_id, "0")
        assert await delegate.set_valve(bt, trv.entity_id, 50) is True
        assert write.await_args.args[2]["entity_id"] == "number.new_name"


@pytest.mark.parametrize("state", [None, "unavailable", "unknown"])
async def test_unavailable_does_not_record_success(registered_trv, state):
    bt, trv, add, _ = registered_trv
    valve = add()
    await shelly.init(bt, trv.entity_id)
    if state is None:
        bt.hass.states.async_remove(valve.entity_id)
    else:
        bt.hass.states.async_set(valve.entity_id, state)
    with patch(
        "homeassistant.core.ServiceRegistry.async_call", new=AsyncMock()
    ) as write:
        assert await delegate.set_valve(bt, trv.entity_id, 37) is False
        assert trv.last_valve_percent is None
        write.assert_not_awaited()
        bt.hass.states.async_set(valve.entity_id, "0")
        assert await delegate.set_valve(bt, trv.entity_id, 37) is True


async def test_failed_write_and_model_override_never_count_as_success(registered_trv):
    bt, trv, add, _ = registered_trv
    add()
    await shelly.init(bt, trv.entity_id)
    override = AsyncMock(return_value=True)
    trv.model_quirks = SimpleNamespace(override_set_valve=override)
    with patch(
        "homeassistant.core.ServiceRegistry.async_call",
        new=AsyncMock(side_effect=RuntimeError),
    ):
        assert await delegate.set_valve(bt, trv.entity_id, 37) is False
    assert trv.last_valve_percent is None
    override.assert_not_awaited()


async def test_no_calibration_init_even_if_discovery_raises(registered_trv):
    bt, trv, _, _ = registered_trv
    with (
        patch.object(shelly, "_find_valve", side_effect=RuntimeError),
        patch.object(generic, "init", new=AsyncMock()) as init,
    ):
        with pytest.raises(RuntimeError):
            await shelly.init(bt, trv.entity_id)
    assert trv.exclusive_direct_valve
    init.assert_not_awaited()


async def test_delegates_guard_before_cache_changes_and_allow_normal_mode(
    registered_trv,
):
    bt, trv, _, _ = registered_trv
    await shelly.init(bt, trv.entity_id)
    with patch(
        "homeassistant.core.ServiceRegistry.async_call", new=AsyncMock()
    ) as write:
        assert await delegate.set_temperature(bt, trv.entity_id, 25) is False
        assert await delegate.set_hvac_mode(bt, trv.entity_id, "heat") is False
        assert await delegate.set_offset(bt, trv.entity_id, 0) is False
        assert trv.last_temperature is None
        assert trv.last_calibration_requested is None
        assert (
            trv.target_temp_received
            and trv.system_mode_received
            and trv.calibration_received
        )
        write.assert_not_awaited()
        trv.advanced["calibration"] = CalibrationType.TARGET_TEMP_BASED
        await delegate.set_temperature(bt, trv.entity_id, 25)
        assert write.await_args.args[:2] == ("climate", "set_temperature")
        await delegate.set_hvac_mode(bt, trv.entity_id, "heat")
        assert write.await_args.args[:2] == ("climate", "set_hvac_mode")


@pytest.mark.parametrize("exclusive", [False, True])
@pytest.mark.parametrize("result", [False, None, True])
async def test_write_confirmation_is_scoped_to_exclusive_shelly(exclusive, result):
    trv = Trv(
        entity_id="climate.trv",
        integration="shelly" if exclusive else "mqtt",
        direct_valve_exclusive=True,
        advanced={"calibration": CalibrationType.DIRECT_VALVE_BASED},
        valve_position_entity="number.valve",
        valve_position_writable=True,
    )
    trv.adapter = SimpleNamespace(set_valve=AsyncMock(return_value=result))
    bt = SimpleNamespace(real_trvs={trv.entity_id: trv}, device_name="Studio")
    expected = result is True if exclusive else True
    assert await delegate.set_valve(bt, trv.entity_id, 37) is expected
    assert trv.last_valve_percent == (37 if expected else None)


async def test_setter_discovery_failure_returns_false(registered_trv):
    bt, trv, _, _ = registered_trv
    with patch.object(shelly, "_find_valve", side_effect=RuntimeError):
        assert await shelly.set_valve(bt, trv.entity_id, 37) is False
    assert trv.last_valve_percent is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), None, "invalid"])
async def test_invalid_requests_are_not_written(registered_trv, value):
    bt, trv, add, _ = registered_trv
    add()
    with patch(
        "homeassistant.core.ServiceRegistry.async_call", new=AsyncMock()
    ) as write:
        assert await shelly.set_valve(bt, trv.entity_id, value) is False
    write.assert_not_awaited()


async def test_valve_writes_are_serialized_only_for_the_same_shelly(registered_trv):
    bt, trv, add, _ = registered_trv
    valve = add()
    second = Trv(entity_id="climate.other_shelly", integration="shelly", adapter=shelly)
    bt.real_trvs[second.entity_id] = second
    bt.hass.states.async_set(
        "number.other_shelly", "0", {"min": 0, "max": 100, "step": 1}
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    completed = []

    async def discover(self, entity_id):
        return valve.entity_id if entity_id == trv.entity_id else "number.other_shelly"

    async def service(domain, name, data, **kwargs):
        if data["entity_id"] == valve.entity_id and data["value"] == 67:
            entered.set()
            await release.wait()
        completed.append((data["entity_id"], data["value"]))

    with (
        patch.object(shelly, "_discover", side_effect=discover),
        patch("homeassistant.core.ServiceRegistry.async_call", side_effect=service),
    ):
        opening = asyncio.create_task(shelly.set_valve(bt, trv.entity_id, 67))
        await asyncio.wait_for(entered.wait(), timeout=1)
        closing = asyncio.create_task(shelly.set_valve(bt, trv.entity_id, 0))
        try:
            assert await asyncio.wait_for(
                shelly.set_valve(bt, second.entity_id, 50), timeout=1
            )
            assert completed == [("number.other_shelly", 50)]
            assert not closing.done()
        finally:
            release.set()
            results = await asyncio.gather(opening, closing)
    assert results == [True, True]
    assert completed == [
        ("number.other_shelly", 50),
        (valve.entity_id, 67),
        (valve.entity_id, 0),
    ]
