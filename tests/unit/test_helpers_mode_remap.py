"""Tests for mode_remap function.

This module tests the mode_remap function which handles HVAC mode translation
between Better Thermostat and TRVs. This includes handling quirks like
heat_auto_swapped devices and TRVs that only support HEAT_COOL but not HEAT.
"""

import logging

from homeassistant.components.climate.const import HVACMode

from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.helpers import (
    get_hvac_bt_mode,
    mode_remap,
)

HELPERS_LOGGER = "custom_components.better_thermostat.utils.helpers"


def _unsupported_records(caplog):
    """Return the log records annunciating an unsupported HVAC mode."""
    return [
        record
        for record in caplog.records
        if "does not offer HVAC mode" in record.getMessage()
    ]


class MockThermostat:
    """Mock Better Thermostat instance for testing."""

    def __init__(self, device_name="Test"):
        """Initialize mock thermostat."""
        self.device_name = device_name
        self.real_trvs = {}

    def add_trv(self, entity_id, heat_auto_swapped=False, hvac_modes=None):
        """Add a TRV configuration."""
        if hvac_modes is None:
            hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.AUTO]

        self.real_trvs[entity_id] = Trv.from_legacy_dict(
            entity_id,
            {
                "advanced": {"heat_auto_swapped": heat_auto_swapped},
                "hvac_modes": hvac_modes,
            },
        )


class TestModeRemapBasic:
    """Test basic mode_remap functionality."""

    def test_returns_mode_unchanged_when_no_remapping_needed(self):
        """Test that modes are returned unchanged when no remapping is needed."""
        mock_bt = MockThermostat()
        mock_bt.add_trv("climate.test")

        # OFF should stay OFF
        result = mode_remap(mock_bt, "climate.test", HVACMode.OFF)
        assert result == HVACMode.OFF

        # HEAT should stay HEAT
        result = mode_remap(mock_bt, "climate.test", HVACMode.HEAT)
        assert result == HVACMode.HEAT

    def test_returns_off_for_unsupported_auto_mode(self):
        """Test that AUTO mode returns OFF when not supported and logs error."""
        mock_bt = MockThermostat()
        mock_bt.add_trv("climate.test", hvac_modes=[HVACMode.OFF, HVACMode.HEAT])

        result = mode_remap(mock_bt, "climate.test", HVACMode.AUTO)
        assert result == HVACMode.OFF


class TestModeRemapHeatAutoSwapped:
    """Test mode_remap with heat_auto_swapped configuration."""

    def test_outbound_heat_becomes_auto_when_swapped(self):
        """Test that HEAT becomes AUTO for outbound when heat_auto_swapped."""
        mock_bt = MockThermostat()
        mock_bt.add_trv("climate.test", heat_auto_swapped=True)

        result = mode_remap(mock_bt, "climate.test", HVACMode.HEAT, inbound=False)
        assert result == HVACMode.AUTO

    def test_inbound_auto_becomes_heat_when_swapped(self):
        """Test that AUTO becomes HEAT for inbound when heat_auto_swapped."""
        mock_bt = MockThermostat()
        mock_bt.add_trv("climate.test", heat_auto_swapped=True)

        result = mode_remap(mock_bt, "climate.test", HVACMode.AUTO, inbound=True)
        assert result == HVACMode.HEAT

    def test_other_modes_unchanged_when_swapped(self):
        """Test that other modes are unchanged when heat_auto_swapped."""
        mock_bt = MockThermostat()
        mock_bt.add_trv(
            "climate.test",
            heat_auto_swapped=True,
            hvac_modes=[HVACMode.OFF, HVACMode.HEAT, HVACMode.AUTO, HVACMode.COOL],
        )

        # OFF should stay OFF
        result = mode_remap(mock_bt, "climate.test", HVACMode.OFF, inbound=False)
        assert result == HVACMode.OFF

        # COOL should stay COOL
        result = mode_remap(mock_bt, "climate.test", HVACMode.COOL, inbound=False)
        assert result == HVACMode.COOL

    def test_heat_auto_swap_takes_precedence(self):
        """Test that heat_auto_swapped takes precedence over other remapping."""
        mock_bt = MockThermostat()
        # TRV that supports HEAT_COOL but has heat_auto_swapped set
        mock_bt.add_trv(
            "climate.test",
            heat_auto_swapped=True,
            hvac_modes=[HVACMode.OFF, HVACMode.AUTO, HVACMode.HEAT_COOL],
        )

        # Should swap HEAT to AUTO, not to HEAT_COOL
        result = mode_remap(mock_bt, "climate.test", HVACMode.HEAT, inbound=False)
        assert result == HVACMode.AUTO


