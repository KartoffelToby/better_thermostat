"""The Gold rules of the Integration Quality Scale, held on a running instance.

Each test carries the rule it holds as a ``quality_rule`` marker;
``quality_scale.yaml`` records the rule's status and ``tests/quality_scale.py``
says how the two work together.

Which entities an entry creates depends on its calibration, so the entity
rules are checked against the entities of every calibration mode and of a
thermostat whose valve is driven directly.
"""

from dataclasses import dataclass, replace

from homeassistant.components.number.const import (
    DEVICE_CLASS_UNITS as NUMBER_DEVICE_CLASS_UNITS,
)
from homeassistant.components.sensor.const import (
    DEVICE_CLASS_UNITS as SENSOR_DEVICE_CLASS_UNITS,
)
from homeassistant.const import EntityCategory
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest

from custom_components.better_thermostat.utils.const import (
    CalibrationMode,
    CalibrationType,
)

from .conftest import (
    build_devices,
    make_entry,
    set_room_humidity,
    set_room_sensor,
    setup_entry,
    wait_for_startup,
)
from .device_profiles import GENERIC_HEAT_TRV, VALVE_TRV

# A valve-driven thermostat beside the generic one, under an id of its own.
DRIVEN_VALVE_TRV = replace(
    VALVE_TRV,
    name="driven_valve",
    entity_id="climate.driven_valve",
    entity_name="driven valve",
    calibration=CalibrationType.DIRECT_VALVE_BASED.value,
)

# Every way an entry can be set up that brings entities of its own.
VARIANTS = (
    *(
        replace(GENERIC_HEAT_TRV, calibration_mode=mode.value)
        for mode in CalibrationMode
    ),
    DRIVEN_VALVE_TRV,
)

# The prefixes of the translation keys of a calibration algorithm's internals:
# its gains and estimates, which only someone tuning the algorithm reads.
ALGORITHM_INTERNALS = ("pid_", "mpc_")

# Units a device class would claim but the entity's value is not of that class.
NOT_OF_THE_UNITS_CLASS = {
    # A difference of two temperatures; the temperature class converts it
    # between units as if it were a temperature itself.
    "pid_error": "K"
}


@dataclass(frozen=True)
class SeenEntity:
    """What one entity looked like while its entry was loaded."""

    entity_id: str
    domain: str
    translation_key: str | None
    name: object
    has_entity_name: bool
    entity_category: EntityCategory | None
    device_class: str | None
    unit: str | None
    enabled_by_default: bool
    device_id: str | None


async def _entities_of_every_variant(hass) -> tuple[list[SeenEntity], list[str]]:
    """Set up an entry of every variant in turn and record its entities.

    Return the entities, and for each entry the problems with its device.
    """
    set_room_sensor(hass, 19.0)
    set_room_humidity(hass, 50.0)
    # One call per device: the two are registered in different ways.
    await build_devices(hass, GENERIC_HEAT_TRV)
    await build_devices(hass, DRIVEN_VALVE_TRV)
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    seen: list[SeenEntity] = []
    device_problems: list[str] = []
    for profile in VARIANTS:
        entry = make_entry(profile, with_humidity=True)
        await setup_entry(hass, entry)
        await wait_for_startup(hass, entry)
        # The registry holds every entity, also those that start disabled
        # and so never reach a platform.
        seen += [
            SeenEntity(
                entity_id=registered.entity_id,
                domain=registered.domain,
                translation_key=registered.translation_key,
                name=registered.original_name,
                has_entity_name=registered.has_entity_name,
                entity_category=registered.entity_category,
                device_class=registered.original_device_class,
                unit=registered.unit_of_measurement,
                enabled_by_default=registered.disabled_by
                is not er.RegistryEntryDisabler.INTEGRATION,
                device_id=registered.device_id,
            )
            for registered in er.async_entries_for_config_entry(
                entity_registry, entry.entry_id
            )
        ]
        device_problems += _device_problems(device_registry, entity_registry, entry)
        assert await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()
    return seen, device_problems


