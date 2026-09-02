"""Device forms the integration harness can be parametrized over.

Pure data: a profile names one real-world device shape together with the
Better Thermostat configuration that belongs to it, because the two are
inseparable — a Zigbee2MQTT head *is* the mqtt integration plus local
calibration, and pairing one with the other integration describes a device
that does not exist. Every profile therefore states its integration, its
calibration strategy and whether its entity carries a device registry entry,
so the table reads as a list of devices instead of a list of overrides.
``conftest.py`` turns a profile into live entities.

A ``RoleScenario`` and a ``GroupScenario`` name the two ways a config entry
reaches more than one of them: which device plays which role in a room, and
which heads the entry drives together.
"""

from dataclasses import dataclass
from enum import StrEnum

from homeassistant.components.climate import ClimateEntityFeature, HVACMode
from homeassistant.const import UnitOfTemperature

TRV_ID = "climate.fake_trv"
SPARE_TRV_ID = "climate.spare_trv"
COOLER_ID = "climate.fake_cooler"

_SETPOINT_FEATURES = (
    ClimateEntityFeature.TARGET_TEMPERATURE
    | ClimateEntityFeature.TURN_OFF
    | ClimateEntityFeature.TURN_ON
)

_RANGE_FEATURES = (
    ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
    | ClimateEntityFeature.TURN_OFF
    | ClimateEntityFeature.TURN_ON
)


class OffsetChannel(StrEnum):
    """How a device takes a calibration offset."""

    NONE = "none"
    NUMBER_ENTITY = "number_entity"
    ECOSYSTEM_SERVICE = "ecosystem_service"


class ValveChannel(StrEnum):
    """How a device takes a valve position."""

    NONE = "none"
    NUMBER_ENTITY = "number_entity"
    MODEL_QUIRK = "model_quirk"


@dataclass(frozen=True, kw_only=True)
class DeviceProfile:
    """A simulated climate device, as Better Thermostat is configured for it.

    Every temperature-shaped field is expressed in the device's own unit,
    which is what a real device reports and what the climate entity
    attributes mean.

    ``integration``, ``calibration`` and ``has_device_registry_entry`` carry
    no defaults on purpose: they decide which adapter runs and which
    discovery channels exist, so a profile that left them implicit would
    describe a different device than its docstring claims.

    ``configured_target_temp_step`` is the config entry's step string, where
    ``"0.0"`` means "not configured". A configured step overrides the device's
    own grid for every child, so a profile that varies the device step keeps
    this at ``"0.0"``.

    ``model`` is what the device registry entry reports, and ``None`` is a
    device that reports none. It is what selects the quirk module a device is
    driven by, so a profile whose behaviour rides on a quirk names it here;
    it needs ``has_device_registry_entry`` to be readable at all.
    """

    name: str
    integration: str
    calibration: str
    has_device_registry_entry: bool
    model: str | None = None
    entity_id: str = TRV_ID
    entity_name: str = "fake trv"
    hvac_modes: tuple[HVACMode, ...] = (HVACMode.HEAT, HVACMode.OFF)
    hvac_mode: HVACMode = HVACMode.HEAT
    min_temp: float = 5.0
    max_temp: float = 30.0
    current_temperature: float = 19.5
    target_temperature: float | None = 20.0
    target_temperature_low: float | None = None
    target_temperature_high: float | None = None
    target_temperature_step: float = 0.5
    temperature_unit: UnitOfTemperature = UnitOfTemperature.CELSIUS
    supported_features: ClimateEntityFeature = _SETPOINT_FEATURES
    precision: float | None = None
    calibration_mode: str = "default"
    configured_target_temp_step: str = "0.0"
    offset_channel: OffsetChannel = OffsetChannel.NONE
    valve_channel: ValveChannel = ValveChannel.NONE


def offset_number_id(profile: DeviceProfile) -> str:
    """Return the entity id of the calibration number on this device."""
    return f"number.{profile.entity_id.split('.', 1)[1]}_calibration"


def valve_number_id(profile: DeviceProfile) -> str:
    """Return the entity id of the valve number on this device."""
    return f"number.{profile.entity_id.split('.', 1)[1]}_valve_position"