class TestModeRemapHeatCoolTranslation:
    """Test mode_remap translation between HEAT and HEAT_COOL."""

    def test_outbound_heat_becomes_heat_cool_when_no_heat_support(self):
        """Test HEAT becomes HEAT_COOL when TRV only supports HEAT_COOL."""
        mock_bt = MockThermostat()
        mock_bt.add_trv("climate.test", hvac_modes=[HVACMode.OFF, HVACMode.HEAT_COOL])

        result = mode_remap(mock_bt, "climate.test", HVACMode.HEAT, inbound=False)
        assert result == HVACMode.HEAT_COOL

    def test_inbound_heat_cool_becomes_heat_when_no_heat_support(self):
        """Test HEAT_COOL becomes HEAT when receiving from TRV."""
        mock_bt = MockThermostat()
        mock_bt.add_trv("climate.test", hvac_modes=[HVACMode.OFF, HVACMode.HEAT_COOL])

        result = mode_remap(mock_bt, "climate.test", HVACMode.HEAT_COOL, inbound=True)
        assert result == HVACMode.HEAT

    def test_no_translation_when_heat_is_supported(self):
        """Test that HEAT is not translated when TRV supports it."""
        mock_bt = MockThermostat()
        mock_bt.add_trv(
            "climate.test", hvac_modes=[HVACMode.OFF, HVACMode.HEAT, HVACMode.HEAT_COOL]
        )

        result = mode_remap(mock_bt, "climate.test", HVACMode.HEAT, inbound=False)
        assert result == HVACMode.HEAT

    def test_heat_cool_stays_when_both_supported(self):
        """Test that HEAT_COOL stays when both HEAT and HEAT_COOL supported."""
        mock_bt = MockThermostat()
        mock_bt.add_trv(
            "climate.test", hvac_modes=[HVACMode.OFF, HVACMode.HEAT, HVACMode.HEAT_COOL]
        )

        result = mode_remap(mock_bt, "climate.test", HVACMode.HEAT_COOL, inbound=False)
        assert result == HVACMode.HEAT_COOL


