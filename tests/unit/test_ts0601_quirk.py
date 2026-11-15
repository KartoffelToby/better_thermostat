"""A TS0601 is driven by its manufacturer string.

Only the Saswell SEA801/SEA802 and its white labels get their setpoint and
offset lifted, as under Zigbee2MQTT; every other TS0601 passes its
calibration through unchanged.
"""

import importlib
from unittest.mock import MagicMock, patch

from homeassistant.core import State
from homeassistant.helpers import device_registry as dr
import pytest

from tests.factories import (
    ThermostatStandIn,
    make_entity_registry,
    make_registry_entry,
    trv_record,
)

ENTITY_ID = "climate.trv"
_MODULE = "custom_components.better_thermostat.model_fixes.TS0601"

TS0601 = importlib.import_module(_MODULE)
SASWELL = importlib.import_module(
    "custom_components.better_thermostat.model_fixes.SEA801-Zigbee_SEA802-Zigbee"
)

# The Hama SEA802 the lift was written for, and a Nous H1Z white label.
SASWELL_HEADS = ["_TZE200_yw7cahqs", "_TZE284_3ymoslep"]
# A TV02, an ME167 and a manufacturer string nobody lists.
OTHER_HEADS = ["_TZE200_hue3yfsn", "_TZE200_bvu2wnxz", "_TZE200_unknown0"]


def _host(trv_reading=20.0, room=21.0, target=21.0):
    """A host whose TRV reads ``trv_reading`` in a room at ``room``."""
    host = ThermostatStandIn()
    host.device_name = "Test BT"
    host.hass = MagicMock()
    host.hass.config.units.temperature_unit = "°C"
    host.hass.states.get = MagicMock(
        return_value=State(ENTITY_ID, "heat", {"current_temperature": trv_reading})
    )
    host.room_temperature = room
    host.heat_target_temperature = target
    host.real_trvs = {ENTITY_ID: trv_record(entity_id=ENTITY_ID)}
    return host


def _registries(manufacturer, device_id="dev1"):
    """Patch the registries so the TRV's device names ``manufacturer``."""
    entity_reg = make_entity_registry(
        make_registry_entry(ENTITY_ID, device_id=device_id)
    )
    device = dr.DeviceEntry(
        config_entry_id="entry",
        manufacturer=manufacturer,
        model="TS0601",
        identifiers={("zha", "a4:c1:38:00:00:00:00:01")},
    )
    dev_reg = MagicMock()
    dev_reg.async_get.return_value = device
    return (
        patch(
            "homeassistant.helpers.entity_registry.async_get", return_value=entity_reg
        ),
        patch("homeassistant.helpers.device_registry.async_get", return_value=dev_reg),
    )


class TestASaswellHeadIsLifted:
    """The Saswell head answers what the SEA801 module answers."""

    @pytest.mark.parametrize("manufacturer", SASWELL_HEADS)
    def test_the_setpoint_is_lifted(self, manufacturer):
        """A setpoint less than 1.5 K above the TRV goes to its reading + 1.5."""
        host = _host(trv_reading=20.0)
        er_patch, dr_patch = _registries(manufacturer)
        with er_patch, dr_patch:
            answer = TS0601.fix_target_temperature_calibration(host, ENTITY_ID, 21.0)

        assert answer == pytest.approx(21.5)
        assert answer == SASWELL.fix_target_temperature_calibration(
            host, ENTITY_ID, 21.0
        )

    @pytest.mark.parametrize("manufacturer", SASWELL_HEADS)
    def test_the_offset_is_lifted_at_the_target(self, manufacturer):
        """A room at the target lifts the offset by 0.5 K."""
        host = _host(room=21.0, target=21.0)
        er_patch, dr_patch = _registries(manufacturer)
        with er_patch, dr_patch:
            answer = TS0601.fix_local_calibration(host, ENTITY_ID, -1.0)

        assert answer == pytest.approx(-0.5)


class TestEveryOtherTs0601PassesThrough:
    """A TS0601 the lift was not written for keeps its calibration."""

    @pytest.mark.parametrize("manufacturer", OTHER_HEADS)
    def test_the_setpoint_is_unchanged(self, manufacturer):
        """The setpoint goes out as Better Thermostat computed it."""
        host = _host(trv_reading=20.0)
        er_patch, dr_patch = _registries(manufacturer)
        with er_patch, dr_patch:
            answer = TS0601.fix_target_temperature_calibration(host, ENTITY_ID, 21.0)

        assert answer == 21.0

    @pytest.mark.parametrize("manufacturer", OTHER_HEADS)
    @pytest.mark.parametrize(
        ("room", "calibration_offset"), [(21.0, -1.0), (20.7, -1.0)]
    )
    def test_the_offset_is_unchanged(self, manufacturer, room, calibration_offset):
        """Neither the lift at the target nor the drop just below it applies."""
        host = _host(room=room, target=21.0)
        er_patch, dr_patch = _registries(manufacturer)
        with er_patch, dr_patch:
            answer = TS0601.fix_local_calibration(host, ENTITY_ID, calibration_offset)

        assert answer == calibration_offset

    def test_an_entity_without_a_device_passes_through(self):
        """Without a device there is no manufacturer to match."""
        host = _host(trv_reading=20.0)
        entity_reg = make_entity_registry(
            make_registry_entry(ENTITY_ID, device_id=None)
        )
        with patch(
            "homeassistant.helpers.entity_registry.async_get", return_value=entity_reg
        ):
            answer = TS0601.fix_target_temperature_calibration(host, ENTITY_ID, 21.0)

        assert answer == 21.0
