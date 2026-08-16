"""Device forms the integration harness can be parametrized over.

Pure data: a profile names one real-world device shape together with the
Better Thermostat configuration that belongs to it, because the two are
inseparable — a Zigbee2MQTT head *is* the mqtt integration plus local
calibration, and pairing one with the other integration describes a device
that does not exist. ``conftest.py`` turns a profile into live entities.
"""

from dataclasses import dataclass
from enum import StrEnum

from homeassistant.components.climate import ClimateEntityFeature, HVACMode
from homeassistant.const import UnitOfTemperature

TRV_ID = "climate.fake_trv"
COOLER_ID = "climate.fake_cooler"
OFFSET_NUMBER_ID = "number.fake_trv_calibration"

_SETPOINT_FEATURES = (
    ClimateEntityFeature.TARGET_TEMPERATURE
    | ClimateEntityFeature.TURN_OFF
    | ClimateEntityFeature.TURN_ON
)


class OffsetChannel(StrEnum):
    """How a device takes a calibration offset."""

    NONE = "none"
    NUMBER_ENTITY = "number_entity"
    ECOSYSTEM_SERVICE = "ecosystem_service"


@dataclass(frozen=True)
class DeviceProfile:
    """A simulated climate device, as Better Thermostat is configured for it.

    Every temperature-shaped field is expressed in the device's own unit,
    which is what a real device reports and what the climate entity
    attributes mean.

    ``configured_target_temp_step`` is the config entry's step string, where
    ``"0.0"`` means "not configured". A configured step overrides the device's
    own grid for every child, so a profile that varies the device step keeps
    this at ``"0.0"``.
    """

    name: str
    entity_id: str = TRV_ID
    entity_name: str = "fake trv"
    hvac_modes: tuple[HVACMode, ...] = (HVACMode.HEAT, HVACMode.OFF)
    hvac_mode: HVACMode = HVACMode.HEAT
    min_temp: float = 5.0
    max_temp: float = 30.0
    current_temperature: float = 19.5
    target_temperature: float = 20.0
    target_temperature_step: float = 0.5
    temperature_unit: UnitOfTemperature = UnitOfTemperature.CELSIUS
    supported_features: ClimateEntityFeature = _SETPOINT_FEATURES
    precision: float | None = None
    integration: str = "generic_thermostat"
    calibration: str = "target_temp_based"
    configured_target_temp_step: str = "0.0"
    offset_channel: OffsetChannel = OffsetChannel.NONE
    has_device_registry_entry: bool = False


@dataclass(frozen=True)
class RoleScenario:
    """How Better Thermostat is wired to one or two devices.

    ``cooler_entity_id`` is the entry's cooler key. It is not always
    ``cooler.entity_id``: a dual-role device is a single entity wired as
    thermostat and as cooler at the same time.
    """

    name: str
    trv: DeviceProfile
    cooler: DeviceProfile | None
    cooler_entity_id: str | None


GENERIC_HEAT_TRV = DeviceProfile(
    name="generic_heat_trv", configured_target_temp_step="0.5"
)
"""The common half-degree Celsius radiator head with heat and off."""

AUTO_COOL_AC = DeviceProfile(
    name="auto_cool_ac",
    hvac_modes=(HVACMode.AUTO, HVACMode.COOL, HVACMode.OFF),
    hvac_mode=HVACMode.AUTO,
)
"""An air conditioner head that offers auto and cool, but no heat."""

AUTO_ONLY_TRV = DeviceProfile(
    name="auto_only_trv",
    hvac_modes=(HVACMode.AUTO, HVACMode.OFF),
    hvac_mode=HVACMode.AUTO,
)
"""A radiator head whose heating mode is called auto."""

HEAT_COOL_TRV = DeviceProfile(
    name="heat_cool_trv",
    hvac_modes=(HVACMode.HEAT_COOL, HVACMode.OFF),
    hvac_mode=HVACMode.HEAT_COOL,
)
"""A radiator head that exposes heat_cool where others expose heat."""

INTEGER_GRID_TRV = DeviceProfile(name="integer_grid_trv", target_temperature_step=1.0)
"""A wall thermostat with a whole-degree setpoint grid."""

FAHRENHEIT_TRV = DeviceProfile(
    name="fahrenheit_trv",
    min_temp=41.0,
    max_temp=86.0,
    current_temperature=67.1,
    target_temperature=68.0,
    target_temperature_step=1.0,
    temperature_unit=UnitOfTemperature.FAHRENHEIT,
    precision=0.1,
)
"""A device on a Fahrenheit system, reporting whole degrees Fahrenheit.

``precision`` is pinned to a tenth because a climate entity on a Fahrenheit
system otherwise rounds every published temperature to a whole degree, which
is a third of a degree Celsius of drift on top of the value under test.
"""

MQTT_OFFSET_TRV = DeviceProfile(
    name="mqtt_offset_trv",
    current_temperature=19.0,
    integration="mqtt",
    calibration="local_calibration_based",
    offset_channel=OffsetChannel.NUMBER_ENTITY,
    has_device_registry_entry=True,
)
"""A Zigbee2MQTT head whose calibration is a number entity on its device.

The registry entry is a precondition, not a decoration: calibration entity
discovery walks the entity registry from the device the head belongs to, so a
device-less entity has no calibration channel at all.
"""

TADO_OFFSET_TRV = DeviceProfile(
    name="tado_offset_trv",
    current_temperature=19.0,
    integration="tado",
    calibration="local_calibration_based",
    offset_channel=OffsetChannel.ECOSYSTEM_SERVICE,
)
"""A head whose calibration is an ecosystem service call, not an entity."""

ROOM_AC_COOLER = DeviceProfile(
    name="room_ac_cooler",
    entity_id=COOLER_ID,
    entity_name="fake cooler",
    hvac_modes=(HVACMode.COOL, HVACMode.OFF),
    hvac_mode=HVACMode.OFF,
    min_temp=16.0,
    max_temp=30.0,
    current_temperature=22.0,
    target_temperature=24.0,
)
"""The cooling partner of a heated room, with a narrower range than the head."""

DUAL_ROLE_AC = DeviceProfile(
    name="dual_role_ac",
    hvac_modes=(HVACMode.HEAT, HVACMode.COOL, HVACMode.OFF),
    hvac_mode=HVACMode.HEAT,
    current_temperature=22.0,
)
"""One entity that heats and cools, wired as thermostat and as cooler."""

HEAT_ONLY = RoleScenario(
    name="heat_only", trv=GENERIC_HEAT_TRV, cooler=None, cooler_entity_id=None
)

SEPARATE_COOLER = RoleScenario(
    name="separate_cooler",
    trv=GENERIC_HEAT_TRV,
    cooler=ROOM_AC_COOLER,
    cooler_entity_id=COOLER_ID,
)

DUAL_ROLE = RoleScenario(
    name="dual_role", trv=DUAL_ROLE_AC, cooler=None, cooler_entity_id=TRV_ID
)

SINGLE_ROLE_PROFILES = (
    GENERIC_HEAT_TRV,
    AUTO_COOL_AC,
    AUTO_ONLY_TRV,
    HEAT_COOL_TRV,
    INTEGER_GRID_TRV,
    FAHRENHEIT_TRV,
    MQTT_OFFSET_TRV,
    TADO_OFFSET_TRV,
)
"""Every profile a test can drive as the single controlled device."""

ROLE_SCENARIOS = (HEAT_ONLY, SEPARATE_COOLER, DUAL_ROLE)
