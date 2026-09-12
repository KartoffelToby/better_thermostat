"""The entity services a set-up entry leaves behind, as a complete set.

Setting an entry up registers Better Thermostat's own services on the
integration's domain. Each of them has three parts that have to agree:
the name registered with Home Assistant, the entity method it calls, and
the entry in ``services.yaml`` the UI builds its form from. None of the
three tells you about the other two. A service registered under a name
that ``services.yaml`` does not carry has no UI; one documented but not
registered is a dead button; one whose method is gone raises only once a
user presses it.

So the names are claimed in full here, once, and everything else is
read from the running integration: the dispatch by calling each service
and watching which method runs, the form by comparing ``services.yaml``
against the registry and the schema.
"""

from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

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
async def test_every_service_dispatches_to_its_own_method(hass, entry_set_up):
    """Calling a service runs that service's method and no other.

    Home Assistant resolves the method by name when the service is called,
    so a renamed method turns a working button into a runtime error that no
    setup path reaches. Checking that the name exists is not enough either:
    a service wired to a different method the entity happens to have would
    pass that check, and only the call reads the pairing that was
    registered.
    """
    entity = hass.data["entity_components"]["climate"].get_entity(BT_ENTITY)
    assert entity is not None

    for service, method in _EXPECTED_SERVICES.items():
        ran = []
        with ExitStack() as stack:
            for name in set(_EXPECTED_SERVICES.values()):
                assert callable(getattr(entity, name, None))
                stack.enter_context(
                    patch.object(
                        entity,
                        name,
                        AsyncMock(side_effect=lambda *a, _n=name, **k: ran.append(_n)),
                    )
                )
            await hass.services.async_call(
                DOMAIN, service, {"entity_id": BT_ENTITY}, blocking=True
            )
        assert ran == [method]


@pytest.mark.parametrize("fake_trv", [GENERIC_HEAT_TRV], indirect=True, ids=profile_id)
async def test_services_yaml_documents_exactly_the_registered_services(
    hass, entry_set_up
):
    """The UI's form and the running registry describe the same set."""
    services_yaml = Path(_component_file).parent / "services.yaml"
    documented = yaml.safe_load(services_yaml.read_text(encoding="utf-8"))
    assert set(documented) == set(hass.services.async_services().get(DOMAIN, {}))


def _a_value_the_selector_accepts(field):
    """A value for one documented field, read from the field's own selector."""
    selector = field["selector"]
    if "boolean" in selector:
        return True
    if "number" in selector:
        return selector["number"]["min"]
    raise AssertionError(f"no value known for selector {sorted(selector)}")


@pytest.mark.parametrize("fake_trv", [GENERIC_HEAT_TRV], indirect=True, ids=profile_id)
async def test_the_pid_reset_service_accepts_its_documented_fields(hass, entry_set_up):
    """The one service with a schema takes every field its form offers.

    The fields and their values come out of ``services.yaml``, so a field
    renamed or added in the form and not in the schema fails here rather
    than at the user's button.

    The other two are registered with an empty schema and take nothing
    beyond the entity target, so a field would be rejected there.
    """
    registry = er.async_get(hass)
    assert registry.async_get(BT_ENTITY) is not None

    services_yaml = Path(_component_file).parent / "services.yaml"
    documented = yaml.safe_load(services_yaml.read_text(encoding="utf-8"))
    fields = documented[SERVICE_RESET_PID_LEARNINGS]["fields"]
    assert fields, "this is the one service whose form offers fields"

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RESET_PID_LEARNINGS,
        {
            "entity_id": BT_ENTITY,
            **{
                name: _a_value_the_selector_accepts(field)
                for name, field in fields.items()
            },
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
