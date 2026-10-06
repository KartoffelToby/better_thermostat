"""The preset attributes are published under their current and deprecated names.

The entity publishes ``preset_cool_temperature``, ``preset_cool_temperatures``
and ``preset_heat_temperatures``, and each one a second time under its
``bt_``-prefixed name, which templates read and from which 1.9 restores its
presets after a rollback. Restoring from either name is covered next to the
other restore tests in ``test_climate_startup.py``.
"""

import json

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.utils.const import (
    ATTR_STATE_PRESET_COOL_TEMPERATURE,
    ATTR_STATE_PRESET_HEAT_TEMPERATURES,
    DEPRECATED_PRESET_ATTRIBUTES,
)
from tests.factories import make_state_attributes_bt


def test_every_preset_attribute_is_published_under_both_names():
    """The deprecated name carries the same value as the current one."""
    entity = make_state_attributes_bt(
        _preset_cool_temperature=24.5, _preset_cool_temperatures={"comfort": 25.0}
    )
    entity.preset_mgr.temperatures = {"comfort": 21.0}

    attrs = BetterThermostat.extra_state_attributes.fget(entity)

    assert attrs[ATTR_STATE_PRESET_COOL_TEMPERATURE] == 24.5
    assert json.loads(attrs[ATTR_STATE_PRESET_HEAT_TEMPERATURES]) == {"comfort": 21.0}
    for name, deprecated_name in DEPRECATED_PRESET_ATTRIBUTES.items():
        assert attrs[deprecated_name] == attrs[name]
