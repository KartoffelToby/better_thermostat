"""The preset an MQTT TRV is put on when Better Thermostat takes it over.

Better Thermostat owns the setpoint, so a device still running its own
schedule has to be taken off it. Home Assistant's MQTT climate entity
contributes ``none`` to every device's ``preset_modes`` and rejects a
discovery config that lists it, so ``none`` is never the device's own
answer and never a value it accepts: the reset has to name a preset the
device brought itself.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import State
import pytest

from custom_components.better_thermostat.adapters.mqtt import init, manual_preset
from custom_components.better_thermostat.trv import Trv

ENTITY_ID = "climate.test_trv"
_MQTT = "custom_components.better_thermostat.adapters.mqtt"
# init imports the valve lookup at call time, so the patch goes on the source.
_FIND_VALVE = "custom_components.better_thermostat.utils.helpers.find_valve_entity"
_FIND_CALIBRATION = f"{_MQTT}.find_local_calibration_entity"
_WAIT_FOR_CALIBRATION = f"{_MQTT}.wait_for_calibration_entity_or_timeout"


def _bt(preset_modes, preset_mode=None) -> MagicMock:
    """Build a stand-in whose TRV reports ``preset_modes`` and needs a reset.

    ``calibration`` is deliberately not 1: the preset reset sits inside the
    branch that discovers the local calibration entity, so a TRV that needs
    no such lookup never reaches it.
    """
    bt = MagicMock()
    bt.device_name = "Test BT"
    bt.real_trvs = {ENTITY_ID: Trv(entity_id=ENTITY_ID, calibration=0)}
    bt.hass.states.get.return_value = State(
        ENTITY_ID, "heat", {"preset_modes": preset_modes, "preset_mode": preset_mode}
    )
    bt.hass.services.async_call = AsyncMock()
    return bt


async def _init(bt) -> None:
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
    bt = _bt(["none", "auto", "manual", "holiday"], preset_mode="auto")
    await _init(bt)
    assert _preset_calls(bt) == [{"entity_id": ENTITY_ID, "preset_mode": "manual"}]


@pytest.mark.asyncio
async def test_preset_reset_is_skipped_without_a_manual_preset():
    """A device with no manual preset keeps the one it is on."""
    bt = _bt(["none", "eco"], preset_mode="eco")
    await _init(bt)
    assert _preset_calls(bt) == []


@pytest.mark.asyncio
async def test_preset_reset_is_skipped_when_the_trv_already_runs_manual():
    """Nothing is written to a device that is already off its schedule."""
    bt = _bt(["none", "auto", "manual"], preset_mode="manual")
    await _init(bt)
    assert _preset_calls(bt) == []
