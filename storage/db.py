"""SQLAlchemy async models and engine factory for SQLite."""

import os
from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, DateTime, Float, Index, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class RawResponseDB(Base):
    __tablename__ = "raw_responses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    data: Mapped[dict] = mapped_column(JSON, nullable=False)


class MarketPriceDB(Base):
    __tablename__ = "market_prices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    market_hash_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    buy_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    buy_market: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sell_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    steam_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    steam_buy_order: Mapped[float | None] = mapped_column(Float, nullable=True)
    buy_order_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    volume_24h: Mapped[int | None] = mapped_column(Integer, nullable=True)
    volume_7d: Mapped[int | None] = mapped_column(Integer, nullable=True)
    listings_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    saved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class PriceSnapshotDB(Base):
    __tablename__ = "price_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    market_hash_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    market: Mapped[str] = mapped_column(String(64), nullable=False)
    price_type: Mapped[str] = mapped_column(String(32), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(precision=12, scale=4), nullable=False)
    currency_original: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    volume: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        Index("ix_snap_name_ts", "market_hash_name", "ts"),
        Index("ix_snap_name_market_type_ts", "market_hash_name", "market", "price_type", "ts"),
    )


class SellHistoryPointDB(Base):
    __tablename__ = "sell_history_points"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_hash_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    market: Mapped[str] = mapped_column(String(64), nullable=False)
    ts_point: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(precision=12, scale=4), nullable=False)
    amount: Mapped[int | None] = mapped_column(Integer, nullable=True)


class CollectRunDB(Base):
    __tablename__ = "collect_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    items_requested: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prices_saved: Mapped[int | None] = mapped_column(Integer, nullable=True)
    errors_json: Mapped[list | None] = mapped_column(JSON, nullable=True)


class BotSettingsDB(Base):
    """Per-chat filter thresholds configured via /filters."""
    __tablename__ = "bot_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    min_roi_pct: Mapped[float] = mapped_column(Float, nullable=False, default=5.0)
    min_volume_24h: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    min_price_usd: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    max_price_usd: Mapped[float] = mapped_column(Float, nullable=False, default=500.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class NotificationsSentDB(Base):
    """Anti-spam log: last notification per (chat, item)."""
    __tablename__ = "notifications_sent"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    market_hash_name: Mapped[str] = mapped_column(String(255), nullable=False)
    last_sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_roi_pct: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (UniqueConstraint("chat_id", "market_hash_name"),)


class BotMuteDB(Base):
    """Per-chat mute list — items the user doesn't want notified about."""
    __tablename__ = "bot_mutes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False)
    market_hash_name: Mapped[str] = mapped_column(String(255), nullable=False)

    __table_args__ = (UniqueConstraint("chat_id", "market_hash_name"),)


def create_engine_and_session(db_path: str):
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        echo=False,
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine, session_factory


# Columns added after the initial release; init_db backfills them into an
# existing SQLite DB so users don't have to recreate data/*.db.
_MIGRATION_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "market_prices": [
        ("buy_market", "VARCHAR(64)"),
        ("steam_buy_order", "FLOAT"),
        ("buy_order_qty", "INTEGER"),
    ],
}


def _ensure_columns(conn) -> None:
    from sqlalchemy import text

    for table, columns in _MIGRATION_COLUMNS.items():
        existing = {
            row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))
        }
        for name, ddl_type in columns:
            if existing and name not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}"))


async def init_db(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_ensure_columns)
