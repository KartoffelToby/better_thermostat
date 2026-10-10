"""The options flow marks only the form of the last thermostat as the last step.

The advanced step repeats once per thermostat. Home Assistant labels the
submit button of a form that ends the flow differently from one that leads
on, so only the last thermostat's form may carry ``last_step``.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import CONF_NAME
import pytest

from custom_components.better_thermostat.config_flow import (
    OptionsFlowHandler,
    _AdvancedContext,
    _TrvDraft,
)
from custom_components.better_thermostat.utils.const import CONF_THERMOSTAT

ADVANCED_CONTEXT = _AdvancedContext(
    entity_id="climate.trv",
    info={},
    default_calibration="target_temp_based",
    homematic=False,
    has_auto=False,
)


def _stored(count: int) -> list[dict[str, object]]:
    return [
        {"trv": f"climate.trv{index}", "integration": "generic", "advanced": {}}
        for index in range(count)
    ]


def _drafts(count: int) -> list[_TrvDraft]:
    return [
        _TrvDraft(
            entity_id=f"climate.trv{index}",
            integration="generic",
            adapter=None,
            stored=stored,
        )
        for index, stored in enumerate(_stored(count))
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 2, 3])
async def test_only_the_last_thermostat_form_is_the_last_step(count: int):
    """Every advanced form but the last one leads on to another form."""
    entry = MagicMock()
    entry.data = {CONF_NAME: "Room", CONF_THERMOSTAT: _stored(count)}
    flow = OptionsFlowHandler(entry)
    flow.hass = MagicMock()
    flow.trv_bundle = _drafts(count)
    flow.updated_config = dict(entry.data)

    flags = []
    with patch(
        "custom_components.better_thermostat.config_flow._prepare_advanced_context",
        new=AsyncMock(return_value=ADVANCED_CONTEXT),
    ):
        form = await flow.async_step_advanced(None, flow.trv_bundle[0])
        flags.append(form["last_step"])
        for _ in range(count - 1):
            form = await flow.async_step_advanced({"calibration": "target_temp_based"})
            assert form["step_id"] == "advanced"
            flags.append(form["last_step"])

    assert flags == [False] * (count - 1) + [True]
