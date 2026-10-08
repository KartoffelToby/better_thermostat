"""Every model quirk module answers to the Protocols in ``model_fixes/types.py``.

The loader and the dispatch narrow a quirk module to those Protocols with
``isinstance``, which only sees whether a name is there. The signatures are
held by the type checker instead, through the ``_Surface`` class each module
binds to every Protocol it implements. A module function missing from its
``_Surface``, or a Protocol the module satisfies without a binding, would
escape that check, so both are pinned here. So is what the loader does with
a module that lacks part of the surface every quirk module provides.
"""

import annotationlib
import importlib
import logging
import pkgutil
from types import ModuleType
import typing
from unittest.mock import MagicMock, patch

import pytest

from custom_components.better_thermostat import model_fixes
from custom_components.better_thermostat.model_fixes import (
    default as default_quirk,
    model_quirks,
)
from custom_components.better_thermostat.model_fixes.types import (
    ExternalTemperatureQuirk,
    InitialTweakQuirk,
    LowestSetpointQuirk,
    MaintenanceIntervalQuirk,
    ModelQuirks,
    SetpointOffsetQuirk,
    UnknownStateQuirk,
    ValveQuirk,
)
from tests.factories import ThermostatStandIn

PACKAGE = model_fixes.__name__
NOT_A_MODEL = {"model_quirks", "types"}
ENTITY_ID = "climate.trv"

# Everything the dispatch narrows a quirk module to.
PROTOCOLS = (
    ModelQuirks,
    ExternalTemperatureQuirk,
    InitialTweakQuirk,
    LowestSetpointQuirk,
    MaintenanceIntervalQuirk,
    SetpointOffsetQuirk,
    UnknownStateQuirk,
    ValveQuirk,
)

MODEL_IDS = sorted(
    info.name
    for info in pkgutil.iter_modules(model_fixes.__path__)
    if info.name not in NOT_A_MODEL
)


def _module(model):
    """Import the quirk module of one model."""
    return importlib.import_module(f"{PACKAGE}.{model}")


def _protocols_satisfied(module):
    """Names of the Protocols a module satisfies at runtime."""
    return {protocol.__name__ for protocol in PROTOCOLS if isinstance(module, protocol)}


class TestEveryModuleAnswersToTheProtocols:
    """The runtime surface and the statically checked one are the same."""

    def test_every_model_is_found(self):
        """The scan below reaches the default module and the hyphenated ones."""
        assert {"default", "TRVZB", "TRV-ZBT", "SHTRV-01"} <= set(MODEL_IDS)

    @pytest.mark.parametrize("model", MODEL_IDS)
    def test_the_required_surface_is_provided(self, model):
        """The dispatch calls these without asking first."""
        assert isinstance(_module(model), ModelQuirks)

    @pytest.mark.parametrize("model", MODEL_IDS)
    def test_the_surface_class_lists_every_member_the_module_offers(self, model):
        """Each member the module offers is held to the signature of its Protocol."""
        module = _module(model)
        offered = {
            name
            for protocol in PROTOCOLS
            for name in typing.get_protocol_members(protocol)
            if hasattr(module, name)
        }
        listed = {name for name in vars(module._Surface) if not name.startswith("__")}

        assert listed == offered
        for name in listed:
            assert getattr(module._Surface, name) == getattr(module, name), name

    @pytest.mark.parametrize("model", MODEL_IDS)
    def test_every_protocol_the_module_satisfies_is_bound(self, model):
        """A satisfied Protocol without a binding goes unchecked by the type checker."""
        module = _module(model)
        annotations = annotationlib.get_annotations(
            module, format=annotationlib.Format.STRING
        )
        protocol_names = {protocol.__name__ for protocol in PROTOCOLS}
        bound = {
            annotation
            for annotation in annotations.values()
            if annotation in protocol_names
        }

        assert bound == _protocols_satisfied(module)


def _host():
    """A loader host whose Home Assistant core is never reached."""
    host = ThermostatStandIn()
    host.device_name = "Test BT"
    host.hass = MagicMock()
    return host


def _partial_module():
    """A module that imports but carries only part of the required surface."""
    module = ModuleType("partial_quirk")
    module.fix_local_calibration = default_quirk.fix_local_calibration
    return module


class TestTheLoaderPassesOverANonconformingModule:
    """A module lacking part of the surface is treated as one that does not import."""

    @pytest.mark.asyncio
    async def test_the_default_module_stands_in(self, caplog):
        """Every call through the partial module would fail mid control cycle."""

        async def _import(_hass, name):
            if name.endswith(".Partial-TRV"):
                return _partial_module()
            return importlib.import_module(name)

        with (
            patch.object(model_quirks, "async_import_module", side_effect=_import),
            caplog.at_level(logging.DEBUG, logger=model_quirks.__name__),
        ):
            module = await model_quirks.load_model_quirks(
                _host(), "Partial-TRV", ENTITY_ID
            )

        assert module is default_quirk
        assert "lacks part of the model quirk surface" in caplog.text
        assert "using default" in caplog.text

    @pytest.mark.asyncio
    async def test_a_conforming_module_is_returned_as_imported(self):
        """The check leaves a module with the full surface untouched."""

        async def _import(_hass, name):
            return importlib.import_module(name)

        with patch.object(model_quirks, "async_import_module", side_effect=_import):
            module = await model_quirks.load_model_quirks(_host(), "TRVZB", ENTITY_ID)

        assert module is _module("TRVZB")

    @pytest.mark.asyncio
    async def test_a_nonconforming_default_module_surfaces(self, caplog):
        """Below the default module nothing is left to fall back to."""

        async def _import(_hass, _name):
            return _partial_module()

        with (
            patch.object(model_quirks, "async_import_module", side_effect=_import),
            caplog.at_level(logging.ERROR, logger=model_quirks.__name__),
            pytest.raises(ImportError, match="model_fixes.default"),
        ):
            await model_quirks.load_model_quirks(_host(), "Partial-TRV", ENTITY_ID)

        assert "failed to import default quirks module" in caplog.text