class TestModeRemapEdgeCases:
    """Test edge cases and potential bugs."""

    def test_missing_entity_id_passes_mode_through(self):
        """An untracked entity_id leaves the mode unchanged instead of raising."""
        mock_bt = MockThermostat()
        # Don't add any TRVs

        result = mode_remap(mock_bt, "climate.missing", HVACMode.HEAT, inbound=False)
        assert result == HVACMode.HEAT

    def test_missing_advanced_config_defaults_to_no_swap(self):
        """Without advanced config the Trv defaults make remap a no-op."""
        mock_bt = MockThermostat()
        # Trv without advanced config: defaults to an empty dict
        mock_bt.real_trvs["climate.test"] = Trv.from_legacy_dict(
            "climate.test", {"hvac_modes": [HVACMode.OFF, HVACMode.HEAT]}
        )

        result = mode_remap(mock_bt, "climate.test", HVACMode.HEAT, inbound=False)
        assert result == HVACMode.HEAT

    def test_unreported_hvac_modes_pass_through(self):
        """hvac_modes=None (device never reported) leaves the mode unchanged.

        convert_outbound_states then handles the device via its
        no-system-mode branch instead of aborting on an exception.
        """
        mock_bt = MockThermostat()
        mock_bt.real_trvs["climate.test"] = Trv.from_legacy_dict(
            "climate.test", {"advanced": {"heat_auto_swapped": False}}
        )

        result = mode_remap(mock_bt, "climate.test", HVACMode.HEAT, inbound=False)
        assert result == HVACMode.HEAT

    def test_cool_mode_handling(self):
        """Test handling of COOL mode."""
        mock_bt = MockThermostat()
        mock_bt.add_trv(
            "climate.test", hvac_modes=[HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL]
        )

        result = mode_remap(mock_bt, "climate.test", HVACMode.COOL, inbound=False)
        assert result == HVACMode.COOL

    def test_dry_mode_handling(self):
        """Test handling of DRY mode."""
        mock_bt = MockThermostat()
        mock_bt.add_trv(
            "climate.test", hvac_modes=[HVACMode.OFF, HVACMode.HEAT, HVACMode.DRY]
        )

        result = mode_remap(mock_bt, "climate.test", HVACMode.DRY, inbound=False)
        assert result == HVACMode.DRY

    def test_fan_only_mode_handling(self):
        """Test handling of FAN_ONLY mode."""
        mock_bt = MockThermostat()
        mock_bt.add_trv(
            "climate.test", hvac_modes=[HVACMode.OFF, HVACMode.HEAT, HVACMode.FAN_ONLY]
        )

        result = mode_remap(mock_bt, "climate.test", HVACMode.FAN_ONLY, inbound=False)
        assert result == HVACMode.FAN_ONLY


