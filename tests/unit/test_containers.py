"""Tests for the Config/Runtime/Learned containers and their entity bridges."""

from dataclasses import FrozenInstanceError, replace

import pytest

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.core.containers import (
    BtConfig,
    BtRuntime,
    container_field_names,
)


def _bare_entity() -> BetterThermostat:
    """Entity shell with containers attached, without running __init__."""
    bare = object.__new__(BetterThermostat)
    bare.config = BtConfig(
        sensor_entity_id="sensor.room_temperature", device_name="Test BT", tolerance=0.3
    )
    bare.runtime = BtRuntime(heat_target_temperature=21.0)
    return bare


class TestContainerAssignment:
    """Each control attribute lives in exactly one container."""

    def test_containers_are_disjoint(self):
        """No attribute name appears in more than one container."""
        names = container_field_names()
        assert names["config"] & names["runtime"] == frozenset()

    def test_learned_values_have_their_own_homes(self):
        """The learned axis lives in trackers, not in the attribute bag."""
        assert isinstance(BetterThermostat.heating_power, property)
        assert isinstance(BetterThermostat.heat_loss_rate, property)

    def test_every_container_field_has_an_entity_bridge(self):
        """Each container field is exposed as a property on the entity."""
        for container_fields in container_field_names().values():
            for field_name in container_fields:
                attribute = getattr(BetterThermostat, field_name, None)
                assert isinstance(attribute, property), (
                    f"{field_name} has no property bridge"
                )


class TestConfigIsFrozen:
    """Static configuration cannot change after setup."""

    def test_config_container_rejects_writes(self):
        """The container itself is frozen."""
        config = BtConfig(
            sensor_entity_id="sensor.room_temperature", device_name="Test BT"
        )
        with pytest.raises(FrozenInstanceError):
            setattr(config, "tolerance", 1.0)

    def test_config_bridges_are_read_only(self):
        """The entity bridges for config fields have no setter."""
        bare = _bare_entity()
        assert bare.tolerance == 0.3
        with pytest.raises(AttributeError):
            setattr(bare, "tolerance", 1.0)
        with pytest.raises(AttributeError):
            setattr(bare, "device_name", "other")


class TestRuntimeAndLearnedBridges:
    """Runtime and learned bridges read and write their containers."""

    def test_runtime_bridge_roundtrip(self):
        """Writing the flat attribute lands in the runtime container."""
        bare = _bare_entity()
        bare.room_temperature = 21.5
        assert bare.runtime.room_temperature == 21.5
        bare.runtime.call_for_heat = False
        assert bare.call_for_heat is False

    def test_the_room_temperature_is_unknown_to_home_assistant_until_startup(self):
        """Home Assistant reads no room temperature before startup sets one."""
        entity = _bare_entity()

        assert entity.current_temperature is None
        entity.room_temperature = 20.5
        assert entity.current_temperature == 20.5

    def test_reading_the_room_temperature_before_startup_is_an_error(self):
        """A room temperature read before startup measured one is refused."""
        entity = _bare_entity()

        with pytest.raises(RuntimeError, match="before startup"):
            _ = entity.room_temperature
        entity.room_temperature = 20.5
        assert entity.room_temperature == 20.5

    def test_mode_flags_derive_from_the_regions(self):
        """The discrete mode flags are read-only views onto the kernel."""
        from custom_components.better_thermostat.core.decide import running_kernel_state
        from custom_components.better_thermostat.core.fsm.window import (
            WindowPhase,
            WindowState,
        )

        bare = _bare_entity()
        bare.kernel_state = running_kernel_state()
        assert bare.startup_running is False
        assert bare.window_open is False
        bare.kernel_state = replace(
            bare.kernel_state, window=WindowState(phase=WindowPhase.OPEN)
        )
        window_open: bool = bare.window_open
        assert window_open is True
        with pytest.raises(AttributeError):
            setattr(bare, "window_open", False)
