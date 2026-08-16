"""The preset behaviour the schedule and night mode guide instructs readers to rely on.

The guide replaces three removed Better Thermostat actions with
``climate.set_preset_mode``. Every behavioural sentence it makes is pinned here
against a real config entry, so a change in preset handling breaks a test
instead of silently turning the published instructions into wrong advice.
"""

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import State
from homeassistant.exceptions import ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
)

from .conftest import DOMAIN, SENSOR_ID, make_entry, setup_entry, wait_for_startup

BT_ENTITY = "climate.bt_test"
SLEEP_NUMBER = "number.bt_test_sleep"


async def _setup(hass, presets=("sleep", "eco")):
    hass.states.async_set(SENSOR_ID, "18.0", {"unit_of_measurement": "°C"})
    data = dict(make_entry().data)
    data["presets"] = list(presets)
    entry = MockConfigEntry(domain=DOMAIN, version=18, data=data, title=data["name"])
    await setup_entry(hass, entry)
    return await wait_for_startup(hass, entry)


async def _set_preset(hass, preset):
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        "set_preset_mode",
        {ATTR_ENTITY_ID: BT_ENTITY, "preset_mode": preset},
        blocking=True,
    )
    await hass.async_block_till_done()


async def _set_target(hass, temperature):
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        "set_temperature",
        {ATTR_ENTITY_ID: BT_ENTITY, "temperature": temperature},
        blocking=True,
    )
    await hass.async_block_till_done()


async def _set_number(hass, entity_id, value):
    await hass.services.async_call(
        "number",
        "set_value",
        {ATTR_ENTITY_ID: entity_id, "value": value},
        blocking=True,
    )
    await hass.async_block_till_done()


def _target(hass):
    return hass.states.get(BT_ENTITY).attributes.get("temperature")


def _preset(hass):
    return hass.states.get(BT_ENTITY).attributes.get("preset_mode")


def _restore_point(hass):
    return hass.states.get(BT_ENTITY).attributes.get("preset_temperature")


@pytest.mark.asyncio
async def test_entering_a_preset_stores_the_current_target(hass, fake_trv):
    await _setup(hass)
    await _set_target(hass, 21.0)

    await _set_preset(hass, "sleep")

    assert _preset(hass) == "sleep"
    assert _target(hass) == 18.0
    assert _restore_point(hass) == 21.0


@pytest.mark.asyncio
async def test_switching_between_presets_keeps_the_first_stored_target(hass, fake_trv):
    await _setup(hass)
    await _set_target(hass, 21.0)
    await _set_preset(hass, "sleep")

    await _set_preset(hass, "eco")

    assert _target(hass) == 19.0
    assert _restore_point(hass) == 21.0

    await _set_preset(hass, "none")
    assert _target(hass) == 21.0


@pytest.mark.asyncio
async def test_returning_to_none_restores_the_target_and_clears_the_store(
    hass, fake_trv
):
    await _setup(hass)
    await _set_target(hass, 21.0)
    await _set_preset(hass, "sleep")

    await _set_preset(hass, "none")

    assert _preset(hass) == "none"
    assert _target(hass) == 21.0
    assert _restore_point(hass) is None


@pytest.mark.asyncio
async def test_a_second_none_call_leaves_the_target_alone(hass, fake_trv):
    await _setup(hass)
    await _set_target(hass, 21.0)
    await _set_preset(hass, "sleep")
    await _set_preset(hass, "none")

    await _set_preset(hass, "none")

    assert _target(hass) == 21.0
    assert _restore_point(hass) is None


@pytest.mark.asyncio
async def test_a_manual_target_change_cancels_the_preset_and_drops_the_store(
    hass, fake_trv
):
    await _setup(hass)
    await _set_target(hass, 21.0)
    await _set_preset(hass, "sleep")

    await _set_target(hass, 16.5)

    assert _preset(hass) == "none"
    assert _target(hass) == 16.5
    assert _restore_point(hass) is None

    # The restore point is gone, so the closing call of a schedule window is
    # unable to put 21.0 back.
    await _set_preset(hass, "none")
    assert _target(hass) == 16.5


@pytest.mark.asyncio
async def test_writing_the_preset_number_retargets_without_touching_the_store(
    hass, fake_trv
):
    await _setup(hass)
    await _set_target(hass, 21.0)
    await _set_preset(hass, "sleep")

    await _set_number(hass, SLEEP_NUMBER, 17.0)

    assert _preset(hass) == "sleep"
    assert _target(hass) == 17.0
    assert _restore_point(hass) == 21.0

    await _set_preset(hass, "none")
    assert _target(hass) == 21.0


@pytest.mark.asyncio
async def test_a_preset_that_is_not_enabled_is_rejected_by_the_service_call(
    hass, fake_trv
):
    await _setup(hass, presets=("sleep",))
    await _set_target(hass, 21.0)

    with pytest.raises(ServiceValidationError):
        await _set_preset(hass, "eco")

    assert _preset(hass) == "none"
    assert _target(hass) == 21.0


@pytest.mark.asyncio
async def test_the_restore_point_survives_a_restart(hass, fake_trv):
    mock_restore_cache(
        hass,
        (
            State(
                BT_ENTITY,
                "heat",
                {
                    "temperature": 18.0,
                    "preset_mode": "sleep",
                    "preset_temperature": 21.5,
                    "unit_of_measurement": "°C",
                },
            ),
        ),
    )
    await _setup(hass)

    assert _preset(hass) == "sleep"
    assert _restore_point(hass) == 21.5

    await _set_preset(hass, "none")
    assert _target(hass) == 21.5
