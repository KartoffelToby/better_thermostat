"""What a quirk answers when the device turns its write down.

Every command a quirk issues is a blocking service call, and Home Assistant
answers one with an exception whenever the device cannot take it: a battery
device that is asleep, a node out of range, an integration reloading its
config entry, a missing service, or an entity that does not carry the
option or the attribute the payload names.

The quirks sit deep inside one control cycle, and they hold the lock that
serialises every TRV of the instance while they run. An error escaping one
ends that cycle for the TRV before its calibration and its setpoint are
written and before the confirmation watchdogs are armed, and the control
queue re-queues the failed cycle immediately, with nothing between the
attempts. A device that refuses one write on every cycle therefore turns the
loop into a spin. The generic adapter, which is what drives a TRV without a
quirk, does not behave that way: it reports a refusal and leaves the caller
to decide.

So each of these functions reports a refused write through its return
value. ``False`` says no command reached the device, which is what routes
the caller to the standard adapter path and its retry handling; ``True``
stays reserved for a command that went out. A supplementary device setting
is the one exception: it is reported in the log, but it does not decide
whether the command the caller asked for is written.
"""

from importlib import import_module
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.climate.const import ClimateEntityFeature
from homeassistant.const import UnitOfTemperature
from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import pytest

from custom_components.better_thermostat.model_fixes import (
    SPZB0001 as spzb0001_quirk,
    ZWA021 as zwa021_quirk,
)
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.const import CalibrationType

# The device name carries a hyphen, so the module is reached by name.
TV02 = import_module("custom_components.better_thermostat.model_fixes.TV02-Zigbee")
BTH_RM = import_module("custom_components.better_thermostat.model_fixes.BTH-RM")
BTH_RM230Z = import_module("custom_components.better_thermostat.model_fixes.BTH-RM230Z")

ENTITY_ID = "climate.trv"
RANGE_BIT = int(ClimateEntityFeature.TARGET_TEMPERATURE_RANGE)

# One instance per way a device says no. ``ServiceValidationError`` is what
# Home Assistant itself raises for an unsupported mode, an unsupported
# option and a service nobody registered, and it derives from
# ``HomeAssistantError``, so catching the base class covers all three.
# ``OSError`` is the transport underneath giving out.
REFUSALS = [
    HomeAssistantError("device did not answer"),
    ServiceValidationError("the entity does not carry that option"),
    OSError("connection reset"),
]
REFUSAL_IDS = ["unreachable", "unsupported", "transport"]


def _host(state=None, model=None, advanced=None):
    """A Better Thermostat stand-in whose every service call is refused."""
    host = MagicMock()
    host.device_name = "Test BT"
    host.context = None
    host.hass = MagicMock()
    host.hass.config.units.temperature_unit = UnitOfTemperature.CELSIUS
    host.hass.states.get.return_value = state
    host.hass.services.async_call = AsyncMock()
    trv = Trv(entity_id=ENTITY_ID)
    trv.model = model
    if advanced is not None:
        trv.advanced = advanced
    host.real_trvs = {ENTITY_ID: trv}
    return host


def _climate_state(supported_features):
    """A live climate state declaring the given feature bitmask."""
    return State(
        ENTITY_ID, "heat", {"supported_features": supported_features, "temperature": 20}
    )


class TestTheBoschRoomThermostatsDeclineARefusedSetpoint:
    """Both Bosch modules write the setpoint themselves, on three branches.

    Which attributes go out depends on what the entity declares, and any of
    the three payloads can come back refused. The range write is the
    likeliest of the three, because it is the one naming attributes an
    entity may not support at the moment it is asked.
    """

    @pytest.fixture(params=[BTH_RM, BTH_RM230Z], ids=["BTH-RM", "BTH-RM230Z"])
    def quirk(self, request):
        """Each Bosch room-thermostat quirk module in turn."""
        return request.param

    @pytest.mark.parametrize(
        "state",
        [None, _climate_state(RANGE_BIT), _climate_state(0)],
        ids=["no_state", "range_write", "plain_write"],
    )
    @pytest.mark.parametrize("refusal", REFUSALS, ids=REFUSAL_IDS)
    @pytest.mark.asyncio
    async def test_a_refused_setpoint_is_declined_on_every_branch(
        self, quirk, state, refusal
    ):
        """Whichever payload the entity earns, a refusal answers False."""
        host = _host(state=state)
        host.hass.services.async_call = AsyncMock(side_effect=refusal)

        assert await quirk.override_set_temperature(host, ENTITY_ID, 21.0) is False
        host.hass.services.async_call.assert_awaited_once()


