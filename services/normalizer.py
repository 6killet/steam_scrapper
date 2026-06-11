"""Converts raw source responses into unified MarketPrice objects.

Pricempire:  prices are integers in USD cents  →  divide by 100
SteamWebAPI: prices are floats in USD          →  use as-is
cs2.sh:      prices are floats in USD          →  use as-is
"""

import logging
from datetime import datetime, timezone
from typing import Any

from models.market import MarketPrice

logger = logging.getLogger(__name__)


def _flt(v: Any, divisor: float = 1.0) -> float | None:
    """Parse a numeric value to float, apply divisor, return None on failure."""
    if v is None:
        return None
    try:
        result = float(v) / divisor
        return result if result > 0 else None
    except (TypeError, ValueError):
        return None


def _int_(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _dt(v: Any) -> datetime:
    """Parse ISO-8601 / datetime-like string; fall back to UTC now."""
    if not v:
        return datetime.now(timezone.utc)
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        s = str(v).strip().replace("Z", "+00:00").replace(" ", "T")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return datetime.now(timezone.utc)


class Normalizer:
    def normalize(self, source: str, raw_data: list[dict]) -> list[MarketPrice]:
        dispatch = {
            "pricempire": self._pricempire,
            "steamwebapi": self._steamwebapi,
            "cs2sh": self._cs2sh,
        }
        fn = dispatch.get(source)
        if fn is None:
            logger.warning("No normalizer registered for source '%s'", source)
            return []

        results: list[MarketPrice] = []
        for item in raw_data:
            try:
                price = fn(item)
                if price is not None:
                    results.append(price)
            except Exception as exc:
                logger.debug("[%s] Normalizer skipped item: %s", source, exc)
        return results

    # ------------------------------------------------------------------
    # Pricempire
    # ------------------------------------------------------------------
    def _pricempire(self, item: dict) -> MarketPrice | None:
        name: str = item.get("market_hash_name", "")
        prices: dict = item.get("prices", {})
        if not name or not isinstance(prices, dict):
            return None

        steam = prices.get("steam") or {}

        # Prices are integers in cents; divide by 100 to get USD.
        steam_price = _flt(steam.get("price"), divisor=100.0)

        # Find the cheapest non-Steam marketplace price.
        non_steam_keys = ["buff163", "buff", "skinport", "csfloat", "waxpeer"]
        buy_candidates: list[tuple[float, str]] = []
        for key in non_steam_keys:
            src = prices.get(key)
            if isinstance(src, dict):
                p = _flt(src.get("price"), divisor=100.0)
                if p:
                    buy_candidates.append((p, key))

        if buy_candidates:
            buy_price, buy_market = min(buy_candidates, key=lambda x: x[0])
        else:
            buy_price, buy_market = steam_price, None
        listings_count = _int_(steam.get("count"))

        updated_str = steam.get("updatedAt") or next(
            (v.get("updatedAt") for v in prices.values() if isinstance(v, dict)),
            None,
        )

        return MarketPrice(
            source="pricempire",
            market_hash_name=name,
            buy_price=buy_price,
            buy_market=buy_market,
            sell_price=steam_price,
            steam_price=steam_price,
            listings_count=listings_count,
            updated_at=_dt(updated_str),
            raw=item,
        )

    # ------------------------------------------------------------------
    # SteamWebAPI
    # ------------------------------------------------------------------
    def _steamwebapi(self, item: dict) -> MarketPrice | None:
        name: str = item.get("market_hash_name") or item.get("name", "")
        if not name:
            return None

        # pricelatest   = lowest current Steam listing (sell target / buyer pays)
        # pricereal     = lowest third-party price     (external buy candidate)
        # buyorderprice = highest active Steam buy order (instant cashout)
        sell_price = _flt(item.get("pricelatest"))
        buy_price = _flt(item.get("pricereal"))
        steam_buy_order = _flt(item.get("buyorderprice"))

        return MarketPrice(
            source="steamwebapi",
            market_hash_name=name,
            buy_price=buy_price,
            sell_price=sell_price,
            steam_price=sell_price,
            steam_buy_order=steam_buy_order,
            volume_24h=_int_(item.get("sold24h")),
            volume_7d=_int_(item.get("sold7d")),
            listings_count=_int_(item.get("offervolume")),
            updated_at=_dt(item.get("priceupdatedat")),
            raw=item,
        )

    # ------------------------------------------------------------------
    # cs2.sh
    # ------------------------------------------------------------------
    def _cs2sh(self, item: dict) -> MarketPrice | None:
        name: str = item.get("market_hash_name", "")
        if not name:
            return None

        steam = item.get("steam") or {}
        skinport = item.get("skinport") or {}

        # Steam ask = lowest listing on Steam Market (sell target / buyer pays)
        sell_price = _flt(steam.get("ask"))

        # Steam bid = highest active Steam buy order (instant cashout)
        steam_buy_order = _flt(steam.get("bid"))

        # Steam bid_volume = quantity in active buy orders (for reliability filter)
        buy_order_qty = _int_(steam.get("bid_volume"))

        # Cheapest external marketplace ask (buy source).
        # "buff" is normalised to "buff163" for consistency with Pricempire.
        buy_candidates: list[tuple[float, str]] = []
        for market_name, key in (
            ("buff163", "buff"),
            ("skinport", "skinport"),
            ("csfloat", "csfloat"),
            ("youpin", "youpin"),
            ("c5game", "c5game"),
        ):
            src = item.get(key) or {}
            p = _flt(src.get("ask"))
            if p:
                buy_candidates.append((p, market_name))
        if buy_candidates:
            buy_price, buy_market = min(buy_candidates, key=lambda x: x[0])
        else:
            buy_price, buy_market = None, None

        # cs2.sh has no Steam sales volume; skinport's sales history is the only
        # real turnover figure available, so it serves as the liquidity proxy.
        h24 = skinport.get("24h_history") or {}
        h7 = skinport.get("7d_history") or {}
        volume_24h = _int_(h24.get("volume"))
        volume_7d = _int_(h7.get("volume"))

        updated_str = (
            steam.get("updated_at")
            or steam.get("collected_at")
            or (item.get("buff") or {}).get("updated_at")
        )

        return MarketPrice(
            source="cs2sh",
            market_hash_name=name,
            buy_price=buy_price,
            buy_market=buy_market,
            sell_price=sell_price,
            steam_price=sell_price,
            steam_buy_order=steam_buy_order,
            buy_order_qty=buy_order_qty,
            volume_24h=volume_24h,
            volume_7d=volume_7d,
            listings_count=_int_(steam.get("ask_volume")),
            updated_at=_dt(updated_str),
            raw=item,
        )
