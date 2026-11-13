"""Two Matter heads apply less of their calibration offset than their entity takes.

Home Assistant's Matter integration publishes the local temperature
calibration of every Matter thermostat with the range the cluster allows,
-12.7 to 12.7 °C, and registers the device with its Matter product id as the
model id. The Aqara W600 (product id 18437) applies an offset from -5.0 to
5.0 °C only; the IKEA LILJEBAGGE (product id 20480) applies none at all.

The offset calibration computes each correction on top of the offset the
device holds, on the assumption that the reported temperature carries that
offset. On either head the reading stays where it was, so without the quirks
the offset runs to the end of the entity's range. With them Better
Thermostat keeps the W600 inside the range it applies, and neither offers
the offset calibration for the LILJEBAGGE nor writes an offset to it.
"""

from __future__ import annotations

from collections.abc import Mapping
import importlib
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import UnitOfTemperature
from homeassistant.core import State
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest

from custom_components.better_thermostat import config_flow
from custom_components.better_thermostat.adapters import delegate, generic
from custom_components.better_thermostat.adapters.base import (
    AdapterCapabilities,
    DeviceChannels,
)
from custom_components.better_thermostat.adapters.types import TrvAdapter
from custom_components.better_thermostat.calibration import calculate_calibration_local
from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.core.desired import DesiredState, TrvDesired
from custom_components.better_thermostat.core.safety import clamp
from custom_components.better_thermostat.core.snapshot import TrvReported
from custom_components.better_thermostat.model_fixes import default, model_quirks
from custom_components.better_thermostat.model_fixes.types import (
    IgnoredOffsetQuirk,
    ModelQuirks,
    OffsetConventionHost,
    OffsetRangeQuirk,
)
from custom_components.better_thermostat.utils.const import (
    CalibrationMode,
    CalibrationOutput,
)
from custom_components.better_thermostat.utils.helpers import get_device_model
from tests.factories import (
    DEFAULT_DEVICE_ID,
    ThermostatStandIn,
    make_entity_registry,
    make_registry_entry,
    make_snapshot,
    make_state,
    trv_record,
)


def _quirk_module(model: str) -> ModelQuirks:
    """Import the quirk module of a model whose name is no Python identifier."""
    module: object = importlib.import_module(
        f"custom_components.better_thermostat.model_fixes.{model}"
    )
    assert isinstance(module, ModelQuirks)
    return module


W600 = _quirk_module("18437")
LILJEBAGGE = _quirk_module("20480")

ENTITY_ID = "climate.radiator_thermostat"
OFFSET_ENTITY_ID = "number.radiator_thermostat_temperature_offset"
MATTER_LOWEST_OFFSET = -12.7
MATTER_HIGHEST_OFFSET = 12.7
MEASURED = 22.0
COLD_ROOM = 15.0
WARM_ROOM = 29.0


class SimulatedMatterHead:
    """A Matter head whose offset entity takes the whole range Matter allows.

    Parameters
    ----------
    lowest_applied, highest_applied : float
        The offsets the head applies to the temperature it reports. An
        offset outside them is held by the entity and changes nothing.
    """

    def __init__(self, *, lowest_applied: float, highest_applied: float) -> None:
        self.lowest_applied = lowest_applied
        self.highest_applied = highest_applied
        self.calibration_offset = 0.0
        self.applied_offset = 0.0
        self.written: list[float] = []

    def reading(self) -> float:
        """The temperature the head reports."""
        return round(MEASURED + self.applied_offset, 1)

    def offset_state(self) -> State:
        """The state its offset number entity publishes."""
        return State(
            OFFSET_ENTITY_ID,
            str(self.calibration_offset),
            {
                "min": MATTER_LOWEST_OFFSET,
                "max": MATTER_HIGHEST_OFFSET,
                "step": 0.1,
                "unit_of_measurement": UnitOfTemperature.CELSIUS,
            },
        )

    async def handle_service(
        self, domain: str, service: str, data: Mapping[str, object], **_kwargs: object
    ) -> None:
        """Take the offset a ``number.set_value`` call writes."""
        assert (domain, service) == ("number", "set_value")
        assert data["entity_id"] == OFFSET_ENTITY_ID
        self.calibration_offset = round(float(str(data["value"])), 1)
        self.written.append(self.calibration_offset)
        if self.lowest_applied <= self.calibration_offset <= self.highest_applied:
            self.applied_offset = self.calibration_offset


