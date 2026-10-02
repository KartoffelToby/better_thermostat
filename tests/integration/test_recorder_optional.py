"""A thermostat keeps working when the recorder fails to set up.

The recorder serves one optional read: the two-day outdoor mean, which falls
back to the current outdoor reading without it. A broken recorder database
must therefore not keep the thermostat from loading.
"""

from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import UnitOfTemperature
import pytest

from .conftest import (
    OUTDOOR_ID,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)


@pytest.fixture(autouse=True)
async def _recorder():
    """Replace the shared recorder fixture: this module runs without one."""
    return


def _publish_outdoor(hass, celsius: float) -> None:
    hass.states.async_set(
        OUTDOOR_ID, str(celsius), {"unit_of_measurement": UnitOfTemperature.CELSIUS}
    )


async def test_the_thermostat_loads_when_the_recorder_fails(hass, fake_trv):
    """The entry loads and the outdoor check runs on the current reading."""
    set_room_sensor(hass, 18.0)
    _publish_outdoor(hass, 2.0)
    entry = make_entry(fake_trv.profile, with_outdoor_sensor=True)
    with patch("homeassistant.components.recorder.async_setup", return_value=False):
        await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    bt = await wait_for_startup(hass, entry)
    _publish_outdoor(hass, 2.5)
    assert await wait_for(hass, lambda: bt.last_avg_outdoor_temp is not None)
    assert "recorder" not in hass.config.components