class TestModeRemapUnsupportedOutboundMode:
    """An outbound mode the device does not offer resolves to no mode write."""

    CHANGEOVER_MODES = [HVACMode.AUTO, HVACMode.COOL, HVACMode.OFF]

    def test_heat_cool_not_offered_returns_none(self):
        """HEAT_COOL is dropped when the device lists neither HEAT nor HEAT_COOL."""
        mock_bt = MockThermostat()
        mock_bt.add_trv("climate.test", hvac_modes=self.CHANGEOVER_MODES)

        result = mode_remap(mock_bt, "climate.test", HVACMode.HEAT_COOL, inbound=False)
        assert result is None

    def test_heat_not_offered_returns_none(self):
        """HEAT is dropped when the device only offers auto, cool and off."""
        mock_bt = MockThermostat()
        mock_bt.add_trv("climate.test", hvac_modes=self.CHANGEOVER_MODES)

        result = mode_remap(mock_bt, "climate.test", HVACMode.HEAT, inbound=False)
        assert result is None

    def test_offered_mode_passes_through(self):
        """A mode the device does list is written unchanged."""
        mock_bt = MockThermostat()
        mock_bt.add_trv("climate.test", hvac_modes=self.CHANGEOVER_MODES)

        result = mode_remap(mock_bt, "climate.test", HVACMode.COOL, inbound=False)
        assert result == HVACMode.COOL

    def test_off_is_exempt_from_the_clamp(self):
        """OFF survives on a device without OFF so min_temp can substitute."""
        mock_bt = MockThermostat()
        mock_bt.add_trv("climate.test", hvac_modes=[HVACMode.AUTO, HVACMode.HEAT])

        result = mode_remap(mock_bt, "climate.test", HVACMode.OFF, inbound=False)
        assert result == HVACMode.OFF

    def test_inbound_modes_are_never_clamped(self):
        """Values reported by the device pass through even when unlisted."""
        mock_bt = MockThermostat()
        mock_bt.add_trv("climate.test", hvac_modes=[HVACMode.OFF, HVACMode.HEAT])

        assert mode_remap(mock_bt, "climate.test", "cool", inbound=True) == "cool"
        assert mode_remap(mock_bt, "climate.test", "dry", inbound=True) == "dry"

    def test_inbound_auto_still_reports_off(self):
        """The AUTO branch keeps precedence over the inbound exemption."""
        mock_bt = MockThermostat()
        mock_bt.add_trv("climate.test", hvac_modes=[HVACMode.OFF, HVACMode.HEAT])

        result = mode_remap(mock_bt, "climate.test", "auto", inbound=True)
        assert result == HVACMode.OFF

    def test_dry_not_offered_returns_none(self):
        """A heat-only device does not receive DRY."""
        mock_bt = MockThermostat()
        mock_bt.add_trv("climate.test", hvac_modes=[HVACMode.OFF, HVACMode.HEAT])

        result = mode_remap(mock_bt, "climate.test", HVACMode.DRY, inbound=False)
        assert result is None

    def test_auto_branch_wins_over_the_clamp(self, caplog):
        """AUTO reports OFF and names the heat auto swapped option."""
        mock_bt = MockThermostat()
        mock_bt.add_trv("climate.test", hvac_modes=self.CHANGEOVER_MODES)

        with caplog.at_level(logging.ERROR, logger=HELPERS_LOGGER):
            result = mode_remap(mock_bt, "climate.test", HVACMode.AUTO, inbound=False)

        assert result == HVACMode.OFF
        swap_hints = [
            record
            for record in caplog.records
            if "heat auto swapped" in record.getMessage()
        ]
        assert len(swap_hints) == 1

    def test_error_is_logged_once_per_mode(self, caplog):
        """Repeated cycles annunciate each unsupported mode a single time."""
        mock_bt = MockThermostat()
        mock_bt.add_trv("climate.test", hvac_modes=self.CHANGEOVER_MODES)

        with caplog.at_level(logging.ERROR, logger=HELPERS_LOGGER):
            for _ in range(5):
                assert (
                    mode_remap(
                        mock_bt, "climate.test", HVACMode.HEAT_COOL, inbound=False
                    )
                    is None
                )

            assert len(_unsupported_records(caplog)) == 1

            assert (
                mode_remap(mock_bt, "climate.test", HVACMode.HEAT, inbound=False)
                is None
            )
            assert len(_unsupported_records(caplog)) == 2

    def test_swapped_auto_not_offered_returns_none(self):
        """A swapped device offering neither AUTO nor HEAT gets no mode."""
        mock_bt = MockThermostat()
        mock_bt.add_trv(
            "climate.test",
            heat_auto_swapped=True,
            hvac_modes=[HVACMode.OFF, HVACMode.COOL],
        )

        result = mode_remap(mock_bt, "climate.test", HVACMode.HEAT, inbound=False)
        assert result is None

    def test_swapped_auto_offered_passes_through(self):
        """A swapped device listing AUTO still receives AUTO for HEAT."""
        mock_bt = MockThermostat()
        mock_bt.add_trv(
            "climate.test",
            heat_auto_swapped=True,
            hvac_modes=[HVACMode.OFF, HVACMode.HEAT, HVACMode.AUTO],
        )

        result = mode_remap(mock_bt, "climate.test", HVACMode.HEAT, inbound=False)
        assert result == HVACMode.AUTO

    def test_swapped_inbound_auto_survives_an_auto_less_list(self):
        """The inbound swap does not depend on AUTO being listed."""
        mock_bt = MockThermostat()
        mock_bt.add_trv(
            "climate.test",
            heat_auto_swapped=True,
            hvac_modes=[HVACMode.OFF, HVACMode.HEAT],
        )

        result = mode_remap(mock_bt, "climate.test", HVACMode.AUTO, inbound=True)
        assert result == HVACMode.HEAT

    def test_unreported_mode_list_is_not_clamped(self):
        """hvac_modes=None keeps the pass-through for no-system-mode devices."""
        mock_bt = MockThermostat()
        mock_bt.real_trvs["climate.test"] = Trv.from_legacy_dict(
            "climate.test", {"advanced": {"heat_auto_swapped": False}}
        )

        result = mode_remap(mock_bt, "climate.test", HVACMode.HEAT_COOL, inbound=False)
        assert result == HVACMode.HEAT_COOL

    def test_plain_string_mode_list_is_normalized(self):
        """A list of plain strings matches HVACMode members."""
        mock_bt = MockThermostat()
        mock_bt.add_trv("climate.test", hvac_modes=["heat", "off"])

        result = mode_remap(mock_bt, "climate.test", HVACMode.HEAT, inbound=False)
        assert result == HVACMode.HEAT

    def test_hint_names_disabling_the_swap_when_it_is_on(self, caplog):
        """A swapped device offering neither AUTO nor HEAT names the swap."""
        mock_bt = MockThermostat()
        mock_bt.add_trv(
            "climate.test",
            heat_auto_swapped=True,
            hvac_modes=[HVACMode.OFF, HVACMode.COOL],
        )

        with caplog.at_level(logging.ERROR, logger=HELPERS_LOGGER):
            assert (
                mode_remap(mock_bt, "climate.test", HVACMode.HEAT, inbound=False)
                is None
            )

        records = _unsupported_records(caplog)
        assert len(records) == 1
        message = records[0].getMessage()
        assert "Disable the heat auto swapped option" in message
        assert "enable the heat auto swapped" not in message

    def test_hint_names_enabling_the_swap_when_it_is_off(self, caplog):
        """An unswapped device is told about the option it has not set."""
        mock_bt = MockThermostat()
        mock_bt.add_trv("climate.test", hvac_modes=self.CHANGEOVER_MODES)

        with caplog.at_level(logging.ERROR, logger=HELPERS_LOGGER):
            assert (
                mode_remap(mock_bt, "climate.test", HVACMode.HEAT, inbound=False)
                is None
            )

        records = _unsupported_records(caplog)
        assert len(records) == 1
        message = records[0].getMessage()
        assert "enable the heat auto swapped option" in message
        assert "Disable the heat auto swapped option" not in message


