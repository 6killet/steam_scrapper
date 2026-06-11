"""Thin persistence wrappers over SQLAlchemy and JSON file storage."""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.market import MarketPrice
from .db import BotMuteDB, BotSettingsDB, CollectRunDB, MarketPriceDB, NotificationsSentDB, PriceSnapshotDB, RawResponseDB

logger = logging.getLogger(__name__)

_RAW_DIR = Path("data/raw")


class RawRepository:
    """Persists raw API responses to SQLite and JSON files under data/raw/."""

    def __init__(self, session_factory) -> None:
        self._factory = session_factory

    async def save(self, source: str, data: list) -> None:
        now = datetime.now(timezone.utc)

        async with self._factory() as session:
            session.add(RawResponseDB(source=source, fetched_at=now, data=data))
            await session.commit()

        _RAW_DIR.mkdir(parents=True, exist_ok=True)
        ts = now.strftime("%Y%m%d_%H%M%S")
        out = _RAW_DIR / f"{source}_{ts}.json"
        try:
            out.write_text(
                json.dumps(
                    {"source": source, "fetched_at": now.isoformat(), "data": data},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            logger.debug("[%s] Raw response saved to %s", source, out)
        except OSError as exc:
            logger.warning("[%s] Could not write raw file: %s", source, exc)

    async def cleanup_old(self, retention_days: int) -> int:
        """Delete raw data (JSON files + raw_responses rows) older than
        retention_days. Returns count of files deleted."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        deleted = 0
        if _RAW_DIR.exists():
            for f in _RAW_DIR.glob("*.json"):
                mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff:
                    f.unlink(missing_ok=True)
                    deleted += 1

        async with self._factory() as session:
            result = await session.execute(
                delete(RawResponseDB).where(RawResponseDB.fetched_at < cutoff)
            )
            await session.commit()
            if result.rowcount:
                logger.info("Cleaned up %d old raw_responses rows", result.rowcount)
        return deleted


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
                        buy_market=p.buy_market,
                        sell_price=p.sell_price,
                        steam_price=p.steam_price,
                        steam_buy_order=p.steam_buy_order,
                        buy_order_qty=p.buy_order_qty,
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

    async def get_latest_by_item(self) -> dict[str, list[MarketPrice]]:
        """Return the most recent MarketPrice per (source, market_hash_name) from DB."""
        subq = (
            select(
                MarketPriceDB.source,
                MarketPriceDB.market_hash_name,
                func.max(MarketPriceDB.saved_at).label("max_saved_at"),
            )
            .group_by(MarketPriceDB.source, MarketPriceDB.market_hash_name)
            .subquery()
        )
        stmt = select(MarketPriceDB).join(
            subq,
            (MarketPriceDB.source == subq.c.source)
            & (MarketPriceDB.market_hash_name == subq.c.market_hash_name)
            & (MarketPriceDB.saved_at == subq.c.max_saved_at),
        )
        async with self._factory() as session:
            rows = (await session.execute(stmt)).scalars().all()

        by_item: dict[str, list[MarketPrice]] = {}
        for row in rows:
            mp = MarketPrice(
                source=row.source,
                market_hash_name=row.market_hash_name,
                currency=row.currency,
                buy_price=row.buy_price,
                buy_market=row.buy_market,
                sell_price=row.sell_price,
                steam_price=row.steam_price,
                steam_buy_order=row.steam_buy_order,
                buy_order_qty=row.buy_order_qty,
                volume_24h=row.volume_24h,
                volume_7d=row.volume_7d,
                listings_count=row.listings_count,
                updated_at=row.updated_at,
                raw=row.raw or {},
            )
            by_item.setdefault(mp.market_hash_name, []).append(mp)
        return by_item


class PriceSnapshotRepository:
    """Saves granular price snapshots derived from MarketPrice objects."""

    def __init__(self, session_factory) -> None:
        self._factory = session_factory

    async def save_from_market_prices(
        self, prices: list[MarketPrice], ts: datetime
    ) -> int:
        """Decompose MarketPrice list into snapshots and persist. Returns count saved."""
        rows: list[PriceSnapshotDB] = []
        for p in prices:
            if p.sell_price is not None:
                rows.append(PriceSnapshotDB(
                    ts=ts,
                    market_hash_name=p.market_hash_name,
                    source=p.source,
                    market="steam",
                    price_type="sell_listing",
                    price=Decimal(str(p.sell_price)),
                    currency_original=p.currency,
                    volume=p.volume_24h,
                ))
            if p.buy_price is not None:
                rows.append(PriceSnapshotDB(
                    ts=ts,
                    market_hash_name=p.market_hash_name,
                    source=p.source,
                    market=p.buy_market or "external",
                    price_type="sell_listing",
                    price=Decimal(str(p.buy_price)),
                    currency_original=p.currency,
                    volume=None,
                ))
            if p.steam_price is not None and p.steam_price != p.sell_price:
                rows.append(PriceSnapshotDB(
                    ts=ts,
                    market_hash_name=p.market_hash_name,
                    source=p.source,
                    market="steam",
                    price_type="avg_24h",
                    price=Decimal(str(p.steam_price)),
                    currency_original=p.currency,
                    volume=None,
                ))
            if p.steam_buy_order is not None:
                rows.append(PriceSnapshotDB(
                    ts=ts,
                    market_hash_name=p.market_hash_name,
                    source=p.source,
                    market="steam",
                    price_type="buy_order",
                    price=Decimal(str(p.steam_buy_order)),
                    currency_original=p.currency,
                    volume=p.buy_order_qty,
                ))
        if rows:
            async with self._factory() as session:
                session.add_all(rows)
                await session.commit()
        return len(rows)

    async def get_history_bulk(
        self,
        names: list[str],
        days: int,
    ) -> dict[str, list]:
        """Return price_snapshots for given items within the last `days` days.
        Keys are market_hash_name strings; values are lists of PriceSnapshotDB rows.
        """
        if not names:
            return {}
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = (
            select(PriceSnapshotDB)
            .where(
                PriceSnapshotDB.market_hash_name.in_(names),
                PriceSnapshotDB.ts >= cutoff,
            )
            .order_by(PriceSnapshotDB.ts)
        )
        async with self._factory() as session:
            rows = (await session.execute(stmt)).scalars().all()
        by_item: dict[str, list] = {}
        for row in rows:
            by_item.setdefault(row.market_hash_name, []).append(row)
        return by_item

    async def get_all_for_backtest(self, days: int) -> list:
        """Return all price_snapshots within the last `days` days (for backtest)."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = (
            select(PriceSnapshotDB)
            .where(PriceSnapshotDB.ts >= cutoff)
            .order_by(PriceSnapshotDB.ts)
        )
        async with self._factory() as session:
            return (await session.execute(stmt)).scalars().all()


class CollectRunRepository:
    """Журнал проходов сбора данных."""

    def __init__(self, session_factory) -> None:
        self._factory = session_factory

    async def start(self) -> int:
        """Create a new run record and return its id."""
        async with self._factory() as session:
            run = CollectRunDB(started_at=datetime.now(timezone.utc))
            session.add(run)
            await session.commit()
            await session.refresh(run)
            return run.id

    async def finish(
        self,
        run_id: int,
        items_requested: int,
        prices_saved: int,
        errors: list[str],
    ) -> None:
        async with self._factory() as session:
            run = await session.get(CollectRunDB, run_id)
            if run:
                run.finished_at = datetime.now(timezone.utc)
                run.items_requested = items_requested
                run.prices_saved = prices_saved
                run.errors_json = errors or None
                await session.commit()

    async def get_last(self) -> CollectRunDB | None:
        stmt = select(CollectRunDB).order_by(CollectRunDB.id.desc()).limit(1)
        async with self._factory() as session:
            return (await session.execute(stmt)).scalars().first()


class BotSettingsRepository:
    """Per-chat filter configuration for the Telegram bot."""

    def __init__(self, session_factory) -> None:
        self._factory = session_factory

    async def get_or_create(self, chat_id: int) -> BotSettingsDB:
        async with self._factory() as session:
            row = (await session.execute(
                select(BotSettingsDB).where(BotSettingsDB.chat_id == chat_id)
            )).scalars().first()
            if row is None:
                row = BotSettingsDB(
                    chat_id=chat_id,
                    min_roi_pct=5.0,
                    min_volume_24h=10,
                    min_price_usd=1.0,
                    max_price_usd=500.0,
                    updated_at=datetime.now(timezone.utc),
                )
                session.add(row)
                await session.commit()
                await session.refresh(row)
            return row

    async def update(self, chat_id: int, **kwargs) -> BotSettingsDB:
        async with self._factory() as session:
            row = (await session.execute(
                select(BotSettingsDB).where(BotSettingsDB.chat_id == chat_id)
            )).scalars().first()
            if row is None:
                row = BotSettingsDB(
                    chat_id=chat_id,
                    min_roi_pct=5.0,
                    min_volume_24h=10,
                    min_price_usd=1.0,
                    max_price_usd=500.0,
                    updated_at=datetime.now(timezone.utc),
                )
                session.add(row)
            for k, v in kwargs.items():
                setattr(row, k, v)
            row.updated_at = datetime.now(timezone.utc)
            await session.commit()
            await session.refresh(row)
            return row

    async def get_all(self) -> list[BotSettingsDB]:
        async with self._factory() as session:
            return list((await session.execute(select(BotSettingsDB))).scalars().all())


class NotificationsSentRepository:
    """Anti-spam log for Telegram notifications."""

    def __init__(self, session_factory) -> None:
        self._factory = session_factory

    async def get(self, chat_id: int, market_hash_name: str) -> NotificationsSentDB | None:
        async with self._factory() as session:
            return (await session.execute(
                select(NotificationsSentDB)
                .where(NotificationsSentDB.chat_id == chat_id)
                .where(NotificationsSentDB.market_hash_name == market_hash_name)
            )).scalars().first()

    async def upsert(self, chat_id: int, market_hash_name: str, roi_pct: float | None) -> None:
        now = datetime.now(timezone.utc)
        async with self._factory() as session:
            row = (await session.execute(
                select(NotificationsSentDB)
                .where(NotificationsSentDB.chat_id == chat_id)
                .where(NotificationsSentDB.market_hash_name == market_hash_name)
            )).scalars().first()
            if row is None:
                session.add(NotificationsSentDB(
                    chat_id=chat_id,
                    market_hash_name=market_hash_name,
                    last_sent_at=now,
                    last_roi_pct=roi_pct,
                ))
            else:
                row.last_sent_at = now
                row.last_roi_pct = roi_pct
            await session.commit()


class BotMuteRepository:
    """Per-chat mute list — items the user has silenced."""

    def __init__(self, session_factory) -> None:
        self._factory = session_factory

    async def mute(self, chat_id: int, market_hash_name: str) -> None:
        async with self._factory() as session:
            exists = (await session.execute(
                select(BotMuteDB)
                .where(BotMuteDB.chat_id == chat_id)
                .where(BotMuteDB.market_hash_name == market_hash_name)
            )).scalars().first()
            if exists is None:
                session.add(BotMuteDB(chat_id=chat_id, market_hash_name=market_hash_name))
                await session.commit()

    async def get_muted(self, chat_id: int) -> set[str]:
        async with self._factory() as session:
            names = list((await session.execute(
                select(BotMuteDB.market_hash_name)
                .where(BotMuteDB.chat_id == chat_id)
            )).scalars().all())
        return set(names)
