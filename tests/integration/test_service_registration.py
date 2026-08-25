"""The entity services a set-up entry leaves behind, as a complete set.

Setting an entry up registers Better Thermostat's own services on the
integration's domain. Each of them has three parts that have to agree:
the name registered with Home Assistant, the entity method it calls, and
the entry in ``services.yaml`` the UI builds its form from. None of the
three tells you about the other two. A service registered under a
name ``services.yaml`` does not carry has no UI; one documented but not
registered is a dead button; one whose method is gone raises only once a
user presses it.

So the set is claimed in full here, and the three parts are compared
against each other rather than each against a hand-written list.
"""

from pathlib import Path

from homeassistant.helpers import entity_registry as er
import pytest
import voluptuous as vol
import yaml

from custom_components.better_thermostat import __file__ as _component_file
from custom_components.better_thermostat.utils.const import (
    SERVICE_RESET_HEATING_POWER,
    SERVICE_RESET_PID_LEARNINGS,
    SERVICE_RUN_VALVE_MAINTENANCE,
)

from .conftest import (
    BT_ENTITY,
    DOMAIN,
    make_entry,
    profile_id,
    set_room_sensor,
    setup_entry,
    wait_for_startup,
)
from .device_profiles import GENERIC_HEAT_TRV

# Name to entity method, as async_setup_entry registers them.
_EXPECTED_SERVICES = {
    SERVICE_RESET_HEATING_POWER: "reset_heating_power",
    SERVICE_RESET_PID_LEARNINGS: "reset_pid_learnings_service",
    SERVICE_RUN_VALVE_MAINTENANCE: "run_valve_maintenance_service",
}


@pytest.fixture
async def entry_set_up(hass, fake_trv):
    """One started entry; the services live on the domain, not the entity."""
    set_room_sensor(hass, 18.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    return await wait_for_startup(hass, entry)


@pytest.mark.parametrize("fake_trv", [GENERIC_HEAT_TRV], indirect=True, ids=profile_id)
async def test_setup_registers_exactly_the_expected_services(hass, entry_set_up):
    """No service missing and none registered that nothing documents."""
    assert set(hass.services.async_services().get(DOMAIN, {})) == set(
        _EXPECTED_SERVICES
    )


@pytest.mark.parametrize("fake_trv", [GENERIC_HEAT_TRV], indirect=True, ids=profile_id)
async def test_every_registered_service_calls_a_method_the_entity_has(
    hass, entry_set_up
):
    """The name a service dispatches to has to exist on the entity.

    Home Assistant resolves the method when the service is called, so a
    renamed method turns a working button into a runtime error that no
    setup path reaches.
    """
    entity = hass.data["entity_components"]["climate"].get_entity(BT_ENTITY)
    assert entity is not None

    missing = [
        method
        for method in _EXPECTED_SERVICES.values()
        if not callable(getattr(entity, method, None))
    ]
    assert not missing


@pytest.mark.parametrize("fake_trv", [GENERIC_HEAT_TRV], indirect=True, ids=profile_id)
async def test_services_yaml_documents_exactly_the_registered_services(
    hass, entry_set_up
):
    """The UI's form and the running registry describe the same set."""
    services_yaml = Path(_component_file).parent / "services.yaml"
    documented = yaml.safe_load(services_yaml.read_text(encoding="utf-8"))
    assert set(documented) == set(hass.services.async_services().get(DOMAIN, {}))


@pytest.mark.parametrize("fake_trv", [GENERIC_HEAT_TRV], indirect=True, ids=profile_id)
async def test_the_pid_reset_service_accepts_its_documented_fields(hass, entry_set_up):
    """The one service with a schema takes every field its form offers.

    The other two are registered with an empty schema and take nothing
    beyond the entity target, so a field would be rejected there.
    """
    registry = er.async_get(hass)
    assert registry.async_get(BT_ENTITY) is not None

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RESET_PID_LEARNINGS,
        {
            "entity_id": BT_ENTITY,
            "apply_pid_defaults": True,
            "defaults_kp": 1.5,
            "defaults_ki": 0.02,
            "defaults_kd": 0.5,
        },
        blocking=True,
    )

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RESET_HEATING_POWER,
            {"entity_id": BT_ENTITY, "defaults_kp": 1.5},
            blocking=True,
        )
