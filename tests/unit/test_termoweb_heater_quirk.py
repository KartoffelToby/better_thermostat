"""A TermoWeb heater holds its temperature offset with the opposite sign.

Better Thermostat's offset is what a device adds to the temperature it
measures before it reports the reading. A TermoWeb electric radiator, which
the ha-termoweb integration registers as model ``Heater`` of manufacturer
``TermoWeb``, subtracts its offset instead: at an offset of +1 K a heater
measuring 20 °C reports 19 °C. Better Thermostat reads, bounds and writes
that offset in its own convention all the same, so the heater lands on the
room temperature. Any other device registered as ``Heater`` keeps the
convention every device has.
"""

from __future__ import annotations

from collections.abc import Mapping
from unittest.mock import AsyncMock, MagicMock

from homeassistant.const import STATE_UNKNOWN, UnitOfTemperature
from homeassistant.core import State
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest

from custom_components.better_thermostat.adapters import delegate, generic
from custom_components.better_thermostat.calibration import calculate_calibration_local
from custom_components.better_thermostat.model_fixes import Heater, default
from custom_components.better_thermostat.model_fixes.model_quirks import (
    local_calibration_inverted,
)
from custom_components.better_thermostat.model_fixes.types import ModelQuirks
from custom_components.better_thermostat.utils import controlling
from custom_components.better_thermostat.utils.const import (
    CalibrationMode,
    CalibrationOutput,
)
from tests.factories import (
    DEFAULT_DEVICE_ID,
    ThermostatStandIn,
    make_entity_registry,
    make_registry_entry,
    make_state,
    trv_record,
)

ENTITY_ID = "climate.heater"
OFFSET_ENTITY_ID = "number.heater_temperature_offset"
ROOM = 21.0
MEASURED = 20.0
LOWEST_CALIBRATION_OFFSET = -3.0
HIGHEST_CALIBRATION_OFFSET = 3.0


