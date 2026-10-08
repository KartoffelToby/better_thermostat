"""Shared builders for unit-test fixtures.

The canonical home of the recurring mock shapes: kernel inputs
(``make_snapshot``/``make_state``), the entity mock for the control path
(``make_bt``), the one for the reported state attributes
(``make_state_attributes_bt``) and the entity registry with its entries
(``make_registry_entry``/``make_entity_registry``). Tests import from here
instead of re-declaring the MagicMock shape per file.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Callable, Mapping
import copy
from dataclasses import replace
from datetime import UTC, datetime
import functools
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from homeassistant.components.climate.const import HVACAction, HVACMode
from homeassistant.helpers import entity_registry as er

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.decide import (
    KernelState,
    running_kernel_state,
)
from custom_components.better_thermostat.core.recorder import FlightRecorder
from custom_components.better_thermostat.core.snapshot import (
    HvacMode as CoreHvacMode,
    TrvReported,
    WorldSnapshot,
)
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.controlling import TaskManager
from custom_components.better_thermostat.utils.preset_manager import PresetManager

DEFAULT_TRV_ID = "climate.trv"
DEFAULT_CONFIG_ENTRY_ID = "config_entry_1"
DEFAULT_DEVICE_ID = "device_1"


def make_state(**overrides) -> KernelState:
    """Return a post-startup KernelState; overridable per test.

    Parameters
    ----------
    **overrides
        Field values applied via ``dataclasses.replace``.

    Returns
    -------
    KernelState
        A running kernel state with the requested overrides.
    """
    return replace(running_kernel_state(), **overrides)


def make_snapshot(**overrides) -> WorldSnapshot:
    """Return a heating-mode snapshot with two TRVs; overridable per test.

    Parameters
    ----------
    **overrides
        Field values that replace the snapshot defaults.

    Returns
    -------
    WorldSnapshot
        A heating-mode snapshot with the requested overrides.
    """
    defaults = {
        "now": datetime(2026, 1, 2, 8, 30, tzinfo=UTC),
        "now_monotonic": 1000.0,
        "heat_target_temperature": 21.0,
        "hvac_mode": CoreHvacMode.HEAT,
        "room_temperature": 19.5,
        "call_for_heat": True,
        "tolerance": 0.3,
        "trvs": {
            "climate.trv1": TrvReported(entity_id="climate.trv1"),
            "climate.trv2": TrvReported(entity_id="climate.trv2"),
        },
    }
    defaults.update(overrides)
    return WorldSnapshot(**defaults)


def trv_from_legacy_dict(entity_id: str, data: Mapping[str, object]) -> Trv:
    """Build a Trv from a plain per-entity dict.

    Known keys become typed fields; unknown keys land in ``extra``.
    The explicit ``entity_id`` argument wins over an ``entity_id``
    key in the dict, and an ``extra`` dict is merged into the extra
    mapping instead of being nested under it; a non-dict ``extra``
    value is kept under the ``extra`` key.

    Parameters
    ----------
    entity_id : str
        Entity id for the built TRV.
    data : Mapping[str, object]
        Per-entity values, keyed by field name.

    Returns
    -------
    Trv
        A TRV carrying the known keys as fields and the rest in ``extra``.
    """
    fields_in = {}
    extra = {}
    for key, value in data.items():
        if key == "entity_id":
            continue
        if key == "extra":
            if isinstance(value, dict):
                extra.update(value)
            else:
                extra[key] = value
        elif key in Trv.__dataclass_fields__:
            fields_in[key] = value
        else:
            extra[key] = value
    trv = Trv(entity_id=entity_id, **fields_in)
    trv.extra.update(extra)
    return trv


def make_trv(entity_id: str = DEFAULT_TRV_ID, **fields) -> Trv:
    """Return a Trv with identity model quirks; overridable per test.

    Parameters
    ----------
    entity_id : str
        Entity id for the built TRV.
    **fields
        Field values that replace the TRV defaults.

    Returns
    -------
    Trv
        A TRV with identity calibration quirks and the requested fields.
    """
    quirks = MagicMock()
    quirks.fix_local_calibration.side_effect = lambda _self, _eid, calibration_offset: (
        float(calibration_offset)
    )
    quirks.fix_target_temperature_calibration.side_effect = (
        lambda _self, _eid, temperature: float(temperature)
    )
    defaults = {
        "advanced": {},
        "current_temperature": 21.0,
        "last_calibration": 0.0,
        "local_calibration_step": 0.1,
        "min_local_calibration": -5.0,
        "max_local_calibration": 5.0,
        "target_temp_step": 0.1,
        "min_temp": 5.0,
        "max_temp": 30.0,
        "model_quirks": quirks,
    }
    defaults.update(fields)
    return trv_from_legacy_dict(entity_id, defaults)


def _thermostat_state_names() -> frozenset[str]:
    """Return the state a BetterThermostat holds.

    That is every attribute the class assigns on ``self`` without a class
    default, every name its body declares without a value, and every
    property it defines. Each property derives from the entity's state,
    so a mock cannot answer one any more truthfully than the state itself.
    Methods and what Home Assistant's base classes define are left out.
    """
    source = Path(inspect.getfile(BetterThermostat)).read_text(encoding="utf-8")
    cls = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.ClassDef) and node.name == BetterThermostat.__name__
    )
    names: set[str] = set()
    for node in cls.body:
        if isinstance(node, ast.AnnAssign) and node.value is None:
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
        elif isinstance(node, ast.FunctionDef) and isinstance(
            inspect.getattr_static(BetterThermostat, node.name, None), property
        ):
            names.add(node.name)
    for node in ast.walk(cls):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign | ast.AugAssign):
            targets = [node.target]
        else:
            continue
        names.update(
            target.attr
            for target in targets
            if isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
            and not hasattr(BetterThermostat, target.attr)
        )
    return frozenset(names)


THERMOSTAT_STATE = _thermostat_state_names()


def _constructor_literals() -> dict[str, object]:
    """Return what ``BetterThermostat.__init__`` assigns as a plain literal.

    Only an assignment of ``None``, a number, a string, a bool or an empty
    container counts, so the value can be rebuilt without the arguments
    the constructor takes.
    """
    source = Path(inspect.getfile(BetterThermostat)).read_text(encoding="utf-8")
    cls = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.ClassDef) and node.name == BetterThermostat.__name__
    )
    init = next(
        node
        for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    literals: dict[str, object] = {}
    for node in ast.walk(init):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                and target.attr not in literals
            ):
                try:
                    literals[target.attr] = ast.literal_eval(value)
                except ValueError:
                    continue
    return literals


# Bookkeeping every constructed thermostat carries and no test leaves out
# on purpose: the stand-in answers these with the value the constructor
# assigns. Leaving one out exercises the same branch a fresh thermostat
# takes, so it hides no state the test forgot.
_CONSTRUCTOR_DEFAULTED = (
    "unavailable_sensors",
    "_critical_grace_until",
    "_outdoor_check_lock",
    "_temperature_filter_lock",
)
_CONSTRUCTOR_LITERALS = _constructor_literals()
STAND_IN_DEFAULTS: dict[str, Callable[[], object]] = {
    **{
        name: functools.partial(copy.copy, _CONSTRUCTOR_LITERALS[name])
        for name in _CONSTRUCTOR_DEFAULTED
    },
    # Built from config the constructor takes; these are the values of a
    # thermostat configured without a temperature step and before Home
    # Assistant assigned it a unique id.
    "flight_recorder": FlightRecorder,
    "task_manager": TaskManager,
    "bt_target_temperature_step": lambda: None,
    "_unique_id": lambda: None,
}

# Properties that only return another attribute, answered from it.
_PROPERTY_SOURCES = {"unique_id": "_unique_id"}


class ThermostatStandIn(MagicMock):
    """A BetterThermostat stand-in that refuses to invent state.

    A plain ``MagicMock`` answers an attribute nobody set with a truthy
    mock, so a missing ``in_maintenance`` reads as maintenance running and
    the test exercises a branch it never meant to. This stand-in raises
    for any state attribute or property the test did not set and still
    answers methods with mocks. Production reads thermostat state
    directly, so a test sets every attribute the path it drives reads.

    The exceptions are the attributes in ``STAND_IN_DEFAULTS``, which a
    thermostat holds from construction on: the stand-in answers them with
    the constructor's value, built fresh per stand-in. ``unique_id``
    answers from ``_unique_id``, as the property does. Its children are
    plain ``MagicMock``s, so ``bt.hass.config`` stays as permissive as
    before.
    """

    def __getattr__(self, name: str):
        """Refuse thermostat state the test did not set."""
        if (default := STAND_IN_DEFAULTS.get(name)) is not None:
            value = default()
            setattr(self, name, value)
            return value
        if (source := _PROPERTY_SOURCES.get(name)) is not None:
            return getattr(self, source)
        if name in THERMOSTAT_STATE:
            raise AttributeError(
                f"the stand-in has no {name!r}; set it on the stand-in "
                "instead of letting MagicMock invent a truthy value"
            )
        return super().__getattr__(name)

    def _get_child_mock(self, **kw):
        """Build children as plain mocks; only the thermostat itself is strict.

        Under a ``spec``, a coroutine method of the spec class stays
        awaitable, as it does on a plain ``MagicMock(spec=...)``.
        """
        if kw.get("_new_name") in self.__dict__.get("_spec_asyncs", ()):
            return AsyncMock(**kw)
        return MagicMock(**kw)


def make_bt(
    *,
    trv_ids: tuple[str, ...] = (DEFAULT_TRV_ID,),
    hvac_action=HVACAction.IDLE,
    room_temperature: float | None = 20.0,
    heat_target_temperature: float | None = 21.0,
    tolerance: float = 0.3,
    **trv_fields,
) -> MagicMock:
    """Return the recurring entity mock: clock, kernel regions, queues, TRVs.

    Parameters
    ----------
    trv_ids : tuple of str
        Entity ids for the TRVs to build on the mock.
    hvac_action : HVACAction
        Initial HVAC action reported by the mock.
    room_temperature : float | None
        Current room temperature.
    heat_target_temperature : float | None
        Target temperature.
    tolerance : float
        Control tolerance band.
    **trv_fields
        Forwarded into every TRV built for ``trv_ids``.

    Returns
    -------
    MagicMock
        The entity mock with clock, kernel regions, queues and TRVs.
    """
    bt = ThermostatStandIn()
    bt.name = "better_thermostat"
    bt.device_name = "Test BT"
    bt.tolerance = tolerance
    bt.hvac_action = hvac_action
    bt.room_temperature = room_temperature
    bt.heat_target_temperature = heat_target_temperature
    bt.outdoor_sensor_entity_id = None
    bt.weather_entity_id = None
    bt.window_sensor_entity_id = None
    bt.door_sensor_entity_id = None
    bt.bt_hvac_mode = HVACMode.HEAT
    bt.window_open = False
    bt.call_for_heat = True
    bt.ignore_states = False
    bt.clock = FakeClock()
    bt.kernel_state = running_kernel_state()
    bt.control_queue_task = asyncio.Queue(maxsize=1)
    bt.window_queue_task = asyncio.Queue(maxsize=1)
    bt.real_trvs = {
        entity_id: make_trv(entity_id, **trv_fields) for entity_id in trv_ids
    }
    return bt


def make_state_attributes_bt(**overrides) -> MagicMock:
    """Return the entity mock ``extra_state_attributes`` can be read from.

    The property JSON-encodes several of the values it reads, so the
    collections it serialises have to be real containers rather than
    MagicMock children.

    Parameters
    ----------
    **overrides
        Attribute values applied on top of the defaults.

    Returns
    -------
    MagicMock
        The entity mock with every attribute the property touches.
    """
    bt = ThermostatStandIn()
    bt.device_name = "Test BT"
    bt.window_open = False
    bt.heating_power_normalized = None
    bt.call_for_heat = True
    bt.last_change = datetime(2026, 5, 18, tzinfo=UTC)
    bt._current_humidity = None
    bt.humidity_sensor_entity_id = None
    bt.last_main_hvac_mode = HVACMode.HEAT
    bt.off_temperature = None
    bt.tolerance = 0.5
    bt.bt_target_temperature_step = 0.5
    bt.heating_power = 0.1
    bt.heat_loss_rate = 0.0
    bt.devices_errors = []
    bt.devices_states = {}
    bt.room_temperature_filtered = 20.5
    bt.degraded_mode = False
    bt.unavailable_sensors = []
    bt.real_trvs = {}
    bt.heating_cycles = []
    bt.loss_cycles = []
    bt.last_heating_power_stats = {}
    bt.last_heat_loss_stats = {}
    bt.next_valve_maintenance = None
    bt._preset_cool_temperatures = {}
    bt._preset_cool_temperature = None
    bt.preset_mgr = PresetManager(temperatures={})
    bt.door_open = False
    bt.kernel_state = running_kernel_state()
    bt.clock = FakeClock()
    bt.temperature_slope = None
    for name, value in overrides.items():
        setattr(bt, name, value)
    return bt


def make_registry_entry(
    entity_id: str,
    *,
    unique_id: str | None = None,
    platform: str = "mqtt",
    config_entry_id: str | None = DEFAULT_CONFIG_ENTRY_ID,
    device_id: str | None = DEFAULT_DEVICE_ID,
    disabled_by: er.RegistryEntryDisabler | None = None,
    translation_key: str | None = None,
    original_name: str | None = None,
    original_device_class: str | None = None,
    device_class: str | None = None,
) -> er.RegistryEntry:
    """Return a real entity registry entry; overridable per test.

    A ``MagicMock`` in its place answers every field it was not told
    about with a truthy mock, so ``disabled_by`` would read as disabled
    and a missing ``translation_key`` as a key that matches nothing. The
    real type answers ``None`` for both, as Home Assistant does.

    Parameters
    ----------
    entity_id : str
        Entity id of the entry; the entry derives its domain from it.
    unique_id : str | None
        Unique id; the entity id when not given.
    platform : str
        Integration that registered the entity.
    config_entry_id : str | None
        Config entry the entity belongs to.
    device_id : str | None
        Device the entity belongs to; ``None`` for an entity without one.
    disabled_by : er.RegistryEntryDisabler | None
        Who disabled the entity; ``None`` for an enabled one.
    translation_key : str | None
        The integration's language-independent name for the entity.
    original_name : str | None
        The name the integration gave the entity.
    original_device_class : str | None
        The device class the integration gave the entity.
    device_class : str | None
        The device class the user set.

    Returns
    -------
    er.RegistryEntry
        The entry with the requested fields and HA's defaults otherwise.
    """
    return er.RegistryEntry(
        entity_id=entity_id,
        unique_id=unique_id if unique_id is not None else entity_id,
        platform=platform,
        capabilities=None,
        config_entry_id=config_entry_id,
        config_subentry_id=None,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        device_class=device_class,
        device_id=device_id,
        disabled_by=disabled_by,
        entity_category=None,
        has_entity_name=False,
        hidden_by=None,
        id=None,
        object_id_base=None,
        options=None,
        original_device_class=original_device_class,
        original_icon=None,
        original_name=original_name,
        suggested_object_id=None,
        supported_features=0,
        translation_key=translation_key,
        unit_of_measurement=None,
    )


def make_entity_registry(*entries: er.RegistryEntry) -> MagicMock:
    """Return an entity registry holding ``entries``.

    The registry itself is a mock specced on ``er.EntityRegistry``, so a
    call outside its surface fails instead of answering with a mock. Its
    ``entities`` is HA's own container, so ``entities.values()``,
    ``entities.get()`` and ``async_entries_for_config_entry`` answer from
    the entries exactly as they do in Home Assistant, disabled ones
    included. ``async_get`` resolves an entity id through that container.

    Parameters
    ----------
    *entries : er.RegistryEntry
        The entries the registry holds, in registration order.

    Returns
    -------
    MagicMock
        The registry, answering from ``entries``.
    """
    items = er.EntityRegistryItems(MagicMock())
    for entry in entries:
        items[entry.entity_id] = entry
    registry = MagicMock(spec=er.EntityRegistry)
    registry.entities = items
    registry.async_get.side_effect = items.get
    return registry