@dataclass(frozen=True)
class GroupScenario:
    """The heads one config entry drives as a group in a single room.

    Better Thermostat holds one target temperature for the room and writes it
    to every head in ``profiles``; which head is listed first decides nothing
    but the order they appear in.
    """

    name: str
    profiles: tuple[DeviceProfile, ...]


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
    name="generic_heat_trv",
    integration="generic_thermostat",
    calibration="target_temp_based",
    has_device_registry_entry=False,
    configured_target_temp_step="0.5",
)
"""A device-less climate helper offering heat and off on half degrees Celsius.

The plainest form Better Thermostat sits in front of: no device registry
entry, so no calibration or valve channel can be discovered for it, and the
setpoint is the only thing that can be written.
"""

SPARE_HEAT_TRV = DeviceProfile(
    name="spare_heat_trv",
    integration="generic_thermostat",
    calibration="target_temp_based",
    has_device_registry_entry=False,
    entity_id=SPARE_TRV_ID,
    entity_name="spare trv",
    configured_target_temp_step="0.5",
)
"""A second device-less helper, for a room whose thermostat is swapped.

Identical in shape to ``GENERIC_HEAT_TRV`` and different only in identity:
what it is for is being a device the config entry has never seen.
"""

AUTO_COOL_AC = DeviceProfile(
    name="auto_cool_ac",
    integration="generic_thermostat",
    calibration="target_temp_based",
    has_device_registry_entry=False,
    hvac_modes=(HVACMode.AUTO, HVACMode.COOL, HVACMode.OFF),
    hvac_mode=HVACMode.AUTO,
)
"""A cooling-first device: it offers auto and cool, and no heating mode."""

AUTO_ONLY_TRV = DeviceProfile(
    name="auto_only_trv",
    integration="generic_thermostat",
    calibration="target_temp_based",
    has_device_registry_entry=False,
    hvac_modes=(HVACMode.AUTO, HVACMode.OFF),
    hvac_mode=HVACMode.AUTO,
)
"""A device whose only mode besides off is auto.

Nothing in the mode list says heat, and whether auto means heating is a
question the device does not answer — only the entry's swap option does.
"""

HEAT_COOL_TRV = DeviceProfile(
    name="heat_cool_trv",
    integration="generic_thermostat",
    calibration="target_temp_based",
    has_device_registry_entry=False,
    hvac_modes=(HVACMode.HEAT_COOL, HVACMode.OFF),
    hvac_mode=HVACMode.HEAT_COOL,
)
"""A device that names its one active mode heat_cool.

A single setpoint drives both directions, so the device offers one mode for
both and expects that name on the wire whenever it is to run at all.
"""

INTEGER_GRID_TRV = DeviceProfile(
    name="integer_grid_trv",
    integration="generic_thermostat",
    calibration="target_temp_based",
    has_device_registry_entry=False,
    target_temperature_step=1.0,
)
"""A wall thermostat with a whole-degree setpoint grid."""

FAHRENHEIT_TRV = DeviceProfile(
    name="fahrenheit_trv",
    integration="generic_thermostat",
    calibration="target_temp_based",
    has_device_registry_entry=False,
    min_temp=41.0,
    max_temp=86.0,
    current_temperature=67.1,
    target_temperature=68.0,
    target_temperature_step=1.0,
    temperature_unit=UnitOfTemperature.FAHRENHEIT,
    precision=0.1,
)
"""A device on a Fahrenheit system, reporting whole degrees Fahrenheit.

Building this profile also puts the whole Home Assistant instance on the US
customary unit system: a climate entity publishes no unit of its own, so the
system unit is the only thing Better Thermostat can read a device unit from.

``precision`` is pinned to a tenth because a climate entity on a Fahrenheit
system otherwise rounds every published temperature to a whole degree, which
is a third of a degree Celsius of drift on top of the value under test.
"""

MQTT_OFFSET_TRV = DeviceProfile(
    name="mqtt_offset_trv",
    integration="mqtt",
    calibration="local_calibration_based",
    has_device_registry_entry=True,
    current_temperature=19.0,
    offset_channel=OffsetChannel.NUMBER_ENTITY,
)
"""A Zigbee2MQTT head whose calibration is a number entity on its device.

The registry entry is a precondition, not a decoration: calibration entity
discovery walks the entity registry from the device the head belongs to, so a
device-less entity has no calibration channel at all.
"""

