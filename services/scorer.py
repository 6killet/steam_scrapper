"""Aggregates MarketPrice records per item and produces ScoredOpportunity.

Two ROI metrics (buff2steam pattern):
  roi_realistic  — buy external, sell via Steam buy order (instant cashout)
                   = (subtract_fee(steam_buy_order) - ext_price) / ext_price
  roi_optimistic — buy external, list on Steam Market and wait
                   = (subtract_fee(steam_sell_listing) - ext_price) / ext_price

Score (0–100) and sort key: roi_realistic.
Items without any steam_buy_order are marked incomplete=True and excluded
from the default top; pass include_incomplete=True to include them.

Score formula:
    score = roi_score + liquidity_score - risk_score   (clamped [0, 100])

ROI score (max 60):    < 0% → 0 | 0–1% → 10 | 1–3% → 25 | 3–5% → 40 | >5% → 60
Liquidity score (40):  <10 → 0  | 10–49 → 10 | 50–199 → 25 | ≥200 → 40
Risk penalty:          no volume → +30 | vol<5 → +30 | no sell → +50 | no buy → +50
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal

from models.market import MarketPrice, ScoredOpportunity
from services.history_analyzer import HistoryResult, compute_weighted_ratio
from services.steam_fee import subtract_fee

logger = logging.getLogger(__name__)


class Scorer:
    def __init__(self, steam_sell_fee: float = 0.13, stability_weight: int = 10) -> None:
        self._legacy_fee = steam_sell_fee  # kept only for fallback / legacy callers
        self.stability_weight = stability_weight

    def score_all(
        self,
        prices_by_item: dict[str, list[MarketPrice]],
        include_incomplete: bool = False,
        history_by_item: dict[str, HistoryResult] | None = None,
    ) -> list[ScoredOpportunity]:
        results = []
        for name, prices in prices_by_item.items():
            hist = history_by_item.get(name) if history_by_item else None
            opp = self._score_item(name, prices, hist)
            if opp is None:
                continue
            if opp.incomplete and not include_incomplete:
                continue
            results.append(opp)
        return results

    def _score_item(
        self, name: str, prices: list[MarketPrice], hist: HistoryResult | None = None
    ) -> ScoredOpportunity | None:
        if not prices:
            return None

        sources = sorted({p.source for p in prices})

        # ── Best external buy price ───────────────────────────────────────────
        # buy_market = marketplace name when the source reported it, else source name
        buy_candidates = [
            (p.buy_price, p.buy_market or p.source) for p in prices if p.buy_price
        ]
        if buy_candidates:
            ext_price, buy_market = min(buy_candidates, key=lambda x: x[0])
        else:
            ext_price, buy_market = None, None

        # ── Steam prices ──────────────────────────────────────────────────────
        buy_orders = [p.steam_buy_order for p in prices if p.steam_buy_order]
        steam_buy_order = max(buy_orders) if buy_orders else None

        sell_listings = [p.sell_price for p in prices if p.sell_price]
        steam_sell_listing = max(sell_listings) if sell_listings else None

        # ── Volume ────────────────────────────────────────────────────────────
        vols = [p.volume_24h for p in prices if p.volume_24h is not None]
        volume_24h = max(vols) if vols else None

        # ── Economics with exact Steam fee ────────────────────────────────────
        # Realistic: instant cashout via buy order
        net_realistic: float | None = None
        roi_realistic: float | None = None
        net_profit_usd: float | None = None
        if steam_buy_order and ext_price:
            net_r = float(subtract_fee(Decimal(str(steam_buy_order))))
            net_realistic = net_r
            roi_realistic = (net_r - ext_price) / ext_price
            net_profit_usd = net_r - ext_price

        # Optimistic: list on Steam, wait for sale
        net_optimistic: float | None = None
        roi_optimistic: float | None = None
        if steam_sell_listing and ext_price:
            net_o = float(subtract_fee(Decimal(str(steam_sell_listing))))
            net_optimistic = net_o
            roi_optimistic = (net_o - ext_price) / ext_price

        # ── Scoring (driven by roi_realistic) ─────────────────────────────────
        roi_for_score = roi_realistic if roi_realistic is not None else roi_optimistic
        roi_score = self._roi_score(roi_for_score)
        liq_score = self._liquidity_score(volume_24h)
        risk = self._risk_penalty(ext_price, steam_sell_listing, volume_24h, steam_buy_order)
        score = max(0, min(100, roi_score + liq_score - risk))

        incomplete = steam_buy_order is None
        qty_unknown = not any(p.buy_order_qty is not None for p in prices)
        reason = self._reason(roi_realistic, roi_optimistic, volume_24h, ext_price, steam_buy_order)
        updated_at = max((p.updated_at for p in prices), default=datetime.now(timezone.utc))

        # ── Stage 4 — history enrichment ─────────────────────────────────────
        is_stable: bool | None = None
        stability_cv: float | None = None
        suggested_sell_price: float | None = None
        history_insufficient = False
        if hist is not None:
            is_stable = hist.is_stable
            stability_cv = hist.stability_cv
            suggested_sell_price = hist.suggested_sell_price
            history_insufficient = hist.history_insufficient
            if hist.is_stable:
                score = min(100, score + self.stability_weight)

        weighted_ratio = compute_weighted_ratio(ext_price, steam_buy_order, steam_sell_listing, hist)

        return ScoredOpportunity(
            market_hash_name=name,
            buy_price=ext_price,
            buy_market=buy_market,
            steam_buy_order=steam_buy_order,
            steam_sell_listing=steam_sell_listing,
            sell_price=steam_sell_listing,
            net_sell=net_optimistic,
            roi_realistic=roi_realistic,
            roi_optimistic=roi_optimistic,
            roi=roi_realistic,
            net_profit_usd=net_profit_usd,
            profit=net_profit_usd,
            volume_24h=volume_24h,
            score=score,
            sources=sources,
            reason=reason,
            incomplete=incomplete,
            qty_unknown=qty_unknown,
            is_stable=is_stable,
            stability_cv=stability_cv,
            suggested_sell_price=suggested_sell_price,
            history_insufficient=history_insufficient,
            weighted_ratio=weighted_ratio,
            updated_at=updated_at,
        )

    # ── Scoring helpers ────────────────────────────────────────────────────────

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
        steam_buy_order: float | None,
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
        if steam_buy_order is None:
            penalty += 20
        return penalty

    def _reason(
        self,
        roi_realistic: float | None,
        roi_optimistic: float | None,
        volume_24h: int | None,
        buy_price: float | None,
        steam_buy_order: float | None,
    ) -> str:
        parts: list[str] = []

        roi = roi_realistic if roi_realistic is not None else roi_optimistic
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

        if roi_realistic is None and roi_optimistic is not None:
            parts.append("no buy order (optimistic only)")

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
            parts.append("no external price")
        if steam_buy_order is None:
            parts.append("no cashout data")

        return ", ".join(parts)
