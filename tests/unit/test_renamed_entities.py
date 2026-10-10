"""The settings of an entry name an entity under the id it was given last.

``settings_with_entity_renamed`` rewrites every setting that holds the old
entity id and keeps everything else as stored, including what it cannot
read: the function also runs for entries whose settings never parsed.
"""

from custom_components.better_thermostat.utils.renamed_entities import (
    ENTITY_SETTINGS,
    settings_with_entity_renamed,
)

OLD = "climate.old"
NEW = "climate.new"


def _settings() -> dict[str, object]:
    return {
        "name": "Living room",
        "thermostat": [
            {
                "trv": OLD,
                "integration": "mqtt",
                "advanced": {"calibration_mode": "pid_calibration", "child_lock": True},
            },
            {"trv": "climate.other", "integration": "mqtt"},
        ],
        "temperature_sensor": "sensor.room",
        "tolerance": 0.3,
    }


def test_a_thermostat_is_renamed_with_its_advanced_options_kept():
    settings = _settings()

    renamed = settings_with_entity_renamed(settings, OLD, NEW)

    assert renamed == {
        **settings,
        "thermostat": [
            {
                "trv": NEW,
                "integration": "mqtt",
                "advanced": {"calibration_mode": "pid_calibration", "child_lock": True},
            },
            {"trv": "climate.other", "integration": "mqtt"},
        ],
    }


def test_the_stored_settings_are_left_as_they_are():
    settings = _settings()

    settings_with_entity_renamed(settings, OLD, NEW)

    assert settings == _settings()


def test_every_single_entity_setting_is_renamed():
    settings = dict.fromkeys(ENTITY_SETTINGS, OLD) | {"tolerance": 0.3}

    renamed = settings_with_entity_renamed(settings, OLD, NEW)

    assert renamed == dict.fromkeys(ENTITY_SETTINGS, NEW) | {"tolerance": 0.3}


def test_settings_that_do_not_name_the_entity_need_no_rewrite():
    assert settings_with_entity_renamed(_settings(), "climate.unrelated", NEW) is None


def test_an_unreadable_thermostat_list_is_kept_as_stored():
    settings = {
        "thermostat": ["climate.old", {"integration": "mqtt"}],
        "temperature_sensor": OLD,
    }

    renamed = settings_with_entity_renamed(settings, OLD, NEW)

    assert renamed == {
        "thermostat": ["climate.old", {"integration": "mqtt"}],
        "temperature_sensor": NEW,
    }
    assert settings_with_entity_renamed({"thermostat": OLD}, OLD, NEW) is None
