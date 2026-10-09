"""What the model quirk shims hand back and record for the module they reach.

``model_quirks`` imports the quirk module of a TRV's model and forwards each
call into it. The shims answer with what the module answers and leave a
trace in the log only when that answer differs from the value they were
handed. Loading falls back to the default module, and a default module that
cannot be imported either is an installation fault that has to surface.
"""

import importlib
import logging
from types import ModuleType
from unittest.mock import MagicMock, patch

from homeassistant.core import State
import pytest

from custom_components.better_thermostat.model_fixes import model_quirks
from custom_components.better_thermostat.trv import Trv
from tests.factories import ThermostatStandIn

ENTITY_ID = "climate.trv"
LOGGER = model_quirks.__name__

TS0601 = importlib.import_module(
    "custom_components.better_thermostat.model_fixes.TS0601"
)
SHTRV_01 = importlib.import_module(
    "custom_components.better_thermostat.model_fixes.SHTRV-01"
)


def _host(model_quirks_module=None, trv_reading=None):
    """A host whose TRV carries ``model_quirks_module`` and reports ``trv_reading``."""
    host = ThermostatStandIn()
    host.device_name = "Test BT"
    host.hass = MagicMock()
    host.hass.config.units.temperature_unit = "°C"
    state = (
        None
        if trv_reading is None
        else State(ENTITY_ID, "heat", {"current_temperature": trv_reading})
    )
    host.hass.states.get = MagicMock(return_value=state)
    host.real_trvs = {
        ENTITY_ID: Trv(entity_id=ENTITY_ID, model_quirks=model_quirks_module)
    }
    return host


class TestLoadingTheQuirkModule:
    """The default module is the floor; below it nothing is left to load."""

    @pytest.mark.asyncio
    async def test_a_default_module_that_fails_to_import_surfaces(self, caplog):
        """Running without any quirk module would fail every control cycle later."""

        async def _refuse(_hass, name):
            raise ImportError(f"No module named {name!r}")

        host = ThermostatStandIn()
        host.device_name = "Test BT"

        with (
            patch.object(model_quirks, "async_import_module", side_effect=_refuse),
            caplog.at_level(logging.ERROR, logger=LOGGER),
            pytest.raises(ImportError, match="model_fixes.default"),
        ):
            await model_quirks.load_model_quirks(host, "Unknown-TRV", ENTITY_ID)

        assert "failed to import default quirks module" in caplog.text
        assert "Unknown-TRV" in caplog.text


class TestTheSetpointShim:
    """The setpoint a model quirk adjusts is the one the shim returns."""

    def test_an_adjusted_setpoint_is_returned_and_logged(self, caplog):
        """TS0601 lifts a setpoint within 1.5 K above its own reading."""
        host = _host(TS0601, trv_reading=20.0)

        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            answer = model_quirks.fix_target_temperature_calibration(
                host, ENTITY_ID, 21.0
            )

        assert answer == pytest.approx(22.5)
        assert "temperature offset model fix: 21.0 to 22.5" in caplog.text

    def test_an_unchanged_setpoint_is_not_logged(self, caplog):
        """A quirk that leaves the request alone leaves no trace either."""
        host = _host(TS0601, trv_reading=23.0)

        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            answer = model_quirks.fix_target_temperature_calibration(
                host, ENTITY_ID, 21.0
            )

        assert answer == 21.0
        assert "temperature offset model fix" not in caplog.text


def _quirk_answering_lowest_setpoint(answer):
    """A loaded quirk module whose ``lowest_setpoint`` answers ``answer``."""
    module = ModuleType("lowest_setpoint_quirk")
    module.lowest_setpoint = lambda _self, _entity_id, _min_temp: answer
    return module


class TestTheLowestSetpointShim:
    """The lowest setpoint is the TRV's minimum unless its quirk says otherwise."""

    def test_a_quirk_without_an_answer_keeps_the_minimum(self, caplog):
        """A module that does not define the function has no say on it."""
        host = _host(ModuleType("no_lowest_setpoint_quirk"))

        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            assert model_quirks.lowest_setpoint(host, ENTITY_ID, 5.0) == 5.0

        assert "lowest setpoint model fix" not in caplog.text

    def test_a_lifted_minimum_is_returned_and_logged(self, caplog):
        """The Shelly TRV reads its minimum back as off, so it is lifted a step."""
        host = _host(SHTRV_01)

        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            assert model_quirks.lowest_setpoint(host, ENTITY_ID, 4.0) == 4.5

        assert "lowest setpoint model fix: 4.0 to 4.5" in caplog.text

    def test_a_quirk_confirming_the_minimum_is_not_logged(self, caplog):
        """An answer equal to the minimum changes nothing worth a trace."""
        host = _host(_quirk_answering_lowest_setpoint(5.0))

        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            assert model_quirks.lowest_setpoint(host, ENTITY_ID, 5.0) == 5.0

        assert "lowest setpoint model fix" not in caplog.text


ANSWERING_PREDICATES = [
    "local_calibration_shifts_setpoint",
    "trv_state_unknown_as_available",
]


class TestOnlyADeclaredAnswerCounts:
    """A device answers these questions only through a quirk that defines them."""

    @pytest.mark.parametrize("name", ANSWERING_PREDICATES)
    def test_a_mock_that_makes_the_function_up_answers_no(self, name):
        """A record whose quirks are a bare mock reads as an unquirked device.

        A mock hands back a truthy mock for any function asked of it, which
        would otherwise mark every mocked TRV as operating while ``unknown``
        or as offsetting its setpoint.
        """
        host = _host(MagicMock())

        assert getattr(model_quirks, name)(host, ENTITY_ID) is False

    @pytest.mark.parametrize("name", ANSWERING_PREDICATES)
    def test_a_quirk_that_defines_the_function_is_asked(self, name):
        """The answer is the one the quirk gives."""
        module = ModuleType("answering_quirk")
        setattr(module, name, lambda _self, _entity_id: True)
        host = _host(module)

        assert getattr(model_quirks, name)(host, ENTITY_ID) is True