TADO_OFFSET_TRV = DeviceProfile(
    name="tado_offset_trv",
    integration="tado",
    calibration="local_calibration_based",
    has_device_registry_entry=False,
    current_temperature=19.0,
    offset_channel=OffsetChannel.ECOSYSTEM_SERVICE,
)
"""A head whose calibration is an ecosystem service call, not an entity."""

VALVE_TRV = DeviceProfile(
    name="valve_trv",
    integration="mqtt",
    calibration="direct_valve_based",
    calibration_mode="heating_power_calibration",
    has_device_registry_entry=True,
    current_temperature=19.0,
    valve_channel=ValveChannel.NUMBER_ENTITY,
)
"""A Zigbee2MQTT head driven by its valve rather than by its setpoint.

The valve number sits on the same device as the climate entity, which is
what makes it discoverable and writable; direct valve control is the one
calibration strategy that puts a percentage on the wire at all.
"""

ZHA_VALVE_QUIRK_TRV = DeviceProfile(
    name="zha_valve_quirk_trv",
    integration="zha",
    calibration="local_calibration_based",
    has_device_registry_entry=True,
    model="TRVZB",
    current_temperature=19.0,
    offset_channel=OffsetChannel.NUMBER_ENTITY,
    valve_channel=ValveChannel.MODEL_QUIRK,
)
"""A Sonoff TRVZB on an ecosystem Better Thermostat has no adapter for.

Its valve is driven by the quirk module of its model rather than by anything
the ecosystem publishes, so the valve channel of this device is invisible to
the adapter that serves it. The device registry model is the whole of what
makes it this device: it is what selects the quirk.
"""

