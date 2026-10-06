"""State attributes are published under their current and deprecated names.

Every entry of ``DEPRECATED_STATE_ATTRIBUTES`` pairs a current attribute name
with the name 1.9 published. The entity writes each value under both, so
templates that read the old name keep working and 1.9 can restore from it
after a rollback. Restoring from either name is covered next to the other
restore tests in ``test_climate_startup.py``.
"""

import json

from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.utils.const import (
    ATTR_STATE_PRESET_COOL_TEMPERATURE,
    ATTR_STATE_PRESET_HEAT_TEMPERATURES,
    DEPRECATED_STATE_ATTRIBUTES,
)
from tests.factories import make_state_attributes_bt


def test_every_deprecated_attribute_is_published_with_the_current_value():
    """Each deprecated name carries the value of its current name."""
    entity = make_state_attributes_bt(
        _preset_cool_temperature=24.5,
        _preset_cool_temperatures={"comfort": 25.0},
        temp_slope=0.0012,
    )
    entity.preset_mgr.temperatures = {"comfort": 21.0}

    attrs = BetterThermostat.extra_state_attributes.fget(entity)

    assert attrs[ATTR_STATE_PRESET_COOL_TEMPERATURE] == 24.5
    assert json.loads(attrs[ATTR_STATE_PRESET_HEAT_TEMPERATURES]) == {"comfort": 21.0}
    for name, deprecated_name in DEPRECATED_STATE_ATTRIBUTES.items():
        assert name in attrs
        assert attrs[deprecated_name] == attrs[name]
