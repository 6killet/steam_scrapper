"""Thin persistence wrappers over SQLAlchemy and JSON file storage."""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from models.market import MarketPrice
from .db import MarketPriceDB, RawResponseDB

logger = logging.getLogger(__name__)

_RAW_DIR = Path("data/raw")


class RawRepository:
    """Persists raw API responses to:
    1. SQLite (queryable)
    2. JSON files under data/raw/ (human-readable, easy to diff/replay)
    """

    def __init__(self, session_factory) -> None:
        self._factory = session_factory

    async def save(self, source: str, data: list) -> None:
        now = datetime.now(timezone.utc)

        # Write to SQLite
        async with self._factory() as session:
            session.add(
                RawResponseDB(source=source, fetched_at=now, data=data)
            )
            await session.commit()

        # Write to JSON file
        _RAW_DIR.mkdir(parents=True, exist_ok=True)
        ts = now.strftime("%Y%m%d_%H%M%S")
        out = _RAW_DIR / f"{source}_{ts}.json"
        try:
            out.write_text(
                json.dumps({"source": source, "fetched_at": now.isoformat(), "data": data},
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.debug("[%s] Raw response saved to %s", source, out)
        except OSError as exc:
            logger.warning("[%s] Could not write raw file: %s", source, exc)


class MarketRepository:
    """Persists normalised MarketPrice records to SQLite."""

    def __init__(self, session_factory) -> None:
        self._factory = session_factory

    async def save_many(self, prices: list[MarketPrice]) -> None:
        if not prices:
            return
        now = datetime.now(timezone.utc)
        async with self._factory() as session:
            for p in prices:
                session.add(
                    MarketPriceDB(
                        source=p.source,
                        market_hash_name=p.market_hash_name,
                        currency=p.currency,
                        buy_price=p.buy_price,
                        sell_price=p.sell_price,
                        steam_price=p.steam_price,
                        volume_24h=p.volume_24h,
                        volume_7d=p.volume_7d,
                        listings_count=p.listings_count,
                        updated_at=p.updated_at,
                        saved_at=now,
                        raw=p.raw,
                    )
                )
            await session.commit()
        logger.debug("Saved %d market prices", len(prices))
