"""Aggregates MarketPrice records per item and produces ScoredOpportunity.

Score formula (0–100):
    score = roi_score + liquidity_score - risk_score   (clamped to [0, 100])

ROI score (max 60):
    < 0%        →  0
    0–1%        → 10
    1–3%        → 25
    3–5%        → 40
    > 5%        → 60

Liquidity score (max 40):
    volume_24h < 10   →  0
    10–49             → 10
    50–199            → 25
    ≥ 200             → 40

Risk penalty:
    no volume data    → +30
    volume_24h < 5    → +30
    no sell price     → +50
    no buy price      → +50
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from models.market import MarketPrice, ScoredOpportunity

logger = logging.getLogger(__name__)


class Scorer:
    def __init__(self, steam_sell_fee: float = 0.13) -> None:
        self.fee = steam_sell_fee

    def score_all(
        self, prices_by_item: dict[str, list[MarketPrice]]
    ) -> list[ScoredOpportunity]:
        return [
            opp
            for name, prices in prices_by_item.items()
            if (opp := self._score_item(name, prices)) is not None
        ]

    def _score_item(
        self, name: str, prices: list[MarketPrice]
    ) -> Optional[ScoredOpportunity]:
        if not prices:
            return None

        sources = sorted({p.source for p in prices})

        # Best buy price: cheapest across all sources
        buys = [p.buy_price for p in prices if p.buy_price]
        buy_price = min(buys) if buys else None

        # Best sell price: highest reliable Steam price across sources
        steam_prices = [p.steam_price for p in prices if p.steam_price]
        sell_candidates = [p.sell_price for p in prices if p.sell_price]
        sell_price = (
            max(steam_prices) if steam_prices
            else max(sell_candidates) if sell_candidates
            else None
        )

        # Volume: take the maximum reported value across sources
        vols_24h = [p.volume_24h for p in prices if p.volume_24h is not None]
        volume_24h = max(vols_24h) if vols_24h else None

        # Financial calculations
        net_sell: float | None = sell_price * (1.0 - self.fee) if sell_price else None
        profit: float | None = (
            net_sell - buy_price
            if (net_sell is not None and buy_price is not None)
            else None
        )
        roi: float | None = (
            profit / buy_price
            if (profit is not None and buy_price and buy_price > 0)
            else None
        )

        # Scoring
        roi_score = self._roi_score(roi)
        liq_score = self._liquidity_score(volume_24h)
        risk_penalty = self._risk_penalty(buy_price, sell_price, volume_24h)
        score = max(0, min(100, roi_score + liq_score - risk_penalty))

        reason = self._reason(roi, volume_24h, buy_price, sell_price)
        updated_at = max((p.updated_at for p in prices), default=datetime.now(timezone.utc))

        return ScoredOpportunity(
            market_hash_name=name,
            buy_price=buy_price,
            sell_price=sell_price,
            net_sell=net_sell,
            profit=profit,
            roi=roi,
            volume_24h=volume_24h,
            score=score,
            sources=sources,
            reason=reason,
            updated_at=updated_at,
        )

    # ------------------------------------------------------------------
    # Scoring helpers
    # ------------------------------------------------------------------

    def _roi_score(self, roi: float | None) -> int:
        if roi is None or roi < 0:
            return 0
        if roi < 0.01:
            return 10
        if roi < 0.03:
            return 25
        if roi < 0.05:
            return 40
        return 60

    def _liquidity_score(self, volume_24h: int | None) -> int:
        if volume_24h is None:
            return 0
        if volume_24h < 10:
            return 0
        if volume_24h < 50:
            return 10
        if volume_24h < 200:
            return 25
        return 40

    def _risk_penalty(
        self,
        buy_price: float | None,
        sell_price: float | None,
        volume_24h: int | None,
    ) -> int:
        penalty = 0
        if volume_24h is None:
            penalty += 30
        elif volume_24h < 5:
            penalty += 30
        if sell_price is None:
            penalty += 50
        if buy_price is None:
            penalty += 50
        return penalty

    def _reason(
        self,
        roi: float | None,
        volume_24h: int | None,
        buy_price: float | None,
        sell_price: float | None,
    ) -> str:
        parts: list[str] = []

        if roi is not None:
            if roi > 0.05:
                parts.append("high ROI >5%")
            elif roi > 0.03:
                parts.append("good ROI 3–5%")
            elif roi > 0.01:
                parts.append("moderate ROI 1–3%")
            elif roi > 0:
                parts.append("positive spread")
            else:
                parts.append("negative spread")
        else:
            parts.append("ROI unknown")

        if volume_24h is not None:
            if volume_24h >= 200:
                parts.append("high volume")
            elif volume_24h >= 50:
                parts.append("moderate volume")
            elif volume_24h >= 10:
                parts.append("low volume")
            else:
                parts.append("very low volume")
        else:
            parts.append("no volume data")

        if buy_price is None:
            parts.append("no buy price")
        if sell_price is None:
            parts.append("no sell price")

        return ", ".join(parts)
