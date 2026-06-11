from datetime import datetime
from typing import Any
from pydantic import BaseModel, ConfigDict


class MarketPrice(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    source: str
    market_hash_name: str
    currency: str = "USD"
    buy_price: float | None = None       # cheapest non-steam ask / best buy order
    sell_price: float | None = None      # lowest steam listing price (target sell)
    steam_price: float | None = None     # steam market price specifically
    volume_24h: int | None = None        # items sold in last 24h
    volume_7d: int | None = None         # items sold in last 7d
    listings_count: int | None = None    # active sell listings
    updated_at: datetime
    raw: dict[str, Any] = {}


class ScoredOpportunity(BaseModel):
    market_hash_name: str
    buy_price: float | None = None
    sell_price: float | None = None
    net_sell: float | None = None        # sell_price * (1 - fee)
    profit: float | None = None          # net_sell - buy_price
    roi: float | None = None             # profit / buy_price
    volume_24h: int | None = None
    score: int = 0                       # 0-100
    sources: list[str] = []
    reason: str = ""
    updated_at: datetime
