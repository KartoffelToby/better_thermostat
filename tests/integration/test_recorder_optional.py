"""A thermostat keeps working when the recorder fails to set up.

The recorder serves one optional read: the outdoor sensor's history, which
fills the damped outdoor temperature. Without it the filter starts at the
current outdoor reading. A broken recorder database must therefore not keep
the thermostat from loading.
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
    """The entry loads and the outdoor check damps the live readings."""
    set_room_sensor(hass, 18.0)
    _publish_outdoor(hass, 2.0)
    entry = make_entry(fake_trv.profile, with_outdoor_sensor=True, off_temperature=5)
    with patch("homeassistant.components.recorder.async_setup", return_value=False):
        await setup_entry(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    bt = await wait_for_startup(hass, entry)
    assert await wait_for(hass, lambda: bt.damped_outdoor_temperature == 2.0)
    assert bt.call_for_heat is True

    # A warm reading enters the filter but has no weight yet.
    _publish_outdoor(hass, 8.0)
    assert await wait_for(hass, lambda: bt.outdoor_damping.reading == 8.0)
    assert bt.damped_outdoor_temperature == pytest.approx(2.0, abs=0.01)
    assert bt.call_for_heat is True
    assert "recorder" not in hass.config.components
