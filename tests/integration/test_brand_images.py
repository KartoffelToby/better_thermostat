"""Home Assistant serves the integration's brand images from its own folder.

A custom integration that ships a ``brand/`` directory has its icon and logo
served locally, ahead of the brands CDN. HACS requires the directory.
"""

from pathlib import Path

from aiohttp import web
from homeassistant.components.brands import BrandsIntegrationView
from homeassistant.setup import async_setup_component
import pytest

from custom_components.better_thermostat.utils.const import DOMAIN

BRAND_DIR = Path(__file__).parents[2] / "custom_components" / DOMAIN / "brand"

NOT_LOCAL = b"served by the disk cache or the brands CDN"


@pytest.mark.quality_rule("brands")
@pytest.mark.parametrize(
    "image", ["icon.png", "icon@2x.png", "logo.png", "logo@2x.png"]
)
async def test_the_brand_images_are_served_from_the_integration(
    hass, hass_client, monkeypatch, image
):
    """Each image comes from the integration's folder, not from the CDN."""

    async def serve_not_local(*_args, **_kwargs):
        return web.Response(body=NOT_LOCAL, content_type="image/png")

    monkeypatch.setattr(
        BrandsIntegrationView, "_serve_from_cache_or_cdn", serve_not_local
    )
    assert await async_setup_component(hass, "brands", {})
    client = await hass_client()

    response = await client.get(
        f"/api/brands/integration/{DOMAIN}/{image}", params={"placeholder": "no"}
    )

    assert response.status == 200
    body = await response.read()
    assert body != NOT_LOCAL
    assert body == (BRAND_DIR / image).read_bytes()
