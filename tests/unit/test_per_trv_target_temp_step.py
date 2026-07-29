"""The per-device setpoint step stays the device's own grid.

``bt_target_temp_step`` is the coarsest step across all children — every TRV
plus the cooler — and is what the integration exposes as its own
``target_temperature_step``. Handing that aggregate to each child would size
the inbound echo window by the coarsest device, so a user turning a
fine-grained TRV by less than the coarse step would have the change
classified as an echo of a Better Thermostat write and dropped.
"""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.climate.const import ATTR_TARGET_TEMP_STEP, HVACMode
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import State
from homeassistant.util import dt as dt_util
import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.events.trv import trigger_trv_change
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.const import (
    CONF_HOMEMATICIP,
    CalibrationMode,
    CalibrationType,
)

TRV_ID = "climate.fine_trv"
COOLER_ID = "climate.coarse_ac"

FINE_STEP = 0.1
COARSE_STEP = 1.0


def _child_state(
    entity_id, step, temperature=21.0, current=20.0, min_t=5.0, max_t=30.0, unit=None
):
    """Build a climate child state, publishing its own step when given."""
    attrs = {
        "min_temp": min_t,
        "max_temp": max_t,
        ATTR_TEMPERATURE: temperature,
        "current_temperature": current,
    }
    if step is not None:
        attrs[ATTR_TARGET_TEMP_STEP] = step
    if unit is not None:
        attrs["temperature_unit"] = unit
    return State(entity_id, "heat", attributes=attrs)


@pytest.fixture
def bt():
    """Mock thermostat wired to one fine TRV and one coarse cooler."""
    mock = MagicMock()
    mock.device_name = "Test BT"
    mock.hass = MagicMock()
    mock.hass.config.units.temperature_unit = UnitOfTemperature.CELSIUS
    mock.all_entities = []
    mock.cooler_entity_id = COOLER_ID
    mock.bt_min_temp = None
    mock.bt_max_temp = None
    mock.bt_target_temp_step = None
    mock._configured_target_temp_step = None
    mock.bt_target_temp = 21.0
    mock.bt_target_cooltemp = 25.0
    mock.bt_hvac_mode = HVACMode.HEAT
    mock.cur_temp = 20.0
    mock.tolerance = 0.3
    mock.startup_running = False
    mock.bt_update_lock = False
    mock.ignore_states = False
    mock.contact_open = False
    mock.window_open = False
    mock.control_queue_task = MagicMock()
    mock.context = MagicMock()
    mock.last_internal_sensor_change = dt_util.now() - timedelta(seconds=60)
    mock.all_trvs = [{"advanced": {CONF_HOMEMATICIP: False}}]
    mock.real_trvs = {
        TRV_ID: Trv(
            entity_id=TRV_ID,
            calibration=1,
            model="SomeModel",
            hvac_mode=HVACMode.HEAT,
            last_hvac_mode=HVACMode.HEAT,
            last_temperature=21.0,
            advanced={
                "calibration": CalibrationType.TARGET_TEMP_BASED,
                "calibration_mode": CalibrationMode.DEFAULT,
                "child_lock": False,
                "no_off_system_mode": False,
                "heat_auto_swapped": False,
            },
        )
    }
    return mock


async def _run_startup(bt, trv_state):
    """Resolve the temperature range and initialize the TRV."""
    BetterThermostat._resolve_temperature_range(
        bt, [trv_state, _child_state(COOLER_ID, COARSE_STEP)]
    )
    bt.hass.states.get.return_value = trv_state
    with (
        patch("custom_components.better_thermostat.climate.init", AsyncMock()),
        patch("custom_components.better_thermostat.climate.initial_tweak", AsyncMock()),
        patch(
            "custom_components.better_thermostat.climate.control_trv",
            AsyncMock(return_value=True),
        ),
    ):
        await BetterThermostat._initialize_trvs(bt)


@pytest.mark.asyncio
async def test_fine_trv_keeps_its_own_step_next_to_a_coarse_cooler(bt):
    """The TRV gets its own 0.1 step while the entity exposes the coarse 1.0."""
    await _run_startup(bt, _child_state(TRV_ID, FINE_STEP))

    assert bt.bt_target_temp_step == pytest.approx(COARSE_STEP)
    assert bt.real_trvs[TRV_ID].target_temp_step == pytest.approx(FINE_STEP)


@pytest.mark.asyncio
async def test_half_degree_user_change_on_a_fine_trv_is_adopted(bt):
    """A 0.5 K turn is below the cooler's step but several of the TRV's own."""
    await _run_startup(bt, _child_state(TRV_ID, FINE_STEP))

    old_state = _child_state(TRV_ID, FINE_STEP, temperature=21.0)
    new_state = _child_state(TRV_ID, FINE_STEP, temperature=21.5)
    bt.hass.states.get.return_value = new_state
    event = MagicMock()
    event.context = MagicMock()
    event.data = {"old_state": old_state, "new_state": new_state, "entity_id": TRV_ID}

    with patch(
        "custom_components.better_thermostat.events.trv.convert_inbound_states",
        return_value=HVACMode.HEAT,
    ):
        await trigger_trv_change(bt, event)

    assert bt.bt_target_temp == pytest.approx(21.5)


@pytest.mark.asyncio
async def test_configured_step_overrides_the_device_step(bt):
    """An explicitly configured step is the user's decision and wins."""
    bt.bt_target_temp_step = 0.25
    bt._configured_target_temp_step = 0.25

    await _run_startup(bt, _child_state(TRV_ID, FINE_STEP))

    assert bt.real_trvs[TRV_ID].target_temp_step == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_fahrenheit_device_step_converts_as_a_delta(bt):
    """A child's step is a temperature difference, not an absolute reading."""
    await _run_startup(
        bt,
        _child_state(
            TRV_ID,
            1.0,
            temperature=70.0,
            current=68.0,
            min_t=41.0,
            max_t=86.0,
            unit=UnitOfTemperature.FAHRENHEIT,
        ),
    )

    assert bt.real_trvs[TRV_ID].target_temp_step == pytest.approx(
        round(1.0 * 5.0 / 9.0, 4)
    )


@pytest.mark.asyncio
async def test_device_without_a_step_falls_back_to_the_aggregate(bt):
    """A child publishing no step inherits the entity-level step."""
    await _run_startup(bt, _child_state(TRV_ID, None))

    assert bt.real_trvs[TRV_ID].target_temp_step == pytest.approx(COARSE_STEP)
