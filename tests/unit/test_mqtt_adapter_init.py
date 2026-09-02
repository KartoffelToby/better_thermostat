"""Startup discovery in the MQTT adapter.

Zigbee2MQTT exposes the valve position as a separate number entity, which the
adapter looks up once at startup. The lookup walks the entity and device
registries, so it is best effort: a lookup that fails leaves the TRV without a
valve entity and records why, and startup continues.

Both steps of that startup are shared with the other adapters that reach the
same two channels, so the seams patched here sit in the modules that hold
them rather than in the MQTT adapter itself.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import State
import pytest

from custom_components.better_thermostat.adapters.mqtt import (
    get_info,
    init,
    manual_preset,
)
from custom_components.better_thermostat.trv import Trv

ENTITY_ID = "climate.test_trv"
_VALVE_LOGGER = "custom_components.better_thermostat.adapters.valve_entity"
_FIND_VALVE = (
    "custom_components.better_thermostat.adapters.valve_entity.find_valve_entity"
)
_FIND_CALIBRATION = (
    "custom_components.better_thermostat.adapters.generic.find_local_calibration_entity"
)
_WAIT_FOR_CALIBRATION = (
    "custom_components.better_thermostat.adapters.generic."
    "wait_for_calibration_entity_or_timeout"
)


def _bt() -> MagicMock:
    """Build a BetterThermostat stand-in whose calibration needs no lookup."""
    bt = MagicMock()
    bt.device_name = "Test BT"
    bt.real_trvs = {ENTITY_ID: Trv(entity_id=ENTITY_ID, calibration=1)}
    return bt


@pytest.mark.asyncio
async def test_writable_valve_entity_is_adopted():
    """A writable valve entity is stored together with its writability."""
    bt = _bt()
    valve = {"entity_id": "number.valve_position", "writable": True}
    with patch(_FIND_VALVE, AsyncMock(return_value=valve)):
        await init(bt, ENTITY_ID)
    assert bt.real_trvs[ENTITY_ID].valve_position_entity == "number.valve_position"
    assert bt.real_trvs[ENTITY_ID].valve_position_writable is True


@pytest.mark.asyncio
async def test_read_only_valve_entity_is_marked_unwritable():
    """A valve entity without write support is stored as read-only."""
    bt = _bt()
    valve = {"entity_id": "sensor.valve_position", "writable": False}
    with patch(_FIND_VALVE, AsyncMock(return_value=valve)):
        await init(bt, ENTITY_ID)
    assert bt.real_trvs[ENTITY_ID].valve_position_writable is False


@pytest.mark.asyncio
async def test_no_valve_entity_leaves_the_trv_untouched():
    """Without a discovered entity the TRV keeps its defaults."""
    bt = _bt()
    with patch(_FIND_VALVE, AsyncMock(return_value=None)):
        await init(bt, ENTITY_ID)
    assert bt.real_trvs[ENTITY_ID].valve_position_entity is None


@pytest.mark.asyncio
async def test_failed_discovery_is_traced_and_init_continues(caplog):
    """A discovery failure names the TRV and leaves the entity unset."""
    bt = _bt()
    with (
        caplog.at_level(logging.DEBUG, logger=_VALVE_LOGGER),
        patch(_FIND_VALVE, AsyncMock(side_effect=RuntimeError("no registry"))),
    ):
        await init(bt, ENTITY_ID)
    assert bt.real_trvs[ENTITY_ID].valve_position_entity is None
    assert f"valve entity discovery for {ENTITY_ID} failed" in caplog.text
    assert any(record.exc_info for record in caplog.records)


@pytest.mark.asyncio
async def test_successful_discovery_is_not_traced_as_a_failure(caplog):
    """A discovery that lands reports nothing."""
    bt = _bt()
    valve = {"entity_id": "number.valve_position", "writable": True}
    with (
        caplog.at_level(logging.DEBUG, logger=_VALVE_LOGGER),
        patch(_FIND_VALVE, AsyncMock(return_value=valve)),
    ):
        await init(bt, ENTITY_ID)
    assert "failed" not in caplog.text


def _bt_with_preset(preset_modes, preset_mode=None) -> MagicMock:
    """Build a stand-in whose TRV reports ``preset_modes`` and needs a reset.

    ``calibration`` is deliberately not 1, the value a configuration that
    names no calibration type leaves behind; every type a configuration can
    name reaches the reset.
    """
    bt = MagicMock()
    bt.device_name = "Test BT"
    bt.real_trvs = {ENTITY_ID: Trv(entity_id=ENTITY_ID, calibration=0)}
    bt.hass.states.get.return_value = State(
        ENTITY_ID, "heat", {"preset_modes": preset_modes, "preset_mode": preset_mode}
    )
    bt.hass.services.async_call = AsyncMock()
    return bt


async def _init_with_preset(bt) -> None:
    """Run init past the discovery steps the preset reset sits behind."""
    with (
        patch(_FIND_VALVE, AsyncMock(return_value=None)),
        patch(_FIND_CALIBRATION, AsyncMock(return_value=None)),
        patch(_WAIT_FOR_CALIBRATION, AsyncMock(return_value=None)),
    ):
        await init(bt, ENTITY_ID)


def _preset_calls(bt) -> list[dict]:
    """The preset payloads init dispatched at the TRV."""
    return [
        call.args[2]
        for call in bt.hass.services.async_call.call_args_list
        if call.args[:2] == ("climate", "set_preset_mode")
    ]


@pytest.mark.parametrize(
    ("preset_modes", "expected"),
    [
        # What Home Assistant reports for a Zigbee2MQTT TRV: the device names
        # its own presets and HA prepends "none" itself.
        (["none", "auto", "manual", "holiday"], "manual"),
        (["none", "programming", "manual", "temporary_manual", "holiday"], "manual"),
        # Spelling is the device's; the match is not.
        (["none", "Manual"], "Manual"),
    ],
)
def test_manual_preset_picks_the_device_preset_that_hands_the_setpoint_over(
    preset_modes, expected
):
    """The device's manual preset wins over the "none" HA contributes."""
    assert manual_preset(preset_modes) == expected