def _device_problems(device_registry, entity_registry, entry) -> list[str]:
    """Return what keeps ``entry``'s entities from sharing one described device."""
    devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    if len(devices) != 1:
        return [f"{entry.title}: {len(devices)} devices"]
    (device,) = devices
    problems = [
        f"{entry.title}: device has no {field}"
        for field in ("name", "manufacturer", "model", "sw_version")
        if not getattr(device, field)
    ]
    problems += [
        f"{registered.entity_id} is not on the device"
        for registered in er.async_entries_for_config_entry(
            entity_registry, entry.entry_id
        )
        if registered.device_id != device.id
    ]
    return problems


def _distinct(entities: list[SeenEntity]) -> list[SeenEntity]:
    """Return each kind of entity once, the first time it was seen."""
    by_kind: dict[tuple[str, str | None], SeenEntity] = {}
    for entity in entities:
        by_kind.setdefault((entity.domain, entity.translation_key), entity)
    return list(by_kind.values())


def _units_with_a_class(domain: str) -> dict[str, set[str]]:
    """Return each unit a device class of ``domain`` takes, with those classes.

    A percentage is left out: most classes that take it are about one thing,
    a battery, a humidity, and say nothing about a percentage of something
    else.
    """
    classes = (
        NUMBER_DEVICE_CLASS_UNITS if domain == "number" else SENSOR_DEVICE_CLASS_UNITS
    )
    units: dict[str, set[str]] = {}
    for device_class, accepted in classes.items():
        for unit in accepted:
            if unit is not None and unit != "%":
                units.setdefault(str(unit), set()).add(str(device_class))
    return units


@pytest.fixture
async def every_entity(hass) -> list[SeenEntity]:
    entities, _ = await _entities_of_every_variant(hass)
    return _distinct(entities)


@pytest.mark.quality_rule("devices")
async def test_every_entity_belongs_to_the_entrys_one_described_device(hass):
    """Each entry has one device, named and described, holding all its entities."""
    entities, device_problems = await _entities_of_every_variant(hass)

    assert entities
    assert device_problems == []


@pytest.mark.quality_rule("entity-category")
async def test_every_entity_but_the_thermostat_has_a_category(every_entity):
    """Settings are configuration, readings of the algorithm are diagnostics.

    The thermostat is what the room is controlled with and stays uncategorised.
    """
    uncategorised = sorted(
        e.entity_id
        for e in every_entity
        if e.domain != "climate" and e.entity_category is None
    )
    assert uncategorised == []


@pytest.mark.quality_rule("entity-device-class")
async def test_every_entity_with_a_classed_unit_has_its_class(every_entity):
    """A value in a unit Home Assistant has a device class for carries it."""
    unclassed = []
    for entity in every_entity:
        if entity.domain not in {"sensor", "number"} or entity.unit is None:
            continue
        if NOT_OF_THE_UNITS_CLASS.get(entity.translation_key or "") == entity.unit:
            continue
        classes = _units_with_a_class(entity.domain).get(entity.unit)
        if classes and entity.device_class not in classes:
            unclassed.append(f"{entity.entity_id} [{entity.unit}] {sorted(classes)}")
    assert sorted(unclassed) == []


@pytest.mark.quality_rule("entity-disabled-by-default")
async def test_the_internals_of_the_algorithms_start_disabled(every_entity):
    """Gains and estimates of a calibration algorithm are there to be turned on.

    They change on every cycle and are read only when an algorithm is tuned,
    so a new entry leaves them off rather than recording all of them.
    """
    internals = [
        e
        for e in every_entity
        if e.domain == "sensor"
        and (e.translation_key or "").startswith(ALGORITHM_INTERNALS)
    ]
    assert internals
    assert sorted(e.entity_id for e in internals if e.enabled_by_default) == []


@pytest.mark.quality_rule("entity-translations")
async def test_every_entity_is_named_through_a_translation(every_entity):
    """An entity's name comes from ``strings.json``, or is its device's name."""
    untranslated = sorted(
        e.entity_id
        for e in every_entity
        if not e.has_entity_name or (e.translation_key is None and e.name is not None)
    )
    assert untranslated == []