class TestTheTuyaValveDeclinesARefusedCommand:
    """TV02-Zigbee writes the mode and the setpoint itself.

    It also puts the device on its manual preset, so the TRV does not fall
    back to its own schedule and overwrite what it was just told. That
    preset is a device setting, not the command the caller asked for.
    """

    @pytest.mark.parametrize("refusal", REFUSALS, ids=REFUSAL_IDS)
    @pytest.mark.asyncio
    async def test_a_refused_mode_write_is_declined(self, refusal):
        """No mode reached the device, so the adapter write has to carry it."""
        host = _host(model="TV02-Zigbee")
        host.hass.services.async_call = AsyncMock(side_effect=refusal)

        assert await TV02.override_set_hvac_mode(host, ENTITY_ID, "heat") is False

    @pytest.mark.parametrize("refusal", REFUSALS, ids=REFUSAL_IDS)
    @pytest.mark.asyncio
    async def test_a_refused_setpoint_write_is_declined(self, refusal):
        """The setpoint is the command, so its refusal decides the answer."""
        host = _host(model="TV02-Zigbee")

        async def _refuse_the_setpoint(_domain, service, _data, **_kwargs):
            if service == "set_temperature":
                raise refusal

        host.hass.services.async_call = AsyncMock(side_effect=_refuse_the_setpoint)

        assert await TV02.override_set_temperature(host, ENTITY_ID, 21.0) is False

    @pytest.mark.parametrize("refusal", REFUSALS, ids=REFUSAL_IDS)
    @pytest.mark.asyncio
    async def test_a_refused_preset_still_lets_the_setpoint_through(self, refusal):
        """A firmware without the manual preset refuses it on every cycle.

        The preset write comes first, so letting its refusal end the call
        would leave that device without a setpoint for as long as it runs.
        """
        host = _host(model="TV02-Zigbee")

        async def _refuse_the_preset(_domain, service, _data, **_kwargs):
            if service == "set_preset_mode":
                raise refusal

        host.hass.services.async_call = AsyncMock(side_effect=_refuse_the_preset)

        assert await TV02.override_set_temperature(host, ENTITY_ID, 21.0) is True
        assert [
            call.args[1] for call in host.hass.services.async_call.await_args_list
        ] == ["set_preset_mode", "set_temperature"]

    @pytest.mark.parametrize("refusal", REFUSALS, ids=REFUSAL_IDS)
    @pytest.mark.asyncio
    async def test_a_refused_preset_does_not_undo_the_mode_that_went_out(self, refusal):
        """The mode is already on the wire, so the caller must not repeat it."""
        host = _host(model="TV02-Zigbee")

        async def _refuse_the_preset(_domain, service, _data, **_kwargs):
            if service == "set_preset_mode":
                raise refusal

        host.hass.services.async_call = AsyncMock(side_effect=_refuse_the_preset)

        assert await TV02.override_set_hvac_mode(host, ENTITY_ID, "heat") is True


class TestTheZWaveValveDeclinesARefusedCommand:
    """ZWA021 drives the valve through two Z-Wave JS command classes.

    Both writes go to ``zwave_js.set_value``, which is missing outright on
    an installation that reaches the device through some other integration,
    and which a sleeping node answers with an error. Declining lets the
    caller do what it does for a TRV without this quirk: put the device on
    the standard climate service, and try the generic valve channel.
    """

    @staticmethod
    def _direct_valve_host():
        """A ZWA021 configured for direct valve control."""
        return _host(
            model="ZWA021", advanced={"calibration": CalibrationType.DIRECT_VALVE_BASED}
        )

    @pytest.mark.parametrize("refusal", REFUSALS, ids=REFUSAL_IDS)
    @pytest.mark.asyncio
    async def test_a_refused_valve_mode_write_is_declined(self, refusal):
        """Without the mode the valve writes are ignored anyway."""
        host = self._direct_valve_host()
        host.hass.services.async_call = AsyncMock(side_effect=refusal)

        answered = await zwa021_quirk.override_set_hvac_mode(host, ENTITY_ID, "heat")

        assert answered is False

    @pytest.mark.parametrize("refusal", REFUSALS, ids=REFUSAL_IDS)
    @pytest.mark.asyncio
    async def test_a_refused_valve_write_is_declined(self, refusal):
        """A position that never reached the valve is not one to record."""
        host = self._direct_valve_host()
        host.hass.services.async_call = AsyncMock(side_effect=refusal)

        assert await zwa021_quirk.override_set_valve(host, ENTITY_ID, 50) is False