class _InvertedNarrowRangeQuirks:
    """A quirk surface for a head that negates its offset and applies -2 to 4 K."""

    fix_local_calibration = staticmethod(default.fix_local_calibration)
    fix_target_temperature_calibration = staticmethod(
        default.fix_target_temperature_calibration
    )
    override_set_hvac_mode = staticmethod(default.override_set_hvac_mode)
    override_set_temperature = staticmethod(default.override_set_temperature)
    MIN_CALIBRATION_OFFSET = -2.0
    MAX_CALIBRATION_OFFSET = 4.0

    @staticmethod
    def local_calibration_inverted(host: OffsetConventionHost, entity_id: str) -> bool:
        """The head subtracts its offset."""
        return True


def _w600() -> SimulatedMatterHead:
    return SimulatedMatterHead(lowest_applied=-5.0, highest_applied=5.0)


def _matter_device(manufacturer: str, model: str, product_id: str) -> dr.DeviceEntry:
    """A device registry entry the way Home Assistant's Matter integration makes it."""
    return dr.DeviceEntry(
        id=DEFAULT_DEVICE_ID,
        config_entry_id="matter_entry",
        identifiers={("matter", f"deviceid_0000000000000001-{product_id}")},
        manufacturer=manufacturer,
        model=model,
        model_id=product_id,
        name=model,
    )


def _host(
    monkeypatch: pytest.MonkeyPatch,
    head: SimulatedMatterHead,
    quirks: ModelQuirks,
    *,
    model: str = "18437",
    room: float = COLD_ROOM,
) -> ThermostatStandIn:
    """A thermostat driving ``head`` through the generic adapter."""
    entity_registry = make_entity_registry(
        make_registry_entry(ENTITY_ID, platform="matter"),
        make_registry_entry(
            OFFSET_ENTITY_ID, platform="matter", translation_key="temperature_offset"
        ),
    )
    monkeypatch.setattr(er, "async_get", lambda _hass: entity_registry)

    bt = ThermostatStandIn()
    bt.kernel_state = make_state()
    bt.name = "better_thermostat"
    bt.device_name = "Test BT"
    bt.tolerance = 0.0
    bt.attr_hvac_action = None
    bt.hvac_action = None
    bt.room_temperature = room
    bt.heat_target_temperature = room
    bt.context = None
    bt.hass = MagicMock()
    bt.hass.states.get = lambda requested: (
        head.offset_state() if requested == OFFSET_ENTITY_ID else None
    )
    bt.hass.services.async_call = AsyncMock(side_effect=head.handle_service)
    bt.real_trvs = {
        ENTITY_ID: trv_record(
            entity_id=ENTITY_ID,
            model=model,
            adapter=generic,
            model_quirks=quirks,
            calibration=CalibrationOutput.LOCAL_BASED,
            calibration_received=True,
            advanced={"calibration_mode": CalibrationMode.DEFAULT},
            local_temperature_calibration_entity=OFFSET_ENTITY_ID,
            current_temperature=head.reading(),
            local_calibration_step=0.1,
            target_temp_step=0.5,
            min_temp=5.0,
            max_temp=30.0,
        )
    }
    return bt


async def _start(bt: ThermostatStandIn) -> None:
    """Read the offset and its bounds the way startup does."""
    trv = bt.real_trvs[ENTITY_ID]
    trv.last_calibration = await delegate.get_calibration_offset(bt, ENTITY_ID)
    trv.min_local_calibration = await delegate.get_min_calibration_offset(bt, ENTITY_ID)
    trv.max_local_calibration = await delegate.get_max_calibration_offset(bt, ENTITY_ID)


def _through_the_safety_hull(bt: ThermostatStandIn, calibration_offset: float) -> float:
    """Clamp an offset to the TRV's bounds the way the command boundary does."""
    trv = bt.real_trvs[ENTITY_ID]
    snapshot = make_snapshot(
        trvs={
            ENTITY_ID: TrvReported(
                entity_id=ENTITY_ID,
                min_local_calibration=trv.min_local_calibration,
                max_local_calibration=trv.max_local_calibration,
            )
        }
    )
    desired = DesiredState(
        trvs={
            ENTITY_ID: TrvDesired(
                entity_id=ENTITY_ID, calibration_offset=calibration_offset
            )
        }
    )
    clamped = clamp(desired, snapshot).trvs[ENTITY_ID].calibration_offset
    assert clamped is not None
    return clamped


