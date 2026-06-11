"""Pricempire v3 source adapter.

Endpoint: GET /v3/items/prices
Auth:     ?api_token=TOKEN
Response: flat dict  { market_hash_name: { source_key: { price: int_cents, count: int, updatedAt: str } } }

Prices are returned as integers in USD cents.
All items are returned in one bulk response; we filter locally to the requested list.

Note: Pricempire also offers a v4 paid endpoint (/v4/paid/items/prices).
      The response schema is identical — swap the URL and you are on v4.
      This adapter uses v3 so it works on the free/basic tier.
"""

import asyncio
import logging
from typing import Any

import aiohttp

from config import settings
from services.rate_limiter import RateLimiter
from .base import MarketSource

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.pricempire.com"
_SOURCES = "steam,buff163,skinport,csfloat,waxpeer"


class PricempireSource(MarketSource):
    name = "pricempire"

    def __init__(self) -> None:
        self._limiter = RateLimiter("pricempire", settings.PRICEMPIRE_MIN_DELAY)
        self._session: aiohttp.ClientSession | None = None

    async def _session_(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def fetch_prices(self, items: list[str]) -> list[dict[str, Any]]:
        if not settings.PRICEMPIRE_API_TOKEN:
            logger.warning("[pricempire] PRICEMPIRE_API_TOKEN not set — skipping")
            return []

        params = {
            "api_token": settings.PRICEMPIRE_API_TOKEN,
            "sources[]": _SOURCES.split(","),
            "currency": "USD",
        }

        raw = await self._fetch_with_retry(_BASE_URL + "/v3/items/prices", params=params)
        if raw is None:
            return []

        # Unwrap possible {"data": {...}} wrapper
        if isinstance(raw, dict) and "data" in raw and isinstance(raw["data"], dict):
            raw = raw["data"]

        items_set = set(items)
        result: list[dict[str, Any]] = []
        for name, prices in raw.items():
            if name in items_set and isinstance(prices, dict):
                result.append({"market_hash_name": name, "prices": prices})
        return result

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
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status == 429:
                        backoff = settings.RETRY_BASE_DELAY ** attempt * 5
                        logger.warning(
                            "[pricempire] HTTP 429, retry %d/%d in %.0fs",
                            attempt + 1, settings.MAX_RETRIES, backoff,
                        )
                        await asyncio.sleep(backoff)
                        continue
                    if resp.status in (401, 403):
                        logger.error(
                            "[pricempire] Auth error HTTP %d — check PRICEMPIRE_API_TOKEN",
                            resp.status,
                        )
                        return None
                    resp.raise_for_status()
                    return await resp.json(content_type=None)

            except asyncio.TimeoutError:
                logger.warning("[pricempire] Timeout (attempt %d)", attempt + 1)
            except aiohttp.ClientError as exc:
                logger.warning("[pricempire] Client error: %s (attempt %d)", exc, attempt + 1)

            if attempt < settings.MAX_RETRIES - 1:
                await asyncio.sleep(settings.RETRY_BASE_DELAY ** attempt)

        logger.error("[pricempire] All %d retries failed", settings.MAX_RETRIES)
        return None