class TestTheEurotronicModeSelectReportsARefusedOption:
    """SPZB0001 puts the device's TRV mode select on the mode it needs.

    The select is what decides whether the device takes external valve
    positions at all, and the write runs during startup, where the rest of
    the TRV's initialisation follows it.
    """

    @staticmethod
    def _registry_holding_a_mode_select():
        """An entity registry whose device carries a TRV mode select."""
        climate_entry = MagicMock(
            entity_id=ENTITY_ID,
            domain="climate",
            device_id="device1",
            unique_id="0x1234_climate",
            original_name="TRV",
        )
        select_entry = MagicMock(
            entity_id="select.trv_trv_mode",
            domain="select",
            device_id="device1",
            unique_id="0x1234_trv_mode",
            original_name="Trv mode",
        )
        registry = MagicMock()
        registry.async_get.return_value = climate_entry
        registry.entities.values.return_value = [climate_entry, select_entry]
        return registry

    @pytest.mark.parametrize("refusal", REFUSALS, ids=REFUSAL_IDS)
    @pytest.mark.asyncio
    async def test_a_refused_option_is_reported(self, refusal):
        """The device keeps the mode it has, and says so."""
        host = _host(state=State("select.trv_trv_mode", "2"), model="SPZB0001")
        host.hass.services.async_call = AsyncMock(side_effect=refusal)

        with patch.object(
            spzb0001_quirk.er,
            "async_get",
            lambda hass: self._registry_holding_a_mode_select(),
        ):
            answered = await spzb0001_quirk.check_operation_mode(host, ENTITY_ID, "1")

        assert answered is False

    @pytest.mark.asyncio
    async def test_the_option_is_written_and_waited_for(self):
        """A refusal only reaches the caller when the call blocks.

        `ServiceRegistry.async_call` defaults to fire-and-forget and runs a
        failing handler in a background task, so without `blocking=True` the
        device's refusal never raises here and the mode reads as switched
        when it is not.
        """
        host = _host(state=State("select.trv_trv_mode", "2"), model="SPZB0001")
        host.hass.services.async_call = AsyncMock(return_value=None)

        with patch.object(
            spzb0001_quirk.er,
            "async_get",
            lambda hass: self._registry_holding_a_mode_select(),
        ):
            assert await spzb0001_quirk.check_operation_mode(host, ENTITY_ID, "1")

        host.hass.services.async_call.assert_awaited_once_with(
            "select",
            "select_option",
            {"entity_id": "select.trv_trv_mode", "option": "1"},
            blocking=True,
            context=host.context,
        )

    @pytest.mark.parametrize("refusal", REFUSALS, ids=REFUSAL_IDS)
    @pytest.mark.asyncio
    async def test_a_refused_option_does_not_end_the_startup_tweak(self, refusal):
        """The startup path runs this write among others."""
        host = _host(
            state=State("select.trv_trv_mode", "2"),
            model="SPZB0001",
            advanced={"calibration": CalibrationType.DIRECT_VALVE_BASED},
        )
        host.hass.services.async_call = AsyncMock(side_effect=refusal)

        with patch.object(
            spzb0001_quirk.er,
            "async_get",
            lambda hass: self._registry_holding_a_mode_select(),
        ):
            assert await spzb0001_quirk.initial_tweak(host, ENTITY_ID) is None


# Everything a quirk module may define that puts a command on a device,
# together with the answer its contract owes the caller.
WRITING_SURFACE = {
    "override_set_hvac_mode": (("heat",), bool),
    "override_set_temperature": ((21.0,), bool),
    "override_set_valve": ((50,), bool),
    "initial_tweak": ((), type(None)),
}
QUIRK_PACKAGE = "custom_components.better_thermostat.model_fixes"
QUIRKS_DIR = Path(spzb0001_quirk.__file__).parent
NOT_A_MODEL = {"__init__", "model_quirks", "types"}


def _writing_pairs():
    """Every (module, write function) pair the quirk package defines."""
    pairs = []
    for path in sorted(QUIRKS_DIR.glob("*.py")):
        if path.stem in NOT_A_MODEL:
            continue
        module = import_module(f"{QUIRK_PACKAGE}.{path.stem}")
        pairs.extend(
            (module, path.stem, name)
            for name in WRITING_SURFACE
            if hasattr(module, name)
        )
    return pairs


WRITING_PAIRS = _writing_pairs()
WRITING_IDS = [f"{model}-{name}" for _module, model, name in WRITING_PAIRS]


class TestNoQuirkLetsARefusalEscape:
    """The same promise, held against every module at once.

    Twelve modules extend Better Thermostat for one device family each, and
    the control cycle reaches them through a duck-typed dispatch that puts
    nothing between them and the loop. A module added later inherits the
    same exposure, so the promise is stated for the whole package rather
    than only for the modules that carry a write today.
    """

    @pytest.mark.parametrize(
        ("module", "model", "name"), WRITING_PAIRS, ids=WRITING_IDS
    )
    @pytest.mark.parametrize("refusal", REFUSALS, ids=REFUSAL_IDS)
    @pytest.mark.asyncio
    async def test_it_answers_its_contract_instead_of_raising(
        self, module, model, name, refusal
    ):
        """Called against a host that refuses everything, it still answers."""
        arguments, expected = WRITING_SURFACE[name]
        host = _host(
            state=_climate_state(RANGE_BIT),
            model=model,
            advanced={"calibration": CalibrationType.DIRECT_VALVE_BASED},
        )
        host.hass.services.async_call = AsyncMock(side_effect=refusal)

        answer = getattr(module, name)(host, ENTITY_ID, *arguments)
        if inspect.iscoroutine(answer):
            answer = await answer

        if expected is type(None):
            assert answer is None
        else:
            assert isinstance(answer, expected)
