"""Tests for BetterThermostat._post_grace_recheck.

The helper backs the post-grace background tasks scheduled in
_finalize_startup: it sleeps until the startup grace window has elapsed and
then re-runs an availability check (check_critical_entities for TRVs,
check_and_update_degraded_mode for optional sensors) unless the entity has
been removed in the meantime.
"""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import custom_components.better_thermostat.climate as climate_module
from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.core.clock import FakeClock

GRACE_SECONDS = 90


@pytest.fixture
def bt():
    """Create a mock BetterThermostat with the attributes the helper reads."""
    mock = MagicMock(spec=BetterThermostat)
    mock.device_name = "Test BT"
    mock.is_removed = False
    mock.clock = FakeClock()
    return mock


class TestPostGraceRecheck:
    """Tests for the sleep-then-recheck behavior of _post_grace_recheck."""

    @pytest.mark.asyncio
    async def test_sleeps_remaining_grace_then_rechecks(self, bt):
        """With grace time remaining, sleep the remainder and run the check."""
        grace_until = bt.clock.now() + timedelta(seconds=GRACE_SECONDS)
        with (
            patch.object(
                climate_module, "check_critical_entities", new_callable=AsyncMock
            ) as check,
            patch.object(
                climate_module.asyncio, "sleep", new_callable=AsyncMock
            ) as sleep,
        ):
            await BetterThermostat._post_grace_recheck(
                bt, grace_until, climate_module.check_critical_entities
            )
        sleep.assert_awaited_once()
        assert 0 < sleep.await_args.args[0] <= GRACE_SECONDS
        check.assert_awaited_once_with(bt)

    @pytest.mark.asyncio
    async def test_removed_during_sleep_skips_recheck(self, bt):
        """When the entity is removed while sleeping, skip the recheck."""
        grace_until = bt.clock.now() + timedelta(seconds=GRACE_SECONDS)

        async def _remove_during_sleep(_delay):
            bt.is_removed = True

        with (
            patch.object(
                climate_module, "check_critical_entities", new_callable=AsyncMock
            ) as check,
            patch.object(
                climate_module.asyncio,
                "sleep",
                new_callable=AsyncMock,
                side_effect=_remove_during_sleep,
            ) as sleep,
        ):
            await BetterThermostat._post_grace_recheck(
                bt, grace_until, climate_module.check_critical_entities
            )
        sleep.assert_awaited_once()
        check.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_expired_grace_rechecks_without_sleep(self, bt):
        """With the grace window already over, recheck immediately."""
        grace_until = bt.clock.now() - timedelta(seconds=1)
        with (
            patch.object(
                climate_module, "check_critical_entities", new_callable=AsyncMock
            ) as check,
            patch.object(
                climate_module.asyncio, "sleep", new_callable=AsyncMock
            ) as sleep,
        ):
            await BetterThermostat._post_grace_recheck(
                bt, grace_until, climate_module.check_critical_entities
            )
        sleep.assert_not_awaited()
        check.assert_awaited_once_with(bt)

    @pytest.mark.asyncio
    async def test_no_grace_window_rechecks_without_sleep(self, bt):
        """With no grace window set, recheck immediately."""
        with (
            patch.object(
                climate_module, "check_critical_entities", new_callable=AsyncMock
            ) as check,
            patch.object(
                climate_module.asyncio, "sleep", new_callable=AsyncMock
            ) as sleep,
        ):
            await BetterThermostat._post_grace_recheck(
                bt, None, climate_module.check_critical_entities
            )
        sleep.assert_not_awaited()
        check.assert_awaited_once_with(bt)
