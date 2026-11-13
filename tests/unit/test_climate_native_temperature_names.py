"""The temperature API under both names Home Assistant reads.

Home Assistant 2026.11 reads a climate entity's temperatures under the
``native_`` names and reports an entity class that still provides the
unprefixed ones; the releases before it, down to the minimum in ``hacs.json``,
read only the unprefixed names.
"""

from homeassistant.components.climate import ClimateEntity
from homeassistant.const import UnitOfTemperature
import pytest

from custom_components.better_thermostat.climate import (
    PRE_NATIVE_TEMPERATURE_NAMES,
    BetterThermostat,
    serve_pre_native_temperature_names,
)
from custom_components.better_thermostat.core.containers import BtConfig, BtRuntime


def _entity(*, cooler_entity_id: str | None) -> BetterThermostat:
    """Entity shell with distinct room, heating and cooling temperatures."""
    entity = object.__new__(BetterThermostat)
    entity.config = BtConfig(
        sensor_entity_id="sensor.room_temperature",
        device_name="Test BT",
        tolerance=0.3,
        cooler_entity_id=cooler_entity_id,
    )
    entity.runtime = BtRuntime(
        heat_target_temperature=21.0,
        room_temperature=19.5,
        cool_target_temperature=25.0,
    )
    entity.bt_min_temp = 5.0
    entity.bt_max_temp = 30.0
    return entity


@pytest.mark.parametrize(("name", "native_name"), PRE_NATIVE_TEMPERATURE_NAMES)
def test_each_pair_names_a_temperature_property_of_home_assistant(
    name: str, native_name: str
) -> None:
    """Each pair names a climate temperature property and its native_ name."""
    assert native_name == f"native_{name}"
    assert name in vars(ClimateEntity) or native_name in vars(ClimateEntity)


@pytest.mark.parametrize("cooler_entity_id", [None, "climate.cooler"])
@pytest.mark.parametrize(("name", "native_name"), PRE_NATIVE_TEMPERATURE_NAMES)
def test_the_unprefixed_name_answers_what_the_native_name_answers(
    cooler_entity_id: str | None, name: str, native_name: str
) -> None:
    """Home Assistant before 2026.11 reads the value the native_ name holds."""
    entity = _entity(cooler_entity_id=cooler_entity_id)

    assert getattr(entity, name) == getattr(entity, native_name)


def test_the_native_names_carry_the_temperatures() -> None:
    """The native_ names report Celsius and the room's own temperatures."""
    entity = _entity(cooler_entity_id="climate.cooler")

    assert entity.native_temperature_unit == UnitOfTemperature.CELSIUS
    assert entity.native_current_temperature == 19.5
    assert entity.native_target_temperature == 21.0
    assert entity.native_target_temperature_low == 21.0
    assert entity.native_target_temperature_high == 25.0


def test_a_base_reading_the_native_names_gets_no_unprefixed_ones() -> None:
    """Where the base serves the unprefixed names, the entity provides none.

    Home Assistant 2026.11 reports every entity class that provides an
    unprefixed temperature name itself.
    """

    class NativeBase:
        native_temperature_unit = UnitOfTemperature.CELSIUS

    class Entity(NativeBase):
        pass

    serve_pre_native_temperature_names(Entity, NativeBase)

    assert not any(name in vars(Entity) for name, _ in PRE_NATIVE_TEMPERATURE_NAMES)


def test_a_base_reading_the_unprefixed_names_gets_all_five() -> None:
    """Where the base reads the unprefixed names, each one reads its native_ name."""

    class PreNativeBase:
        pass

    class Entity(PreNativeBase):
        native_temperature_unit = "unit"
        native_current_temperature = 1.0
        native_target_temperature = 2.0
        native_target_temperature_low = 3.0
        native_target_temperature_high = 4.0

    serve_pre_native_temperature_names(Entity, PreNativeBase)
    entity = Entity()

    assert [getattr(entity, name) for name, _ in PRE_NATIVE_TEMPERATURE_NAMES] == [
        "unit",
        1.0,
        2.0,
        3.0,
        4.0,
    ]
