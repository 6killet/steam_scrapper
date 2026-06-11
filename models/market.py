from datetime import datetime
from typing import Any
from pydantic import BaseModel, ConfigDict


class MarketPrice(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    source: str
    market_hash_name: str
    currency: str = "USD"
    buy_price: float | None = None          # cheapest external marketplace ask
    buy_market: str | None = None           # marketplace of that ask (buff163, skinport, …)
    sell_price: float | None = None         # lowest Steam listing (buyer pays)
    steam_price: float | None = None        # Steam market price (same as sell_price usually)
    steam_buy_order: float | None = None    # highest active Steam buy order
    buy_order_qty: int | None = None        # number of active Steam buy orders
    volume_24h: int | None = None
    volume_7d: int | None = None
    listings_count: int | None = None
    updated_at: datetime
    raw: dict[str, Any] = {}


class ScoredOpportunity(BaseModel):
    market_hash_name: str

    # Where to buy
    buy_price: float | None = None          # external ask price
    buy_market: str | None = None           # source name with best buy price

    # Steam prices
    steam_buy_order: float | None = None    # highest Steam buy order (instant cashout)
    steam_sell_listing: float | None = None # lowest Steam sell listing (list and wait)

    # Legacy fields (kept for backwards compatibility)
    sell_price: float | None = None         # = steam_sell_listing
    net_sell: float | None = None           # net from sell listing after fee

    # Economics (realistic = via buy order, optimistic = via sell listing)
    roi_realistic: float | None = None
    roi_optimistic: float | None = None
    roi: float | None = None                # = roi_realistic (primary sort key)
    net_profit_usd: float | None = None     # absolute profit via buy order path
    profit: float | None = None             # = net_profit_usd

    # Quality signals
    volume_24h: int | None = None
    score: int = 0                          # 0–100
    sources: list[str] = []
    reason: str = ""
    incomplete: bool = False                # True when no steam_buy_order available
    qty_unknown: bool = False               # True when no buy order quantity data
    # Stage 4 — historical analytics
    is_stable: bool | None = None
    stability_cv: float | None = None
    suggested_sell_price: float | None = None
    history_insufficient: bool = False      # True when < STABILITY_MIN_DAYS of data
    weighted_ratio: float | None = None     # buy*0.4 + sell*0.2 + trans*0.4 (lower = better)
    updated_at: datetime