async def _control_cycle(bt: ThermostatStandIn, head: SimulatedMatterHead) -> None:
    """Calibrate once, write the offset and let the head report again."""
    trv = bt.real_trvs[ENTITY_ID]
    calibration_offset = calculate_calibration_local(bt, ENTITY_ID)
    assert calibration_offset is not None
    await delegate.set_calibration_offset(
        bt, ENTITY_ID, _through_the_safety_hull(bt, calibration_offset)
    )
    trv.current_temperature = head.reading()


class TestTheModelIsTheMatterProductId:
    """The quirk modules are named after the model id Matter registers."""

    @pytest.mark.parametrize(
        ("manufacturer", "model", "product_id"),
        [
            ("Aqara", "Radiator Thermostat W600", "18437"),
            ("IKEA of Sweden", "LILJEBAGGE radiator thermostat", "20480"),
        ],
    )
    @pytest.mark.asyncio
    async def test_the_product_id_is_the_model(self, manufacturer, model, product_id):
        device = _matter_device(manufacturer, model, product_id)
        entity_registry = make_entity_registry(
            make_registry_entry(ENTITY_ID, platform="matter")
        )
        device_registry = MagicMock(spec=dr.DeviceRegistry)
        device_registry.async_get.side_effect = {DEFAULT_DEVICE_ID: device}.get
        bt = ThermostatStandIn()
        bt.hass = MagicMock()
        bt.device_name = "Test BT"

        with (
            patch.object(er, "async_get", return_value=entity_registry),
            patch.object(dr, "async_get", return_value=device_registry),
        ):
            assert await get_device_model(bt, ENTITY_ID) == product_id

    @pytest.mark.parametrize(
        ("model", "module"), [("18437", W600), ("20480", LILJEBAGGE)]
    )
    @pytest.mark.asyncio
    async def test_the_loader_imports_the_module_of_the_product_id(self, model, module):
        bt = ThermostatStandIn()
        bt.device_name = "Test BT"

        async def _import(_hass: object, name: str) -> ModuleType:
            return importlib.import_module(name)

        with patch.object(model_quirks, "async_import_module", side_effect=_import):
            loaded = await model_quirks.load_model_quirks(bt, model, ENTITY_ID)

        assert loaded is module


class TestTheW600IsCalibratedInsideTheRangeItApplies:
    def test_the_quirk_declares_five_kelvin_either_way(self):
        quirks: object = W600
        assert isinstance(quirks, OffsetRangeQuirk)
        assert (quirks.MIN_CALIBRATION_OFFSET, quirks.MAX_CALIBRATION_OFFSET) == (
            -5.0,
            5.0,
        )

    @pytest.mark.asyncio
    async def test_the_bounds_are_the_range_the_head_applies(self, monkeypatch):
        bt = _host(monkeypatch, _w600(), W600)

        assert await delegate.get_min_calibration_offset(bt, ENTITY_ID) == -5.0
        assert await delegate.get_max_calibration_offset(bt, ENTITY_ID) == 5.0

    @pytest.mark.asyncio
    async def test_a_head_without_the_quirk_keeps_the_published_bounds(
        self, monkeypatch
    ):
        bt = _host(monkeypatch, _w600(), default)

        assert (
            await delegate.get_min_calibration_offset(bt, ENTITY_ID)
            == MATTER_LOWEST_OFFSET
        )
        assert (
            await delegate.get_max_calibration_offset(bt, ENTITY_ID)
            == MATTER_HIGHEST_OFFSET
        )

    @pytest.mark.parametrize(
        ("room", "bound"), [(COLD_ROOM, -5.0), (WARM_ROOM, 5.0)], ids=["cold", "warm"]
    )
    @pytest.mark.asyncio
    async def test_a_correction_beyond_the_range_rests_on_its_bound(
        self, monkeypatch, room, bound
    ):
        """Every offset written is one the head applies, and the last one holds.

        The room needs a correction of 7 K, more than the head applies; the
        offset goes to the bound, the head reports the reading it moved to,
        and the next cycles leave the offset there.
        """
        head = _w600()
        bt = _host(monkeypatch, head, W600, room=room)
        await _start(bt)

        for _cycle in range(5):
            await _control_cycle(bt, head)

        assert head.written
        assert all(-5.0 <= written <= 5.0 for written in head.written)
        assert head.calibration_offset == bound
        assert head.applied_offset == bound
        assert bt.real_trvs[ENTITY_ID].last_calibration == bound

    @pytest.mark.asyncio
    async def test_the_range_of_a_head_that_negates_its_offset_is_negated_too(
        self, monkeypatch
    ):
        """The quirk's bounds are the head's own, so they turn with its convention."""
        bt = _host(monkeypatch, _w600(), _InvertedNarrowRangeQuirks())

        assert await delegate.get_min_calibration_offset(bt, ENTITY_ID) == -4.0
        assert await delegate.get_max_calibration_offset(bt, ENTITY_ID) == 2.0

    @pytest.mark.parametrize(
        ("quirks", "left_in_place", "bounds"),
        [
            (W600, (-7.0, 7.0), (-5.0, 5.0)),
            (W600, (-3.0, 4.0), (-3.0, 4.0)),
            (default, (-7.0, 7.0), (-7.0, 7.0)),
            (_InvertedNarrowRangeQuirks(), (-7.0, 7.0), (-4.0, 2.0)),
        ],
        ids=["w600_unread", "w600_read", "default", "inverted_narrow"],
    )
    def test_the_startup_fallback_keeps_the_bounds_inside_the_range_the_head_applies(
        self, monkeypatch, quirks, left_in_place, bounds
    ):
        """A failed bound read leaves no bound the head ignores.

        Startup falls back when the offset or its bounds cannot be read, and
        the TRV then still holds the generic range it was created with.
        """
        bt = _host(monkeypatch, _w600(), quirks)
        trv = bt.real_trvs[ENTITY_ID]
        trv.min_local_calibration, trv.max_local_calibration = left_in_place

        BetterThermostat._set_trv_calibration_defaults(bt, ENTITY_ID)

        assert (trv.min_local_calibration, trv.max_local_calibration) == bounds

    def test_the_safety_hull_holds_a_w600_on_fallback_bounds_inside_its_range(
        self, monkeypatch
    ):
        bt = _host(monkeypatch, _w600(), W600)

        BetterThermostat._set_trv_calibration_defaults(bt, ENTITY_ID)

        assert _through_the_safety_hull(bt, 6.0) == 5.0
        assert _through_the_safety_hull(bt, -6.0) == -5.0