def _fallback_records(caplog):
    """Return the log records announcing the unswapped fallback."""
    return [
        record
        for record in caplog.records
        if "Writing heat instead" in record.getMessage()
    ]


class TestModeRemapSwapFallback:
    """A swapped device without AUTO still receives the mode BT wants."""

    def test_heat_is_written_when_auto_is_missing(self):
        """The swap's output is unwritable, so the original HEAT is written."""
        mock_bt = MockThermostat()
        mock_bt.add_trv(
            "climate.test",
            heat_auto_swapped=True,
            hvac_modes=[HVACMode.OFF, HVACMode.HEAT],
        )

        result = mode_remap(mock_bt, "climate.test", HVACMode.HEAT, inbound=False)
        assert result == HVACMode.HEAT

    def test_auto_still_wins_when_the_device_offers_it(self):
        """The swap keeps its effect on the devices it was meant for."""
        mock_bt = MockThermostat()
        mock_bt.add_trv(
            "climate.test",
            heat_auto_swapped=True,
            hvac_modes=[HVACMode.OFF, HVACMode.HEAT, HVACMode.AUTO],
        )

        result = mode_remap(mock_bt, "climate.test", HVACMode.HEAT, inbound=False)
        assert result == HVACMode.AUTO

    def test_no_fallback_when_heat_is_not_offered_either(self):
        """The fallback never resurrects a mode the device does not offer."""
        mock_bt = MockThermostat()
        mock_bt.add_trv(
            "climate.test",
            heat_auto_swapped=True,
            hvac_modes=[HVACMode.OFF, HVACMode.COOL],
        )

        result = mode_remap(mock_bt, "climate.test", HVACMode.HEAT, inbound=False)
        assert result is None

    def test_fallback_matches_a_prefixed_mode_spelling(self):
        """A mode list spelled "HVACMode.HEAT" is recognised as offering HEAT."""
        mock_bt = MockThermostat()
        mock_bt.add_trv(
            "climate.test",
            heat_auto_swapped=True,
            hvac_modes=["HVACMode.OFF", "HVACMode.HEAT"],
        )

        result = mode_remap(mock_bt, "climate.test", HVACMode.HEAT, inbound=False)
        assert result == HVACMode.HEAT

    def test_fallback_does_not_fire_inbound(self):
        """An inbound AUTO keeps becoming HEAT without consulting the list."""
        mock_bt = MockThermostat()
        mock_bt.add_trv(
            "climate.test",
            heat_auto_swapped=True,
            hvac_modes=[HVACMode.OFF, HVACMode.COOL],
        )

        assert (
            mode_remap(mock_bt, "climate.test", HVACMode.AUTO, inbound=True)
            == HVACMode.HEAT
        )
        assert mode_remap(mock_bt, "climate.test", "dry", inbound=True) == "dry"

    def test_fallback_leaves_the_auto_error_branch_alone(self, caplog):
        """An unswapped device still answers AUTO with OFF and its own error."""
        mock_bt = MockThermostat()
        mock_bt.add_trv("climate.test", hvac_modes=[HVACMode.OFF, HVACMode.HEAT])

        with caplog.at_level(logging.ERROR, logger=HELPERS_LOGGER):
            result = mode_remap(mock_bt, "climate.test", HVACMode.AUTO, inbound=False)

        assert result == HVACMode.OFF
        assert not _fallback_records(caplog)

    def test_fallback_is_announced_once(self, caplog):
        """Repeated cycles announce the substitution a single time."""
        mock_bt = MockThermostat()
        mock_bt.add_trv(
            "climate.test",
            heat_auto_swapped=True,
            hvac_modes=[HVACMode.OFF, HVACMode.HEAT],
        )

        with caplog.at_level(logging.WARNING, logger=HELPERS_LOGGER):
            for _ in range(5):
                assert (
                    mode_remap(mock_bt, "climate.test", HVACMode.HEAT, inbound=False)
                    == HVACMode.HEAT
                )

        records = _fallback_records(caplog)
        assert len(records) == 1
        assert "Disable the heat auto swapped option" in records[0].getMessage()


