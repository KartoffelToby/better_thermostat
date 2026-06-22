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
    bare.config = BtConfig(device_name="Test BT", tolerance=0.3)
    bare.runtime = BtRuntime()
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
        config = BtConfig(device_name="Test BT")
        with pytest.raises(FrozenInstanceError):
            config.tolerance = 1.0

    def test_config_bridges_are_read_only(self):
        """The entity bridges for config fields have no setter."""
        bare = _bare_entity()
        assert bare.tolerance == 0.3
        with pytest.raises(AttributeError):
            bare.tolerance = 1.0
        with pytest.raises(AttributeError):
            bare.device_name = "other"


class TestRuntimeAndLearnedBridges:
    """Runtime and learned bridges read and write their containers."""

    def test_runtime_bridge_roundtrip(self):
        """Writing the historical attribute lands in the runtime container."""
        bare = _bare_entity()
        bare.cur_temp = 21.5
        assert bare.runtime.cur_temp == 21.5
        bare.runtime.call_for_heat = False
        assert bare.call_for_heat is False

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
        assert bare.window_open is True
        with pytest.raises(AttributeError):
            bare.window_open = False
