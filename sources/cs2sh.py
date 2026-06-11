"""cs2.sh source adapter.

Endpoint: POST /v1/prices/latest
Auth:     Authorization: Bearer <API_KEY>
Body:     {"items": ["market_hash_name", ...]}   # non-empty list is required

Plan tiers: the basic plan only allows /v1/prices/latest; the full catalog
(GET /v1/items, used for universe mode) requires the Scale plan.

Response schema:
{
  "response_time": "ISO8601",
  "currency": "USD",
  "items": {
    "AK-47 | Redline (Field-Tested)": {
      "market_hash_name": "AK-47 | Redline (Field-Tested)",
      "buff": {
        "updated_at":   "ISO8601",
        "collected_at": "ISO8601",
        "ask":          10.50,   // lowest listing price (USD float)
        "ask_volume":   100,     // items listed for sale
        "bid":          10.00,   // highest buy order (USD float, when available)
        "bid_volume":   50       // active buy orders
      },
      "steam": {
        "updated_at":   "ISO8601",
        "collected_at": "ISO8601",
        "ask":          12.00,
        "ask_volume":   200
      },
      "skinport": { ... },
      "csfloat":  { ... },
      "youpin":   { ... }
    }
  }
}

Data refreshes approximately every 5 minutes.
"""

import asyncio
import logging
from typing import Any

import aiohttp

from config import settings
from services.rate_limiter import RateLimiter
from .base import MarketSource

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.cs2.sh"


class CS2SHSource(MarketSource):
    name = "cs2sh"

    def __init__(self) -> None:
        self._limiter = RateLimiter("cs2sh", settings.CS2SH_MIN_DELAY)
        self._session: aiohttp.ClientSession | None = None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {settings.CS2SH_API_KEY}",
            "Accept-Encoding": "gzip",
            "Content-Type": "application/json",
        }

    async def _session_(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def fetch_prices(self, items: list[str]) -> list[dict[str, Any]]:
        if not settings.CS2SH_API_KEY:
            logger.warning("[cs2sh] CS2SH_API_KEY not set — skipping")
            return []

        if not items:
            return []
        data = await self._fetch_with_retry(
            _BASE_URL + "/v1/prices/latest",
            payload={"items": items},
        )
        if data is None:
            return []
        items_data = data.get("items", {})
        if not isinstance(items_data, dict):
            logger.error("[cs2sh] Unexpected 'items' type: %s", type(items_data).__name__)
            return []

        result: list[dict[str, Any]] = []
        for name, marketplace_prices in items_data.items():
            if isinstance(marketplace_prices, dict):
                entry: dict[str, Any] = {"market_hash_name": name}
                entry.update(marketplace_prices)
                result.append(entry)
        return result

    async def fetch_universe_items(
        self,
        min_price_usd: float,
        max_price_usd: float,
        max_items: int,
    ) -> list[str]:
        """Return market_hash_names for universe mode via GET /v1/items.

        Requires the cs2.sh Scale plan; on 403 logs a clear message and
        returns [] so main falls back to file mode. Price filtering happens
        later in the pipeline (the catalog endpoint returns names only).
        """
        if not settings.CS2SH_API_KEY:
            return []
        session = await self._session_()
        await self._limiter.acquire()
        try:
            async with session.get(
                _BASE_URL + "/v1/items",
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status == 403:
                    logger.warning(
                        "[cs2sh] Catalog endpoint needs the Scale plan — "
                        "universe mode unavailable on the current tier"
                    )
                    return []
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
            logger.warning("[cs2sh] Catalog request failed: %s", exc)
            return []

        # Schema is unknown until a Scale key is available — accept both a
        # bare list of names and an {"items": [...]} / {"items": {...}} wrapper.
        if isinstance(data, dict):
            data = data.get("items", [])
        if isinstance(data, dict):
            names = [n for n in data if isinstance(n, str)]
        elif isinstance(data, list):
            names = [
                n if isinstance(n, str) else n.get("market_hash_name", "")
                for n in data
                if isinstance(n, (str, dict))
            ]
        else:
            names = []
        return [n for n in names if n][:max_items]

    async def _fetch_with_retry(
        self, url: str, payload: dict | None = None
    ) -> Any:
        session = await self._session_()
        for attempt in range(settings.MAX_RETRIES):
            await self._limiter.acquire()
            try:
                async with session.post(
                    url,
                    headers=self._headers(),
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status == 429:
                        backoff = settings.RETRY_BASE_DELAY ** attempt * 5
                        logger.warning(
                            "[cs2sh] HTTP 429, retry %d/%d in %.0fs",
                            attempt + 1, settings.MAX_RETRIES, backoff,
                        )
                        await asyncio.sleep(backoff)
                        continue
                    if resp.status == 401:
                        logger.error("[cs2sh] Invalid API key (HTTP 401)")
                        return None
                    if resp.status == 403:
                        logger.error("[cs2sh] Access denied (HTTP 403) — check API key tier")
                        return None
                    resp.raise_for_status()
                    return await resp.json(content_type=None)

            except asyncio.TimeoutError:
                logger.warning("[cs2sh] Timeout (attempt %d)", attempt + 1)
            except aiohttp.ClientError as exc:
                logger.warning("[cs2sh] Client error: %s (attempt %d)", exc, attempt + 1)

            if attempt < settings.MAX_RETRIES - 1:
                await asyncio.sleep(settings.RETRY_BASE_DELAY ** attempt)

        logger.error("[cs2sh] All %d retries failed", settings.MAX_RETRIES)
        return None
