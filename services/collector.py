"""Orchestrates concurrent data collection from all sources."""

import asyncio
import logging
from typing import TYPE_CHECKING

from models.market import MarketPrice

if TYPE_CHECKING:
    from sources.base import MarketSource
    from storage.repositories import RawRepository, MarketRepository
    from services.normalizer import Normalizer

logger = logging.getLogger(__name__)


class Collector:
    def __init__(
        self,
        sources: list["MarketSource"],
        raw_repo: "RawRepository",
        market_repo: "MarketRepository",
        normalizer: "Normalizer",
    ) -> None:
        self.sources = sources
        self.raw_repo = raw_repo
        self.market_repo = market_repo
        self.normalizer = normalizer

    async def collect(self, items: list[str]) -> dict[str, list[MarketPrice]]:
        """Fetch from all sources concurrently, return prices keyed by market_hash_name."""
        tasks = [self._collect_one(src, items) for src in self.sources]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        by_item: dict[str, list[MarketPrice]] = {}
        for result in results:
            if isinstance(result, BaseException):
                logger.error("Source task raised unexpectedly: %s", result)
                continue
            for price in result:
                by_item.setdefault(price.market_hash_name, []).append(price)

        return by_item

    async def _collect_one(
        self, source: "MarketSource", items: list[str]
    ) -> list[MarketPrice]:
        try:
            logger.info("[%s] Fetching prices for %d items…", source.name, len(items))
            raw_data = await source.fetch_prices(items)

            if raw_data:
                await self.raw_repo.save(source.name, raw_data)

            normalized = self.normalizer.normalize(source.name, raw_data)
            if normalized:
                await self.market_repo.save_many(normalized)

            logger.info("[%s] Got %d prices", source.name, len(normalized))
            return normalized

        except Exception as exc:
            logger.error("[%s] Unhandled error: %s", source.name, exc, exc_info=True)
            return []
