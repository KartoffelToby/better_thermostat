"""Home Assistant serves the integration's brand images from its own folder.

A custom integration that ships a ``brand/`` directory has its icon and logo
served locally, ahead of the brands CDN. HACS requires the directory.
"""

from pathlib import Path

from homeassistant.setup import async_setup_component
import pytest

from custom_components.better_thermostat.utils.const import DOMAIN

BRAND_DIR = Path(__file__).parents[2] / "custom_components" / DOMAIN / "brand"


@pytest.mark.parametrize(
    "image", ["icon.png", "icon@2x.png", "logo.png", "logo@2x.png"]
)
async def test_the_brand_images_are_served_from_the_integration(
    hass, hass_client, image
):
    """Each image comes from the integration's folder, not from the CDN."""
    assert await async_setup_component(hass, "brands", {})
    client = await hass_client()

    response = await client.get(
        f"/api/brands/integration/{DOMAIN}/{image}", params={"placeholder": "no"}
    )

    assert response.status == 200
    assert await response.read() == (BRAND_DIR / image).read_bytes()