class TestModeRemapSwappedDeviceInACoolerRoom:
    """A swapped radiator sharing a room with a separate cooler.

    Such an instance offers HEAT_COOL in place of HEAT, so HEAT_COOL is the
    mode its heat demand is carried in and the mode every TRV in that room is
    driven with.
    """

    def test_heat_cool_reaches_a_swapped_device_as_auto(self):
        """A room-level heat demand arrives as the device's heating mode."""
        mock_bt = MockThermostat()
        mock_bt.add_trv(
            "climate.test",
            heat_auto_swapped=True,
            hvac_modes=[HVACMode.OFF, HVACMode.AUTO],
        )

        result = mode_remap(mock_bt, "climate.test", HVACMode.HEAT_COOL, inbound=False)
        assert result == HVACMode.AUTO

    def test_heat_cool_falls_back_to_heat_when_auto_is_missing(self):
        """The swap's output is unwritable, so the unswapped mode is written."""
        mock_bt = MockThermostat()
        mock_bt.add_trv(
            "climate.test",
            heat_auto_swapped=True,
            hvac_modes=[HVACMode.OFF, HVACMode.HEAT],
        )

        result = mode_remap(mock_bt, "climate.test", HVACMode.HEAT_COOL, inbound=False)
        assert result == HVACMode.HEAT

    def test_heat_cool_survives_on_a_device_that_offers_only_that(self):
        """HEAT_COOL is the last resort, ahead of writing no mode at all.

        The unswapped path already treats HEAT_COOL as the heating mode of a
        device offering nothing narrower; the swap resolves it the same way.
        """
        mock_bt = MockThermostat()
        mock_bt.add_trv(
            "climate.test",
            heat_auto_swapped=True,
            hvac_modes=[HVACMode.OFF, HVACMode.HEAT_COOL],
        )

        assert (
            mode_remap(mock_bt, "climate.test", HVACMode.HEAT_COOL, inbound=False)
            == HVACMode.HEAT_COOL
        )
        assert (
            mode_remap(mock_bt, "climate.test", HVACMode.HEAT, inbound=False)
            == HVACMode.HEAT_COOL
        )

    def test_heat_outranks_heat_cool_as_a_fallback(self):
        """A radiator offering both receives the single-setpoint mode."""
        mock_bt = MockThermostat()
        mock_bt.add_trv(
            "climate.test",
            heat_auto_swapped=True,
            hvac_modes=[HVACMode.OFF, HVACMode.HEAT, HVACMode.HEAT_COOL],
        )

        result = mode_remap(mock_bt, "climate.test", HVACMode.HEAT_COOL, inbound=False)
        assert result == HVACMode.HEAT

    def test_heat_cool_is_dropped_when_neither_mode_is_offered(self, caplog):
        """A device offering none of the three keeps its own, and says so once."""
        mock_bt = MockThermostat()
        mock_bt.add_trv(
            "climate.test",
            heat_auto_swapped=True,
            hvac_modes=[HVACMode.OFF, HVACMode.COOL],
        )

        with caplog.at_level(logging.ERROR, logger=HELPERS_LOGGER):
            result = mode_remap(
                mock_bt, "climate.test", HVACMode.HEAT_COOL, inbound=False
            )

        assert result is None
        records = _unsupported_records(caplog)
        assert len(records) == 1
        # The mode named is the one the swap asked for, not the room's.
        assert "HVAC mode auto" in records[0].getMessage()

    def test_heat_and_heat_cool_reach_the_device_identically(self):
        """The two spellings of the same demand produce the same write.

        A caller that narrows HEAT_COOL to HEAT before calling — which is what
        a device carrying both the heating and the cooling role receives — and
        one that passes the room's mode through reach the same device mode, so
        the narrowing neither adds to nor subtracts from what arrives.
        """
        for hvac_modes in (
            [HVACMode.OFF, HVACMode.AUTO],
            [HVACMode.OFF, HVACMode.HEAT],
            [HVACMode.OFF, HVACMode.HEAT_COOL],
            [HVACMode.OFF, HVACMode.HEAT, HVACMode.AUTO],
            [HVACMode.OFF, HVACMode.HEAT, HVACMode.HEAT_COOL],
            [HVACMode.OFF, HVACMode.COOL],
            [HVACMode.OFF, HVACMode.AUTO, HVACMode.HEAT_COOL],
        ):
            mock_bt = MockThermostat()
            mock_bt.add_trv(
                "climate.test", heat_auto_swapped=True, hvac_modes=hvac_modes
            )

            via_heat = mode_remap(mock_bt, "climate.test", HVACMode.HEAT, inbound=False)
            via_heat_cool = mode_remap(
                mock_bt, "climate.test", HVACMode.HEAT_COOL, inbound=False
            )
            assert via_heat == via_heat_cool, hvac_modes

    def test_inbound_auto_becomes_heat_for_a_cooler_room_too(self):
        """The device's heating mode is adopted as the instance's HEAT.

        HEAT is what the instance stores for "the room wants heat" whether or
        not a cooler is configured; a room with one re-expresses it as
        HEAT_COOL when it publishes its own mode.
        """
        mock_bt = MockThermostat()
        mock_bt.map_on_hvac_mode = HVACMode.HEAT_COOL
        mock_bt.add_trv(
            "climate.test",
            heat_auto_swapped=True,
            hvac_modes=[HVACMode.OFF, HVACMode.AUTO],
        )

        adopted = mode_remap(mock_bt, "climate.test", HVACMode.AUTO, inbound=True)
        assert adopted == HVACMode.HEAT
        assert get_hvac_bt_mode(mock_bt, adopted) == HVACMode.HEAT_COOL

    def test_inbound_heat_cool_is_not_translated_by_the_swap(self):
        """A reported HEAT_COOL passes the swapped branch unchanged."""
        mock_bt = MockThermostat()
        mock_bt.add_trv(
            "climate.test",
            heat_auto_swapped=True,
            hvac_modes=[HVACMode.OFF, HVACMode.AUTO],
        )

        result = mode_remap(mock_bt, "climate.test", HVACMode.HEAT_COOL, inbound=True)
        assert result == HVACMode.HEAT_COOL