ROOM_AC_COOLER = DeviceProfile(
    name="room_ac_cooler",
    integration="generic_thermostat",
    calibration="target_temp_based",
    has_device_registry_entry=False,
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

RANGED_AC_COOLER = DeviceProfile(
    name="ranged_ac_cooler",
    integration="generic_thermostat",
    calibration="target_temp_based",
    has_device_registry_entry=False,
    entity_id=COOLER_ID,
    entity_name="fake cooler",
    hvac_modes=(HVACMode.COOL, HVACMode.OFF),
    hvac_mode=HVACMode.OFF,
    min_temp=16.0,
    max_temp=30.0,
    current_temperature=22.0,
    target_temperature=None,
    target_temperature_low=20.0,
    target_temperature_high=24.0,
    supported_features=_RANGE_FEATURES,
)
"""A cooler that publishes a band instead of a setpoint.

It advertises only the range feature, so it rejects a plain ``temperature``
payload; the cooling setpoint has to arrive as the upper bound of a band.
"""

DUAL_ROLE_AC = DeviceProfile(
    name="dual_role_ac",
    integration="generic_thermostat",
    calibration="target_temp_based",
    has_device_registry_entry=False,
    hvac_modes=(HVACMode.HEAT, HVACMode.COOL, HVACMode.OFF),
    hvac_mode=HVACMode.HEAT,
    current_temperature=22.0,
)
"""One entity that heats and cools, wired as thermostat and as cooler."""


def _group_member(letter: str) -> DeviceProfile:
    """Return one plain heat head of a group, identified by ``letter``.

    Identical to every other member in everything but identity. A group of
    these answers the questions that need several heads and nothing else, so
    any other difference between them would be a second variable.
    """
    return DeviceProfile(
        name=f"group_trv_{letter}",
        integration="generic_thermostat",
        calibration="target_temp_based",
        has_device_registry_entry=False,
        entity_id=f"climate.group_trv_{letter}",
        entity_name=f"group trv {letter}",
        configured_target_temp_step="0.5",
    )


GROUP_OF_THREE = GroupScenario(
    name="three_identical_heads",
    profiles=tuple(_group_member(letter) for letter in ("a", "b", "c")),
)
"""Three interchangeable heads in one room.

Three rather than two so that a majority is not yet everybody: with two
heads, one dissenter is already half the room, and a rule that waits for all
of them looks exactly like one that waits for most.
"""

MIXED_GRID_A = DeviceProfile(
    name="mixed_grid_a",
    integration="generic_thermostat",
    calibration="target_temp_based",
    has_device_registry_entry=False,
    entity_id="climate.mixed_half_degree_trv",
    entity_name="mixed half degree trv",
    target_temperature_step=0.5,
)
"""The half-degree head of a room whose two heads are not the same model."""

MIXED_GRID_B = DeviceProfile(
    name="mixed_grid_b",
    integration="generic_thermostat",
    calibration="target_temp_based",
    has_device_registry_entry=False,
    entity_id="climate.mixed_whole_degree_trv",
    entity_name="mixed whole degree trv",
    hvac_modes=(HVACMode.HEAT_COOL, HVACMode.OFF),
    hvac_mode=HVACMode.HEAT_COOL,
    min_temp=7.0,
    max_temp=28.0,
    target_temperature_step=1.0,
)
"""The whole-degree head of that room, which also names its mode differently.

Its range is narrower than its partner's at both ends as well, so the two do
not even agree on which temperatures they can be asked for.
"""

MIXED_GRID_GROUP = GroupScenario(
    name="mixed_grid_group", profiles=(MIXED_GRID_A, MIXED_GRID_B)
)
"""Two heads that agree on nothing but the room they heat.

Neither profile configures a setpoint step, because a configured step is
entry-wide and would flatten the very axis this scenario exists for.
"""

VALVE_GROUP_WARM = DeviceProfile(
    name="valve_group_warm",
    integration="mqtt",
    calibration="direct_valve_based",
    calibration_mode="mpc_calibration",
    has_device_registry_entry=True,
    entity_id="climate.valve_group_warm_trv",
    entity_name="valve group warm trv",
    current_temperature=21.0,
    valve_channel=ValveChannel.NUMBER_ENTITY,
)
"""The head sitting in the warmer half of a room driven by its valves."""

VALVE_GROUP_COLD = DeviceProfile(
    name="valve_group_cold",
    integration="mqtt",
    calibration="direct_valve_based",
    calibration_mode="mpc_calibration",
    has_device_registry_entry=True,
    entity_id="climate.valve_group_cold_trv",
    entity_name="valve group cold trv",
    current_temperature=18.0,
    valve_channel=ValveChannel.NUMBER_ENTITY,
)
"""The head by the draughty window, three Kelvin colder than its partner.

Same model as its partner and different only in what it measures, so the
opening the two are commanded can differ for one reason alone.
"""

VALVE_GROUP = GroupScenario(
    name="valve_group", profiles=(VALVE_GROUP_WARM, VALVE_GROUP_COLD)
)
"""A room whose two heads are driven by valve position rather than setpoint.

Distributing one group-level valve command across the heads is the only
calibration path that reads more than one head's temperature, and it runs
under the MPC modes alone.
"""

HEAT_ONLY = RoleScenario(
    name="heat_only", trv=GENERIC_HEAT_TRV, cooler=None, cooler_entity_id=None
)

SEPARATE_COOLER = RoleScenario(
    name="separate_cooler",
    trv=GENERIC_HEAT_TRV,
    cooler=ROOM_AC_COOLER,
    cooler_entity_id=COOLER_ID,
)

RANGED_COOLER = RoleScenario(
    name="ranged_cooler",
    trv=GENERIC_HEAT_TRV,
    cooler=RANGED_AC_COOLER,
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
    VALVE_TRV,
)
"""Every profile a test can drive as the single controlled device."""

ROLE_SCENARIOS = (HEAT_ONLY, SEPARATE_COOLER, RANGED_COOLER, DUAL_ROLE)
"""Every wiring of a room a test can drive."""

GROUP_SCENARIOS = (GROUP_OF_THREE, MIXED_GRID_GROUP, VALVE_GROUP)
"""Every group of heads a test can drive as one room."""

SHAPES_FROM_REPORTS: dict[str, DeviceProfile | RoleScenario | GroupScenario] = {
    "a head that offers no heating mode at all": AUTO_COOL_AC,
    "a head whose calibration is a number entity on its device": MQTT_OFFSET_TRV,
    "one entity wired as thermostat and as cooler": DUAL_ROLE,
    "a thermostat entity with no device registry entry": GENERIC_HEAT_TRV,
    "a room with several heads that can disagree": GROUP_OF_THREE,
}
"""The device shapes users have reported a bug against, by what they are.

A report teaches the project a shape of device or wiring it did not have. The
regression test that closes the report covers the one path the bug sat on; the
entry here is what makes the whole integration suite run against that shape
from then on. Adding to this table is half of closing such a report — see
CONTRIBUTING.md.

A report about the config flow or about entity startup has no device shape and
does not belong here; those live in their own scenario files.
"""
