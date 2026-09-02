"""Tests for the config flow's reads of what a device can be driven by.

A climate entity publishes its capability list in its own spelling, so both
the AUTO probe and the OFF gate must compare normalized. A device naming its
modes ``HVACMode.OFF`` offers OFF just as one naming them ``off`` does.

The calibration strategies the advanced step offers are the second such read,
and the one that goes past the entity: they follow from the write channels
behind it. A valve the device's own model quirk drives is such a channel, and
the adapter serving the ecosystem it is paired through reports none.
"""

import contextlib
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import CONF_NAME
from homeassistant.core import State
import pytest

from custom_components.better_thermostat.config_flow import (
    ConfigFlow,
    OptionsFlowHandler,
    _trv_supports_auto,
)
from custom_components.better_thermostat.utils.const import (
    CONF_CALIBRATION,
    CONF_HEATER,
    CalibrationType,
)


def _flow_with_modes(modes):
    """Build a flow whose single TRV reports the given mode list."""
    state = MagicMock()
    state.attributes = {"hvac_modes": modes}

    flow = MagicMock()
    flow.hass.states.get.return_value = state
    return flow


class TestTrvSupportsAuto:
    """The AUTO probe reads the device's own spelling."""

    def test_auto_offered_as_plain_string(self):
        """A plain ``auto`` is recognized."""
        assert _trv_supports_auto(_flow_with_modes(["off", "auto"]), "climate.trv")

    def test_auto_offered_in_the_device_spelling(self):
        """An ``HVACMode.AUTO`` spelling is recognized just the same."""
        assert _trv_supports_auto(
            _flow_with_modes(["HVACMode.OFF", "HVACMode.AUTO"]), "climate.trv"
        )

    def test_auto_absent_in_the_device_spelling(self):
        """A device genuinely without AUTO is still reported as lacking it."""
        assert not _trv_supports_auto(
            _flow_with_modes(["HVACMode.OFF", "HVACMode.HEAT"]), "climate.trv"
        )


def _advanced_flow(modes):
    """Build a ConfigFlow parked on the last TRV of a one-TRV bundle."""
    flow = ConfigFlow()
    flow.hass = MagicMock()
    state = MagicMock()
    state.attributes = {"hvac_modes": modes}
    flow.hass.states.get.return_value = state
    flow.i = 0
    flow.trv_bundle = [{"trv": "climate.trv", "advanced": {}}]
    flow._active_trv_config = flow.trv_bundle[0]
    return flow


async def _run_advanced(flow):
    """Submit the advanced step and return the confirm_type it hands on."""
    confirm = AsyncMock(return_value="confirm")
    with (
        patch(
            "custom_components.better_thermostat.config_flow._prepare_advanced_context",
            new=AsyncMock(
                return_value={
                    "trv_id": "climate.trv",
                    "default_calibration": "target_temp_based",
                    "homematic": False,
                    "has_auto": False,
                    "info": {},
                }
            ),
        ),
        patch(
            "custom_components.better_thermostat.config_flow._normalize_advanced_submission",
            return_value={},
        ),
        patch.object(ConfigFlow, "async_step_confirm", new=confirm),
    ):
        await flow.async_step_advanced({})
    return confirm.await_args[0][1] if len(confirm.await_args[0]) > 1 else None


class TestOffModeGate:
    """The OFF gate reads the device's own spelling."""

    @pytest.mark.asyncio
    async def test_off_offered_as_plain_string_passes_the_gate(self):
        """A plain ``off`` reaches confirm without the no_off_mode notice."""
        assert await _run_advanced(_advanced_flow(["off", "heat"])) is None

    @pytest.mark.asyncio
    async def test_off_offered_in_the_device_spelling_passes_the_gate(self):
        """An ``HVACMode.OFF`` spelling passes the gate just the same."""
        assert (
            await _run_advanced(_advanced_flow(["HVACMode.OFF", "HVACMode.HEAT"]))
            is None
        )

    @pytest.mark.asyncio
    async def test_device_without_off_still_raises_the_notice(self):
        """A device genuinely without OFF still gets the no_off_mode notice."""
        assert await _run_advanced(_advanced_flow(["HVACMode.HEAT"])) == "no_off_mode"


def test_hvac_mode_enum_members_are_recognized():
    """A list holding HVACMode members keeps working."""
    assert _trv_supports_auto(
        _flow_with_modes([HVACMode.OFF, HVACMode.AUTO]), "climate.trv"
    )


TRV_ID = "climate.trv"

# A model whose quirk module drives the valve itself, and one that is served
# by the default quirk module, which drives nothing.
QUIRK_BACKED_MODEL = "TRVZB"
UNQUIRKED_MODEL = "Generic"


