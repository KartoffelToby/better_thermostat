"""Creation and options enforce algorithms that produce a valve command."""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import State
import pytest

from custom_components.better_thermostat.config_flow import (
    ConfigFlow,
    OptionsFlowHandler,
)
from custom_components.better_thermostat.utils.const import (
    DEFAULT_CALIBRATION_MODE,
    CalibrationMode,
    CalibrationType,
)

FLOW = "custom_components.better_thermostat.config_flow"


def flow_context(kind):
    entry = MagicMock(data={"name": "Studio"})
    flow = ConfigFlow() if kind == "create" else OptionsFlowHandler(entry)
    flow.hass = MagicMock()
    flow.hass.states.get.return_value = State(
        "climate.trv", "heat", {"hvac_modes": ["heat"]}
    )
    flow.trv_bundle = [{"trv": "climate.trv", "integration": "shelly", "advanced": {}}]
    flow._active_trv_config = flow.trv_bundle[0]
    flow.data = {"name": "Studio"}
    flow.updated_config = {"name": "Studio"}
    return flow


@pytest.mark.parametrize("kind", ["create", "options"])
@pytest.mark.parametrize("algorithm", list(CalibrationMode) + [None])
async def test_algorithm_validation(kind, algorithm):
    flow = flow_context(kind)
    info = {
        "support_valve": True,
        "support_offset": False,
        "direct_valve_exclusive": True,
    }
    ctx = {
        "info": info,
        "default_calibration": "direct_valve_based",
        "homematic": False,
        "has_auto": False,
        "trv_id": "climate.trv",
    }
    data = {"calibration": CalibrationType.DIRECT_VALVE_BASED}
    if algorithm is not None:
        data["calibration_mode"] = algorithm
    rejected = algorithm in {
        CalibrationMode.DEFAULT,
        CalibrationMode.AGGRESIVE_CALIBRATION,
        CalibrationMode.NO_CALIBRATION,
    }
    with patch(f"{FLOW}._prepare_advanced_context", new=AsyncMock(return_value=ctx)):
        result = await flow.async_step_advanced(data)
    if rejected:
        assert result["step_id"] == "advanced"
        assert result["errors"] == {"calibration_mode": "exclusive_valve_algorithm"}
        assert flow.i == 0
        assert flow.trv_bundle[0]["advanced"] == {}
        flow.hass.config_entries.async_update_entry.assert_not_called()
    else:
        stored = flow.trv_bundle[0]["advanced"]
        assert stored["calibration_mode"] == (algorithm or DEFAULT_CALIBRATION_MODE)
        if kind == "create":
            assert result["step_id"] == "confirm"
            assert result["errors"] == {}  # No false minimum-temperature OFF notice.
        else:
            assert result["type"] == "create_entry"
            flow.hass.config_entries.async_update_entry.assert_called_once()


@pytest.mark.parametrize(
    "exclusive, calibration",
    [(False, "direct_valve_based"), (True, "target_temp_based")],
)
async def test_ordinary_config_still_accepts_normal_and_warns_about_off(
    exclusive, calibration
):
    flow = flow_context("create")
    ctx = {
        "info": {"support_valve": True, "direct_valve_exclusive": exclusive},
        "default_calibration": calibration,
        "homematic": False,
        "has_auto": False,
        "trv_id": "climate.trv",
    }
    with patch(f"{FLOW}._prepare_advanced_context", new=AsyncMock(return_value=ctx)):
        result = await flow.async_step_advanced(
            {"calibration": calibration, "calibration_mode": "default"}
        )
    assert result["step_id"] == "confirm"
    assert result["errors"] == {"base": "no_off_mode"}
