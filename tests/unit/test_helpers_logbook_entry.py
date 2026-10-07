"""Logbook annunciation (helpers.async_fire_logbook_entry).

The entry carries the translated message for *key* when the translation
catalogue can be read, and the caller-supplied default otherwise. Either way
the event is fired, and a catalogue that cannot be read is recorded at debug
level.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.better_thermostat.utils.helpers import async_fire_logbook_entry
from tests.factories import ThermostatStandIn

_HELPERS = "custom_components.better_thermostat.utils.helpers"
_TRANSLATIONS = "homeassistant.helpers.translation.async_get_translations"
_KEY = "component.better_thermostat.entity.sensor.logbook.state.window_open"


def _bt() -> MagicMock:
    """Build the caller surface async_fire_logbook_entry reads."""
    bt = ThermostatStandIn()
    bt.hass = MagicMock()
    bt.hass.config.language = "de"
    bt.entity_id = "climate.test_bt"
    bt.device_name = "Test BT"
    # A thermostat named by its device, as Home Assistant sees it.
    bt.name = None
    return bt


def _fired(bt: MagicMock) -> dict:
    """Return the payload of the single fired logbook event."""
    bt.hass.bus.async_fire.assert_called_once()
    return bt.hass.bus.async_fire.call_args[0][1]


@pytest.mark.asyncio
async def test_translated_message_is_used():
    """A catalogue hit replaces the default message."""
    bt = _bt()
    with patch(_TRANSLATIONS, AsyncMock(return_value={_KEY: "Fenster offen"})):
        await async_fire_logbook_entry(bt, "window_open", "Window open")
    assert _fired(bt)["message"] == "Fenster offen"


@pytest.mark.asyncio
async def test_missing_translation_keeps_the_default():
    """A catalogue without the key keeps the default message."""
    bt = _bt()
    with patch(_TRANSLATIONS, AsyncMock(return_value={})):
        await async_fire_logbook_entry(bt, "window_open", "Window open")
    assert _fired(bt)["message"] == "Window open"


@pytest.mark.asyncio
async def test_unreadable_catalogue_is_traced_and_entry_still_fires(caplog):
    """A catalogue that raises still yields an entry with the default message."""
    bt = _bt()
    with (
        caplog.at_level(logging.DEBUG, logger=_HELPERS),
        patch(_TRANSLATIONS, AsyncMock(side_effect=RuntimeError("no catalogue"))),
    ):
        await async_fire_logbook_entry(bt, "window_open", "Window open")
    assert _fired(bt)["message"] == "Window open"
    assert "logbook translation for window_open unavailable" in caplog.text
    assert any(record.exc_info for record in caplog.records)


@pytest.mark.asyncio
async def test_the_entry_carries_the_configured_name():
    """The entry names the thermostat by its configured name.

    The entity takes its name from the device, so Home Assistant reports
    the entity's own name as None.
    """
    bt = _bt()
    with patch(_TRANSLATIONS, AsyncMock(return_value={})):
        await async_fire_logbook_entry(bt, "window_open", "Window open")
    assert _fired(bt)["name"] == "Test BT"
    assert _fired(bt)["entity_id"] == "climate.test_bt"


@pytest.mark.asyncio
async def test_an_entity_without_an_id_yet_names_the_id_its_name_produces():
    """Before Home Assistant assigns the entity id, the entry predicts it.

    The id is the one the configured name produces.
    """
    bt = _bt()
    bt.entity_id = None
    bt.device_name = "Living Room"
    with patch(_TRANSLATIONS, AsyncMock(return_value={})):
        await async_fire_logbook_entry(bt, "window_open", "Window open")
    assert _fired(bt)["entity_id"] == "climate.living_room"


@pytest.mark.asyncio
async def test_an_entity_not_yet_added_fires_nothing():
    """Without ``hass`` there is no bus to fire on."""
    bt = _bt()
    hass = bt.hass
    bt.hass = None
    with patch(_TRANSLATIONS, AsyncMock(return_value={})) as translations:
        await async_fire_logbook_entry(bt, "window_open", "Window open")
    translations.assert_not_awaited()
    hass.bus.async_fire.assert_not_called()