@pytest.mark.parametrize(
    "preset_modes",
    [
        # Nothing the device offers takes it off its own schedule.
        ["none", "eco", "comfort"],
        # "none" alone is HA's contribution and nothing else, so a device
        # reporting only it names no preset of its own.
        ["none"],
        [],
        None,
        # A bare string is not a list of presets; iterating it would match
        # single characters.
        "manual",
    ],
)
def test_manual_preset_answers_none_when_the_device_offers_no_manual(preset_modes):
    """No manual preset means nothing is written, not that "none" is."""
    assert manual_preset(preset_modes) is None


@pytest.mark.asyncio
async def test_preset_reset_writes_the_device_preset_not_none():
    """The reset sends "manual"; "none" is what the broker rejects."""
    bt = _bt_with_preset(["none", "auto", "manual", "holiday"], preset_mode="auto")
    await _init_with_preset(bt)
    assert _preset_calls(bt) == [{"entity_id": ENTITY_ID, "preset_mode": "manual"}]


@pytest.mark.asyncio
async def test_preset_reset_is_skipped_without_a_manual_preset():
    """A device with no manual preset keeps the one it is on."""
    bt = _bt_with_preset(["none", "eco"], preset_mode="eco")
    await _init_with_preset(bt)
    assert _preset_calls(bt) == []


@pytest.mark.asyncio
async def test_preset_reset_is_skipped_when_the_trv_already_runs_manual():
    """Nothing is written to a device that is already off its schedule."""
    bt = _bt_with_preset(["none", "auto", "manual"], preset_mode="manual")
    await _init_with_preset(bt)
    assert _preset_calls(bt) == []


@pytest.mark.parametrize("calibration", [0, 2, 3])
@pytest.mark.asyncio
async def test_every_calibration_a_configuration_can_name_reaches_the_reset(
    calibration,
):
    """How a TRV calibrates does not decide whether it keeps its schedule.

    The reset is read off the record startup built, before the lookup in
    the same pass fills the calibration entity in, so a TRV that ends up
    with one is taken off its schedule as much as a TRV that never had one.
    """
    bt = _bt_with_preset(["none", "auto", "manual"], preset_mode="auto")
    bt.real_trvs[ENTITY_ID].calibration = calibration

    with (
        patch(_FIND_VALVE, AsyncMock(return_value=None)),
        patch(_FIND_CALIBRATION, AsyncMock(return_value="number.trv_calibration")),
        patch(_WAIT_FOR_CALIBRATION, AsyncMock(return_value=None)),
    ):
        await init(bt, ENTITY_ID)

    assert _preset_calls(bt) == [{"entity_id": ENTITY_ID, "preset_mode": "manual"}]


_GET_INFO_FIND_VALVE = (
    "custom_components.better_thermostat.adapters.mqtt.find_valve_entity"
)
_GET_INFO_FIND_CALIBRATION = (
    "custom_components.better_thermostat.adapters.mqtt.find_local_calibration_entity"
)


async def _reported_surface(calibration_entity, valve):
    """What the adapter reports for the entities discovery would find."""
    with (
        patch(_GET_INFO_FIND_CALIBRATION, AsyncMock(return_value=calibration_entity)),
        patch(_GET_INFO_FIND_VALVE, AsyncMock(return_value=valve)),
    ):
        return await get_info(_bt(), ENTITY_ID)


class TestTheReportedSurface:
    """What the config flow is offered follows what the device exposes.

    ``get_info`` runs before any TRV record exists, on the config flow
    handler as well as on the climate entity, and its answer is what
    decides which calibration modes a user can pick.
    """

    @pytest.mark.asyncio
    async def test_a_device_without_either_entity_offers_no_channel(self):
        """Nothing discovered means nothing to offer."""
        assert await _reported_surface(None, None) == {
            "support_offset": False,
            "support_valve": False,
        }

    @pytest.mark.asyncio
    async def test_a_calibration_entity_enables_the_offset_channel(self):
        """A discovered calibration entity is the offset channel."""
        surface = await _reported_surface("number.trv_calibration", None)

        assert surface == {"support_offset": True, "support_valve": False}

    @pytest.mark.asyncio
    async def test_a_writable_valve_entity_enables_the_valve_channel(self):
        """A valve entity that accepts writes is the valve channel."""
        valve = {"entity_id": "number.valve_position", "writable": True}

        surface = await _reported_surface(None, valve)

        assert surface == {"support_offset": False, "support_valve": True}

    @pytest.mark.asyncio
    async def test_a_read_only_valve_entity_offers_no_valve_channel(self):
        """A valve that only reports back cannot be driven."""
        valve = {"entity_id": "sensor.valve_position", "writable": False}

        surface = await _reported_surface(None, valve)

        assert surface["support_valve"] is False

    @pytest.mark.asyncio
    async def test_a_valve_result_without_an_entity_offers_no_channel(self):
        """A discovery result naming no entity addresses nothing."""
        surface = await _reported_surface(None, {"entity_id": "", "writable": True})

        assert surface["support_valve"] is False