class SimulatedHeater:
    """A heater that reports its measured temperature with its offset applied.

    Parameters
    ----------
    subtracts : bool
        True for a TermoWeb heater, which subtracts the offset it holds;
        False for a device that adds it.
    calibration_offset : float or None
        Offset the heater holds; None while nothing was ever written to it,
        which its number entity publishes as ``unknown``.
    """

    def __init__(self, *, subtracts: bool, calibration_offset: float | None) -> None:
        self.subtracts = subtracts
        self.calibration_offset = calibration_offset
        self.lowest_calibration_offset = LOWEST_CALIBRATION_OFFSET
        self.highest_calibration_offset = HIGHEST_CALIBRATION_OFFSET

    def reading(self) -> float:
        """The temperature the heater reports."""
        held = self.calibration_offset or 0.0
        return MEASURED - held if self.subtracts else MEASURED + held

    def offset_state(self) -> State:
        """The state its offset number entity publishes."""
        return State(
            OFFSET_ENTITY_ID,
            STATE_UNKNOWN
            if self.calibration_offset is None
            else str(self.calibration_offset),
            {
                "min": self.lowest_calibration_offset,
                "max": self.highest_calibration_offset,
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


def _device(manufacturer: str | None) -> dr.DeviceEntry:
    return dr.DeviceEntry(
        id=DEFAULT_DEVICE_ID,
        config_entry_id="termoweb_entry",
        identifiers={("termoweb", "gateway_1:htr:1")},
        manufacturer=manufacturer,
        model="Heater",
    )


def _host(
    monkeypatch: pytest.MonkeyPatch,
    heater: SimulatedHeater,
    *,
    manufacturer: str | None = "TermoWeb",
    quirks: ModelQuirks = Heater,
    on_a_device: bool = True,
) -> ThermostatStandIn:
    """A thermostat driving ``heater`` through the generic adapter."""
    device = _device(manufacturer)
    entity_registry = make_entity_registry(
        make_registry_entry(
            ENTITY_ID,
            platform="termoweb",
            device_id=DEFAULT_DEVICE_ID if on_a_device else None,
        ),
        make_registry_entry(
            OFFSET_ENTITY_ID,
            platform="termoweb",
            translation_key="temperature_offset",
            device_id=DEFAULT_DEVICE_ID if on_a_device else None,
        ),
    )
    device_registry = MagicMock(spec=dr.DeviceRegistry)
    device_registry.async_get.side_effect = {DEFAULT_DEVICE_ID: device}.get
    monkeypatch.setattr(er, "async_get", lambda _hass: entity_registry)
    monkeypatch.setattr(dr, "async_get", lambda _hass: device_registry)

    bt = ThermostatStandIn()
    bt.kernel_state = make_state()
    bt.name = "better_thermostat"
    bt.device_name = "Test BT"
    bt.tolerance = 0.5
    bt.attr_hvac_action = None
    bt.hvac_action = None
    bt.room_temperature = ROOM
    bt.heat_target_temperature = ROOM
    bt.context = None
    bt.hass = MagicMock()
    bt.hass.states.get = lambda requested: (
        heater.offset_state() if requested == OFFSET_ENTITY_ID else None
    )
    bt.hass.services.async_call = AsyncMock(side_effect=heater.handle_service)
    bt.real_trvs = {
        ENTITY_ID: trv_record(
            entity_id=ENTITY_ID,
            model="Heater",
            adapter=generic,
            model_quirks=quirks,
            calibration=CalibrationOutput.LOCAL_BASED,
            calibration_received=True,
            advanced={"calibration_mode": CalibrationMode.DEFAULT},
            local_temperature_calibration_entity=OFFSET_ENTITY_ID,
            current_temperature=heater.reading(),
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


async def _control_cycle(bt: ThermostatStandIn, heater: SimulatedHeater) -> float:
    """Calibrate once, write the offset and let the heater report again."""
    trv = bt.real_trvs[ENTITY_ID]
    calibration_offset = calculate_calibration_local(bt, ENTITY_ID)
    assert calibration_offset is not None
    assert await delegate.set_calibration_offset(bt, ENTITY_ID, calibration_offset)
    trv.current_temperature = heater.reading()
    return calibration_offset


class TestOnlyATermoWebHeaterIsInverted:
    def test_a_termoweb_heater_answers_that_it_negates_its_offset(self, monkeypatch):
        bt = _host(monkeypatch, SimulatedHeater(subtracts=True, calibration_offset=0.0))

        assert local_calibration_inverted(bt, ENTITY_ID) is True

    def test_a_heater_of_another_manufacturer_adds_its_offset(self, monkeypatch):
        """``Heater`` is a model name any integration may register."""
        bt = _host(
            monkeypatch,
            SimulatedHeater(subtracts=False, calibration_offset=0.0),
            manufacturer="Acme",
        )

        assert local_calibration_inverted(bt, ENTITY_ID) is False

    def test_a_heater_naming_no_manufacturer_adds_its_offset(self, monkeypatch):
        bt = _host(
            monkeypatch,
            SimulatedHeater(subtracts=False, calibration_offset=0.0),
            manufacturer=None,
        )

        assert local_calibration_inverted(bt, ENTITY_ID) is False

    def test_an_entity_without_a_device_adds_its_offset(self, monkeypatch):
        bt = _host(
            monkeypatch,
            SimulatedHeater(subtracts=False, calibration_offset=0.0),
            on_a_device=False,
        )

        assert local_calibration_inverted(bt, ENTITY_ID) is False

    def test_a_module_without_the_answer_adds_its_offset(self, monkeypatch):
        bt = _host(
            monkeypatch,
            SimulatedHeater(subtracts=True, calibration_offset=0.0),
            quirks=default,
        )

        assert local_calibration_inverted(bt, ENTITY_ID) is False


class TestTheOffsetIsReadInBetterThermostatsConvention:
    @pytest.mark.asyncio
    async def test_the_offset_a_termoweb_heater_holds_is_read_negated(
        self, monkeypatch
    ):
        bt = _host(monkeypatch, SimulatedHeater(subtracts=True, calibration_offset=1.0))

        assert await delegate.get_calibration_offset(bt, ENTITY_ID) == -1.0

    @pytest.mark.asyncio
    async def test_an_offset_never_written_reads_as_zero(self, monkeypatch):
        """The heater's offset number publishes ``unknown`` until first written."""
        bt = _host(
            monkeypatch, SimulatedHeater(subtracts=True, calibration_offset=None)
        )

        calibration_offset = await delegate.get_calibration_offset(bt, ENTITY_ID)

        assert calibration_offset == 0.0
        assert str(calibration_offset) == "0.0"

    @pytest.mark.asyncio
    async def test_the_bounds_are_the_negated_bounds_of_the_heater(self, monkeypatch):
        """The lowest offset the heater takes is the highest one in BT's terms."""
        heater = SimulatedHeater(subtracts=True, calibration_offset=0.0)
        heater.lowest_calibration_offset = -2.0
        heater.highest_calibration_offset = 3.0
        bt = _host(monkeypatch, heater)

        assert await delegate.get_min_calibration_offset(bt, ENTITY_ID) == -3.0
        assert await delegate.get_max_calibration_offset(bt, ENTITY_ID) == 2.0

    @pytest.mark.asyncio
    async def test_another_heater_is_read_as_it_reports(self, monkeypatch):
        heater = SimulatedHeater(subtracts=False, calibration_offset=1.0)
        heater.lowest_calibration_offset = -2.0
        heater.highest_calibration_offset = 3.0
        bt = _host(monkeypatch, heater, manufacturer="Acme")

        assert await delegate.get_calibration_offset(bt, ENTITY_ID) == 1.0
        assert await delegate.get_min_calibration_offset(bt, ENTITY_ID) == -2.0
        assert await delegate.get_max_calibration_offset(bt, ENTITY_ID) == 3.0

    @pytest.mark.asyncio
    async def test_a_device_without_the_hook_is_read_as_it_reports(self, monkeypatch):
        heater = SimulatedHeater(subtracts=False, calibration_offset=1.0)
        heater.lowest_calibration_offset = -2.0
        bt = _host(monkeypatch, heater, quirks=default)

        assert await delegate.get_calibration_offset(bt, ENTITY_ID) == 1.0
        assert await delegate.get_min_calibration_offset(bt, ENTITY_ID) == -2.0
        assert await delegate.get_max_calibration_offset(bt, ENTITY_ID) == 3.0


class TestTheOffsetIsWrittenInTheHeatersConvention:
    @pytest.mark.asyncio
    async def test_a_termoweb_heater_is_written_the_negated_offset(self, monkeypatch):
        """The record keeps BT's offset; the heater holds its negation."""
        heater = SimulatedHeater(subtracts=True, calibration_offset=0.0)
        bt = _host(monkeypatch, heater)
        trv = bt.real_trvs[ENTITY_ID]

        assert await delegate.set_calibration_offset(bt, ENTITY_ID, 1.0)

        assert heater.calibration_offset == -1.0
        assert trv.last_calibration == 1.0
        assert trv.last_calibration_requested == 1.0

    @pytest.mark.asyncio
    async def test_the_heaters_clamp_is_recorded_in_bts_terms(self, monkeypatch):
        """A heater taking -2 K at the lowest holds BT's +2 K at the highest."""
        heater = SimulatedHeater(subtracts=True, calibration_offset=0.0)
        heater.lowest_calibration_offset = -2.0
        bt = _host(monkeypatch, heater)
        trv = bt.real_trvs[ENTITY_ID]

        assert await delegate.set_calibration_offset(bt, ENTITY_ID, 2.5)

        assert heater.calibration_offset == -2.0
        assert trv.last_calibration == 2.0
        assert trv.last_calibration_requested == 2.5

    @pytest.mark.asyncio
    async def test_a_write_that_raises_leaves_the_record_in_bts_terms(
        self, monkeypatch
    ):
        heater = SimulatedHeater(subtracts=True, calibration_offset=0.0)
        bt = _host(monkeypatch, heater)
        trv = bt.real_trvs[ENTITY_ID]
        trv.last_calibration = 1.5
        bt.hass.services.async_call = AsyncMock(side_effect=ConnectionError("gone"))
        monkeypatch.setattr("asyncio.sleep", AsyncMock())

        assert not await delegate.set_calibration_offset(bt, ENTITY_ID, 1.0)

        assert trv.last_calibration == 1.5

    @pytest.mark.asyncio
    async def test_the_record_is_in_bts_terms_while_the_write_is_under_way(
        self, monkeypatch
    ):
        """The adapter waits for the heater's mode after the offset went out.

        Whatever reads the record while the adapter waits, a calibration
        or the reconciler, finds BT's offset, before the write and after.
        """
        heater = SimulatedHeater(subtracts=True, calibration_offset=-0.5)
        bt = _host(monkeypatch, heater)
        trv = bt.real_trvs[ENTITY_ID]
        trv.last_calibration = 0.5
        trv.last_hvac_mode = "heat"
        bt.hass.states.get = lambda requested: (
            heater.offset_state()
            if requested == OFFSET_ENTITY_ID
            else State(ENTITY_ID, "heat")
        )
        seen_on_the_wire: list[float] = []
        seen_while_waiting: list[float] = []

        async def write_and_look(
            domain: str, service: str, data: Mapping[str, object], **_kwargs: object
        ) -> None:
            seen_on_the_wire.append(trv.last_calibration)
            await heater.handle_service(domain, service, data)

        async def wait_and_look(_seconds: float) -> None:
            seen_while_waiting.append(trv.last_calibration)

        bt.hass.services.async_call = AsyncMock(side_effect=write_and_look)
        monkeypatch.setattr(generic.asyncio, "sleep", wait_and_look)

        assert await delegate.set_calibration_offset(bt, ENTITY_ID, 1.0)

        assert seen_on_the_wire == [0.5]
        assert seen_while_waiting == [1.0]
        assert heater.calibration_offset == -1.0
        assert trv.last_calibration == 1.0

    @pytest.mark.asyncio
    async def test_a_heater_without_an_offset_entity_keeps_the_record(
        self, monkeypatch
    ):
        """An adapter that finds nothing to write to leaves the record alone."""
        heater = SimulatedHeater(subtracts=True, calibration_offset=0.0)
        bt = _host(monkeypatch, heater)
        trv = bt.real_trvs[ENTITY_ID]
        trv.local_temperature_calibration_entity = None
        trv.last_calibration = 1.5

        assert not await delegate.set_calibration_offset(bt, ENTITY_ID, 1.0)

        assert trv.last_calibration == 1.5
        assert heater.calibration_offset == 0.0

    @pytest.mark.asyncio
    async def test_another_heater_is_written_the_offset_as_asked(self, monkeypatch):
        heater = SimulatedHeater(subtracts=False, calibration_offset=0.0)
        bt = _host(monkeypatch, heater, manufacturer="Acme")
        trv = bt.real_trvs[ENTITY_ID]

        assert await delegate.set_calibration_offset(bt, ENTITY_ID, 1.0)

        assert heater.calibration_offset == 1.0
        assert trv.last_calibration == 1.0

    @pytest.mark.asyncio
    async def test_a_device_without_the_hook_is_written_as_asked(self, monkeypatch):
        heater = SimulatedHeater(subtracts=False, calibration_offset=0.0)
        bt = _host(monkeypatch, heater, quirks=default)

        assert await delegate.set_calibration_offset(bt, ENTITY_ID, 1.0)

        assert heater.calibration_offset == 1.0


class TestTheHeaterSettlesOnTheRoomTemperature:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("starting_offset", [None, 0.0, 1.0, -1.0])
    async def test_the_heater_reports_the_room_temperature(
        self, monkeypatch, starting_offset
    ):
        """The room is at 21 °C and the heater measures 20 °C.

        Whatever offset the heater starts from, it reports 21 °C after one
        control cycle, which takes it holding an offset of -1 K.
        """
        heater = SimulatedHeater(subtracts=True, calibration_offset=starting_offset)
        bt = _host(monkeypatch, heater)
        await _start(bt)

        calibration_offset = await _control_cycle(bt, heater)

        assert calibration_offset == pytest.approx(ROOM - MEASURED)
        assert heater.calibration_offset == pytest.approx(-1.0)
        assert heater.reading() == pytest.approx(ROOM)

    @pytest.mark.asyncio
    async def test_the_offset_holds_over_later_cycles(self, monkeypatch):
        heater = SimulatedHeater(subtracts=True, calibration_offset=1.0)
        bt = _host(monkeypatch, heater)
        await _start(bt)

        written = [await _control_cycle(bt, heater) for _ in range(3)]

        assert written == pytest.approx([ROOM - MEASURED] * 3)
        assert heater.reading() == pytest.approx(ROOM)

    @pytest.mark.asyncio
    async def test_another_heater_settles_with_the_offset_added(self, monkeypatch):
        heater = SimulatedHeater(subtracts=False, calibration_offset=-1.0)
        bt = _host(monkeypatch, heater, manufacturer="Acme")
        await _start(bt)

        await _control_cycle(bt, heater)

        assert heater.calibration_offset == pytest.approx(1.0)
        assert heater.reading() == pytest.approx(ROOM)


class TestTheReconcilerComparesInBetterThermostatsConvention:
    def test_a_heater_holding_the_negated_command_has_not_diverged(self, monkeypatch):
        heater = SimulatedHeater(subtracts=True, calibration_offset=-1.0)
        bt = _host(monkeypatch, heater)
        trv = bt.real_trvs[ENTITY_ID]
        trv.last_calibration = 1.0

        assert controlling._offset_diverges(bt, trv) is False

    def test_a_heater_holding_the_command_itself_has_diverged(self, monkeypatch):
        heater = SimulatedHeater(subtracts=True, calibration_offset=1.0)
        bt = _host(monkeypatch, heater)
        trv = bt.real_trvs[ENTITY_ID]
        trv.last_calibration = 1.0

        assert controlling._offset_diverges(bt, trv) is True