def _adapter_without_a_valve_channel():
    """Build the capability answer an ecosystem without a valve gives.

    Every adapter that reads its channels off the entity answers this way for
    a device whose valve it cannot see, the generic one included.
    """
    adapter = MagicMock()
    adapter.get_info = AsyncMock(
        return_value={"support_offset": True, "support_valve": False}
    )
    return adapter


@contextlib.contextmanager
def _a_device_of_model(model):
    """Run the body against a Home Assistant holding one device of ``model``.

    The device registry reports the model, the quirk loader imports the
    modules that ship with the package, and the TRV is served by an adapter
    that publishes no valve channel.
    """
    entity_registry = MagicMock()
    entity_registry.async_get.return_value = SimpleNamespace(device_id="device")
    device_registry = MagicMock()
    device_registry.async_get.return_value = SimpleNamespace(
        manufacturer="Vendor",
        model=model,
        model_id=model,
        name=model,
        identifiers=set(),
    )
    with (
        patch(
            "custom_components.better_thermostat.utils.helpers.er.async_get",
            return_value=entity_registry,
        ),
        patch(
            "custom_components.better_thermostat.utils.helpers.dr.async_get",
            return_value=device_registry,
        ),
        patch(
            "custom_components.better_thermostat.model_fixes.model_quirks"
            ".async_import_module",
            new=AsyncMock(
                side_effect=lambda _hass, path: importlib.import_module(path)
            ),
        ),
        patch(
            "custom_components.better_thermostat.config_flow.load_adapter",
            autospec=True,
            return_value=_adapter_without_a_valve_channel(),
        ),
    ):
        yield


def _trv_bundle_entry(model):
    """Return the one device bundle a config entry carries for ``model``."""
    return {"trv": TRV_ID, "integration": "zha", "model": model, "advanced": {}}


def _hass_holding_the_trv():
    """Return a Home Assistant whose only entity is the TRV under test."""
    hass = MagicMock()
    hass.states.get.return_value = State(
        TRV_ID, "heat", {"hvac_modes": ["heat", "off"]}
    )
    return hass


def _offered_calibrations(form):
    """Return the calibration strategies an advanced form publishes."""
    schema = form["data_schema"].schema
    for marker in schema:
        if marker == CONF_CALIBRATION:
            return list(schema[marker].config["options"])
    raise AssertionError("the advanced step publishes no calibration field")


async def _create_flow_advanced_form(model):
    """Render the advanced step a new entry is configured through."""
    flow = ConfigFlow()
    flow.hass = _hass_holding_the_trv()
    flow.trv_bundle = [_trv_bundle_entry(model)]
    flow.i = 0
    with _a_device_of_model(model):
        return await flow.async_step_advanced(None, flow.trv_bundle[0])


async def _options_flow_advanced_form(model):
    """Render the advanced step an existing entry is reconfigured through."""
    entry = MagicMock()
    entry.data = {CONF_NAME: "Living Room", CONF_HEATER: [_trv_bundle_entry(model)]}
    flow = OptionsFlowHandler(entry)
    flow.hass = _hass_holding_the_trv()
    flow.trv_bundle = [_trv_bundle_entry(model)]
    flow.updated_config = dict(entry.data)
    with _a_device_of_model(model):
        return await flow.async_step_advanced(None, flow.trv_bundle[0], entry.data)


ADVANCED_FORMS = {
    "create": _create_flow_advanced_form,
    "options": _options_flow_advanced_form,
}


class TestOfferedCalibrationStrategies:
    """Direct valve control is offered where a valve can be written."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("render", ADVANCED_FORMS.values(), ids=ADVANCED_FORMS)
    async def test_a_quirk_driven_valve_is_offered(self, render):
        """A device whose model quirk drives the valve can be calibrated on it.

        The quirk reaches the valve through the entities the device's model
        carries, so it works under whichever adapter serves the ecosystem the
        device is paired through — including the generic fallback, which
        reports no valve channel of its own.
        """
        form = await render(QUIRK_BACKED_MODEL)

        assert CalibrationType.DIRECT_VALVE_BASED in _offered_calibrations(form)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("render", ADVANCED_FORMS.values(), ids=ADVANCED_FORMS)
    async def test_a_device_with_nothing_to_write_to_is_not_offered_it(self, render):
        """A device with neither channel is left off direct valve control.

        Its adapter has no valve to write to and its model has no quirk that
        would, so a thermostat put on the strategy would command positions
        that reach nothing.
        """
        form = await render(UNQUIRKED_MODEL)

        assert CalibrationType.DIRECT_VALVE_BASED not in _offered_calibrations(form)
