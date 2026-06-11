"""Price stability and sell-price suggestion from accumulated snapshot history.

Activates per item when >= min_days of sell_listing snapshots are available.
Uses harmonic-mean sliding windows (Soniclev/steam_csmoney pattern) and a
reverse-CDF percentile for the suggested listing price.

Works with any objects that have:  .ts, .price, .volume, .price_type, .market
(compatible with PriceSnapshotDB and test fakes alike).
"""

import logging
from dataclasses import dataclass
from datetime import timedelta
from statistics import median, pstdev

logger = logging.getLogger(__name__)

_DEFAULT_WINDOW = 15


@dataclass
class HistoryResult:
    is_stable: bool | None = None
    stability_cv: float | None = None
    suggested_sell_price: float | None = None
    history_insufficient: bool = False
    avg_price_7d: float | None = None   # used for trans_ratio in weighted_ratio


class HistoryAnalyzer:
    def __init__(self, min_days: int = 7, window_size: int = _DEFAULT_WINDOW) -> None:
        self.min_days = min_days
        self.window_size = window_size

    # ── public API ─────────────────────────────────────────────────────────────

    def analyze(self, snapshots: list) -> HistoryResult:
        """Analyse one item's snapshot list and return a HistoryResult."""
        if not snapshots:
            return HistoryResult(history_insufficient=True)

        sell_pts = sorted(
            [
                (float(s.price), s.volume or 0, s.ts)
                for s in snapshots
                if s.price_type == "sell_listing" and s.market == "steam" and s.price
            ],
            key=lambda x: x[2],
        )
        if not sell_pts:
            return HistoryResult(history_insufficient=True)

        ts_list = [t for _, _, t in sell_pts]
        span_days = (max(ts_list) - min(ts_list)).days
        if span_days < self.min_days:
            return HistoryResult(history_insufficient=True)

        # 7-day rolling average for trans_ratio
        cutoff_7d = max(ts_list) - timedelta(days=7)
        recent = [p for p, _, t in sell_pts if t >= cutoff_7d]
        avg_price_7d = sum(recent) / len(recent) if recent else None

        if len(sell_pts) < self.window_size:
            return HistoryResult(history_insufficient=True, avg_price_7d=avg_price_7d)

        prices_seq = [p for p, _, _ in sell_pts]
        volumes_seq = [v for _, v, _ in sell_pts]

        windows = [
            (prices_seq[i: i + self.window_size], volumes_seq[i: i + self.window_size])
            for i in range(len(prices_seq) - self.window_size + 1)
        ]
        window_means = [m for m in (_weighted_harmonic_mean(ps, vs) for ps, vs in windows) if m > 0]

        if len(window_means) < 2:
            return HistoryResult(history_insufficient=True, avg_price_7d=avg_price_7d)

        is_stable, cv = _stability_check(window_means)
        ref = avg_price_7d or median(prices_seq)
        # Suggest from the last 7 days (per TZ); fall back to the full series
        # when the recent window is too thin to be representative.
        suggest_seq = recent if len(recent) >= 5 else prices_seq
        suggested = _percentile_sell(suggest_seq, ref)

        return HistoryResult(
            is_stable=is_stable,
            stability_cv=cv,
            suggested_sell_price=suggested,
            history_insufficient=False,
            avg_price_7d=avg_price_7d,
        )

    def analyze_bulk(self, history_by_item: dict) -> dict[str, HistoryResult]:
        return {name: self.analyze(snaps) for name, snaps in history_by_item.items()}


# ── module-level helpers (also used in scorer for weighted_ratio) ──────────────

def compute_weighted_ratio(
    ext_price: float | None,
    steam_buy_order: float | None,
    steam_sell_listing: float | None,
    hist: HistoryResult | None,
) -> float | None:
    """Compute weighted ratio (lower = better arbitrage).

    weighted = buy_ratio*0.4 + sell_ratio*0.2 + trans_ratio*0.4
    ratio = external_price / corresponding_steam_price.
    Missing components shrink to available weights (normalized).
    """
    if ext_price is None or ext_price <= 0:
        return None
    comps: list[tuple[float, float]] = []
    if steam_buy_order and steam_buy_order > 0:
        comps.append((ext_price / steam_buy_order, 0.4))
    if steam_sell_listing and steam_sell_listing > 0:
        comps.append((ext_price / steam_sell_listing, 0.2))
    if hist and hist.avg_price_7d and hist.avg_price_7d > 0:
        comps.append((ext_price / hist.avg_price_7d, 0.4))
    if not comps:
        return None
    total_w = sum(w for _, w in comps)
    return sum(v * w for v, w in comps) / total_w


# ── private helpers ────────────────────────────────────────────────────────────

def _weighted_harmonic_mean(prices: list[float], volumes: list[int]) -> float:
    """Weighted harmonic mean; if all volumes are 0 use equal weights."""
    if not prices:
        return 0.0
    weights = [max(v, 1) for v in volumes]
    denom = sum(w / p for w, p in zip(weights, prices) if p > 0)
    if denom == 0:
        return 0.0
    return sum(weights) / denom


def _stability_check(means: list[float]) -> tuple[bool, float]:
    """Apply four stability conditions; return (is_stable, cv)."""
    med = median(means)
    if med <= 0:
        return False, 1.0
    cv = pstdev(means) / med
    # 1. Low coefficient of variation
    cv_ok = cv < 0.06
    # 2. No declining trend: first window mean must not exceed last by >1%
    trend_ok = not (means[0] > means[-1] * 1.01)
    # 3. Minimum window mean not below median by more than 10%
    min_ok = min(means) >= med * 0.90
    # 4. Maximum window mean not above median by more than 10%
    max_ok = max(means) <= med * 1.10
    return all((cv_ok, trend_ok, min_ok, max_ok)), cv


def _percentile_sell(prices: list[float], ref_price: float) -> float | None:
    """Suggest a sell price via reverse-CDF percentile.

    P50 for items < $100 (50% of sales at or above this price).
    P20 for items >= $100 (20% of sales at or above this price = 80th percentile).
    """
    if not prices:
        return None
    s = sorted(prices)
    n = len(s)
    quantile = 0.50 if ref_price < 100.0 else 0.80
    idx = min(int(quantile * n), n - 1)
    return s[idx]