class TestTheLiljebaggeIsNotCalibratedByOffset:
    def test_the_quirk_declares_the_offset_ignored(self):
        quirks: object = LILJEBAGGE
        assert isinstance(quirks, IgnoredOffsetQuirk)
        assert model_quirks.quirk_ignores_calibration_offset(LILJEBAGGE) is True

    @pytest.mark.parametrize("quirks", [default, W600])
    def test_every_other_module_applies_the_offset(self, quirks):
        assert model_quirks.quirk_ignores_calibration_offset(quirks) is False

    def test_a_module_declaring_the_offset_applied_applies_it(self):
        quirks = SimpleNamespace(CALIBRATION_OFFSET_IGNORED=False)

        assert model_quirks.quirk_ignores_calibration_offset(quirks) is False

    @pytest.mark.asyncio
    async def test_no_offset_is_written_to_it(self, monkeypatch):
        head = SimulatedMatterHead(lowest_applied=1.0, highest_applied=-1.0)
        bt = _host(monkeypatch, head, LILJEBAGGE, model="20480")

        assert await delegate.set_calibration_offset(bt, ENTITY_ID, -3.0) is False

        assert head.written == []
        assert bt.real_trvs[ENTITY_ID].last_calibration_requested is None

    @pytest.mark.asyncio
    async def test_the_offset_does_not_run_away_over_cycles(self, monkeypatch):
        head = SimulatedMatterHead(lowest_applied=1.0, highest_applied=-1.0)
        bt = _host(monkeypatch, head, LILJEBAGGE, model="20480")
        await _start(bt)

        for _cycle in range(5):
            await _control_cycle(bt, head)

        assert head.calibration_offset == 0.0
        assert bt.real_trvs[ENTITY_ID].last_calibration == 0.0

    def test_its_offset_entity_is_no_offset_channel(self):
        trv = trv_record(
            entity_id=ENTITY_ID,
            adapter=generic,
            model_quirks=LILJEBAGGE,
            local_temperature_calibration_entity=OFFSET_ENTITY_ID,
        )

        assert trv.capabilities().supports_offset_write is False

    def test_the_offset_entity_of_another_head_is_an_offset_channel(self):
        trv = trv_record(
            entity_id=ENTITY_ID,
            adapter=generic,
            model_quirks=default,
            local_temperature_calibration_entity=OFFSET_ENTITY_ID,
        )

        assert trv.capabilities().supports_offset_write is True

    @pytest.mark.parametrize(
        ("quirks", "offered"),
        [(LILJEBAGGE, False), (default, True), (None, True)],
        ids=["liljebagge", "default", "unreadable_model"],
    )
    @pytest.mark.asyncio
    async def test_the_setup_form_offers_the_offset_calibration_only_where_it_applies(
        self, quirks, offered
    ):
        adapter = MagicMock(spec=TrvAdapter)
        adapter.get_info = AsyncMock(
            return_value=DeviceChannels(offset_write=True, valve_write=False)
        )

        with (
            patch.object(
                config_flow, "get_device_model", autospec=True, return_value="20480"
            ),
            patch.object(
                config_flow,
                "load_model_quirks",
                autospec=True,
                return_value=quirks,
                side_effect=None if quirks is not None else ImportError("no module"),
            ),
        ):
            _adapter, channels = await config_flow._load_adapter_info(
                config_flow.ConfigFlow(), "matter", ENTITY_ID, existing_adapter=adapter
            )

        assert channels.offset_write is offered
        assert (
            config_flow._default_calibration_for(channels)
            is CalibrationOutput.LOCAL_BASED
        ) is offered

    @pytest.mark.asyncio
    async def test_the_options_form_reads_the_stored_model_when_the_registry_has_none(
        self,
    ):
        """The form probes the model the running thermostat loads its quirk for."""
        adapter = MagicMock(spec=TrvAdapter)
        adapter.get_info = AsyncMock(
            return_value=DeviceChannels(offset_write=True, valve_write=False)
        )
        adapter.CAPABILITIES = AdapterCapabilities(offset_write=True)
        draft = config_flow._TrvDraft(
            entity_id=ENTITY_ID,
            integration="matter",
            adapter=adapter,
            stored={
                "trv": ENTITY_ID,
                "integration": "matter",
                "model": "20480",
                "adapter": None,
            },
        )
        flow = config_flow.ConfigFlow()
        flow.hass = MagicMock()
        flow.hass.states.get.return_value = None

        async def _registry_without_model(
            _flow: object, _entity_id: str, *, configured_model: str | None = None
        ) -> str:
            return configured_model or "generic"

        async def _quirks_of(_flow: object, model: str, _entity_id: str) -> ModelQuirks:
            return LILJEBAGGE if model == "20480" else default

        with (
            patch.object(
                config_flow,
                "get_device_model",
                autospec=True,
                side_effect=_registry_without_model,
            ),
            patch.object(
                config_flow, "load_model_quirks", autospec=True, side_effect=_quirks_of
            ),
            patch.object(
                config_flow,
                "regulates_on_bound_sensor",
                autospec=True,
                return_value=False,
            ),
        ):
            context = await config_flow._prepare_advanced_context(flow, draft)

        assert context.channels.offset_write is False
        assert context.default_calibration is CalibrationOutput.TARGET_TEMP_BASED

    @pytest.mark.asyncio
    async def test_the_valve_probe_reads_the_stored_model_when_the_registry_has_none(
        self,
    ):
        """The valve channel a quirk adds is found under the stored model too."""
        valve_quirks = object()
        adapter = MagicMock(spec=TrvAdapter)
        adapter.get_info = AsyncMock(
            return_value=DeviceChannels(offset_write=False, valve_write=False)
        )

        async def _registry_without_model(
            _flow: object, _entity_id: str, *, configured_model: str | None = None
        ) -> str:
            return configured_model or "generic"

        async def _quirks_of(_flow: object, model: str, _entity_id: str) -> object:
            return valve_quirks if model == "valve_head" else default

        with (
            patch.object(
                config_flow,
                "get_device_model",
                autospec=True,
                side_effect=_registry_without_model,
            ),
            patch.object(
                config_flow, "load_model_quirks", autospec=True, side_effect=_quirks_of
            ),
            patch.object(
                config_flow,
                "quirk_writes_valve",
                autospec=True,
                side_effect=lambda quirks: quirks is valve_quirks,
            ),
        ):
            _adapter, channels = await config_flow._load_adapter_info(
                config_flow.ConfigFlow(),
                "mqtt",
                ENTITY_ID,
                existing_adapter=adapter,
                configured_model="valve_head",
            )

        assert channels.valve_write is True
