"""SteamWebAPI source adapter.

Endpoint: GET /steam/api/items
Auth:     ?key=KEY
Response: JSON array of item objects.

Key response fields (all prices in USD as floats):
  market_hash_name  — item identifier
  pricelatest       — lowest current Steam Market listing
  pricelatestsell   — price of the last completed Steam sale
  pricereal         — lowest third-party marketplace price
  buyorderprice     — highest active Steam buy order
  offervolume       — number of active sell listings
  sold24h           — items sold in the last 24 hours
  sold7d            — items sold in the last 7 days
  sold30d           — items sold in the last 30 days
  priceupdatedat    — "YYYY-MM-DD HH:MM:SS" timestamp

The endpoint returns ALL CS2 items in one call (~15k–20k items).
We filter locally to the requested list.
"""

import asyncio
import logging
from typing import Any

import aiohttp

from config import settings
from services.rate_limiter import RateLimiter
from .base import MarketSource

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.steamwebapi.com"


class SteamWebAPISource(MarketSource):
    name = "steamwebapi"

    def __init__(self) -> None:
        self._limiter = RateLimiter("steamwebapi", settings.STEAMWEBAPI_MIN_DELAY)
        self._session: aiohttp.ClientSession | None = None

    async def _session_(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def fetch_prices(self, items: list[str]) -> list[dict[str, Any]]:
        if not settings.STEAMWEBAPI_KEY:
            logger.warning("[steamwebapi] STEAMWEBAPI_KEY not set — skipping")
            return []

        params = {"key": settings.STEAMWEBAPI_KEY, "game": "csgo"}
        data = await self._fetch_with_retry(_BASE_URL + "/steam/api/items", params=params)
        if data is None:
            return []

        if not isinstance(data, list):
            logger.error("[steamwebapi] Expected list, got %s", type(data).__name__)
            return []

        items_set = set(items)
        return [row for row in data if row.get("market_hash_name") in items_set]

    async def _fetch_with_retry(
        self, url: str, params: dict | None = None
    ) -> Any:
        session = await self._session_()
        for attempt in range(settings.MAX_RETRIES):
            await self._limiter.acquire()
            try:
                async with session.get(
                    url,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as resp:
                    if resp.status == 429:
                        backoff = settings.RETRY_BASE_DELAY ** attempt * 5
                        logger.warning(
                            "[steamwebapi] HTTP 429, retry %d/%d in %.0fs",
                            attempt + 1, settings.MAX_RETRIES, backoff,
                        )
                        await asyncio.sleep(backoff)
                        continue
                    if resp.status in (401, 403):
                        logger.error(
                            "[steamwebapi] Auth error HTTP %d — check STEAMWEBAPI_KEY",
                            resp.status,
                        )
                        return None
                    resp.raise_for_status()
                    return await resp.json(content_type=None)

            except asyncio.TimeoutError:
                logger.warning("[steamwebapi] Timeout (attempt %d)", attempt + 1)
            except aiohttp.ClientError as exc:
                logger.warning("[steamwebapi] Client error: %s (attempt %d)", exc, attempt + 1)

            if attempt < settings.MAX_RETRIES - 1:
                await asyncio.sleep(settings.RETRY_BASE_DELAY ** attempt)

        logger.error("[steamwebapi] All %d retries failed", settings.MAX_RETRIES)
        return None
