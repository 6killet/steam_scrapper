"""Liquidity and data-quality filters applied before scoring.

Hard filters (item excluded from top when triggered):
  1. no_volume_data       — no source has volume_24h
  2. low_volume           — max(volume_24h) < MIN_VOLUME_24H
  3. price_out_of_range   — best price outside [MIN_PRICE_USD, MAX_PRICE_USD]
  4. insufficient_sources — fewer than MIN_SOURCES have data for this item
  5. divergent_data       — sell price spread across sources > MAX_SOURCE_DIVERGENCE_PCT
  6. low_buy_order_qty    — buy_order_qty available AND qty < MIN_BUY_ORDER_QTY

Soft flags (item passes but flagged):
  qty_unknown — no source provides buy_order_qty; handled in Scorer
"""

import logging
from dataclasses import dataclass

from models.market import MarketPrice

logger = logging.getLogger(__name__)


@dataclass
class FilterSummary:
    total: int = 0
    passed: int = 0
    no_volume_data: int = 0
    low_volume: int = 0
    price_out_of_range: int = 0
    insufficient_sources: int = 0
    divergent_data: int = 0
    low_buy_order_qty: int = 0

    @property
    def filtered(self) -> int:
        return self.total - self.passed

    def funnel_str(self, top_n: int | None = None) -> str:
        top_part = f" → top {top_n}" if top_n is not None else ""
        line = f"Items: {self.total} total → {self.passed} passed filters{top_part}"
        reasons = []
        for attr in ("no_volume_data", "low_volume", "price_out_of_range",
                     "insufficient_sources", "divergent_data", "low_buy_order_qty"):
            val = getattr(self, attr)
            if val:
                reasons.append(f"{attr}={val}")
        if reasons:
            line += f"\n  Filtered: {', '.join(reasons)}"
        return line


class LiquidityFilter:
    def __init__(
        self,
        min_volume_24h: int = 10,
        min_price_usd: float = 1.0,
        max_price_usd: float = 500.0,
        min_sources: int = 2,
        max_source_divergence_pct: float = 15.0,
        min_buy_order_qty: int = 3,
    ) -> None:
        self.min_volume_24h = min_volume_24h
        self.min_price_usd = min_price_usd
        self.max_price_usd = max_price_usd
        self.min_sources = min_sources
        self.max_divergence = max_source_divergence_pct
        self.min_buy_order_qty = min_buy_order_qty

    def apply(
        self,
        prices_by_item: dict[str, list[MarketPrice]],
    ) -> tuple[dict[str, list[MarketPrice]], FilterSummary]:
        """Return (passed_items, summary). Rejected items are DEBUG-logged."""
        summary = FilterSummary(total=len(prices_by_item))
        passed: dict[str, list[MarketPrice]] = {}

        for name, prices in prices_by_item.items():
            reason = self._reject_reason(prices)
            if reason:
                logger.debug("Filtered %r: %s", name, reason)
                _bump(summary, reason)
            else:
                passed[name] = prices

        summary.passed = len(passed)
        return passed, summary

    # ── per-item decision ──────────────────────────────────────────────────────

    def _reject_reason(self, prices: list[MarketPrice]) -> str:
        # 1. Volume data
        vols = [p.volume_24h for p in prices if p.volume_24h is not None]
        if not vols:
            return "no_volume_data"
        if max(vols) < self.min_volume_24h:
            return "low_volume"

        # 2. Price range
        ref = _best_price(prices)
        if ref is not None and not (self.min_price_usd <= ref <= self.max_price_usd):
            return "price_out_of_range"

        # 3. Minimum number of independent sources
        if len({p.source for p in prices}) < self.min_sources:
            return "insufficient_sources"

        # 4. Source price divergence
        if self._is_divergent(prices):
            return "divergent_data"

        # 5. Buy order quantity (hard check only when data is present)
        qty = _best_buy_order_qty(prices)
        if qty is not None and qty < self.min_buy_order_qty:
            return "low_buy_order_qty"

        return ""

    def _is_divergent(self, prices: list[MarketPrice]) -> bool:
        sell_prices = [p.sell_price for p in prices if p.sell_price]
        if len(sell_prices) < 2:
            return False
        lo, hi = min(sell_prices), max(sell_prices)
        if lo <= 0:
            return False
        return (hi - lo) / lo * 100 > self.max_divergence


# ── helpers ────────────────────────────────────────────────────────────────────

def _best_price(prices: list[MarketPrice]) -> float | None:
    candidates = [p.buy_price for p in prices if p.buy_price]
    if not candidates:
        candidates = [p.sell_price for p in prices if p.sell_price]
    return min(candidates) if candidates else None


def _best_buy_order_qty(prices: list[MarketPrice]) -> int | None:
    qtys = [p.buy_order_qty for p in prices if p.buy_order_qty is not None]
    return max(qtys) if qtys else None


def _bump(summary: FilterSummary, reason: str) -> None:
    if hasattr(summary, reason):
        setattr(summary, reason, getattr(summary, reason) + 1)
