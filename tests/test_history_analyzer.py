"""Tests for services/history_analyzer.py.

Covers:
- _weighted_harmonic_mean (known values)
- _stability_check via HistoryAnalyzer.analyze:
    stable series / declining trend / outlier / high CV
- history_insufficient when data span < min_days or too few points
- percentile sell suggestion (known distribution → known answer)
- analyze_bulk (dict in → dict out)
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from services.history_analyzer import (
    HistoryAnalyzer,
    HistoryResult,
    _percentile_sell,
    _weighted_harmonic_mean,
)


# ── test fixture factory ───────────────────────────────────────────────────────

@dataclass
class FakeSnap:
    ts: datetime
    price: Decimal
    volume: int | None
    price_type: str = "sell_listing"
    market: str = "steam"
    market_hash_name: str = "test"
    source: str = "test"
    currency_original: str = "USD"


def _snap(price: float, days_ago: float, volume: int = 50) -> FakeSnap:
    ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return FakeSnap(ts=ts, price=Decimal(str(price)), volume=volume)


def _series(prices: list[float], days_span: float = 10.0) -> list[FakeSnap]:
    """Spread N prices evenly over days_span days (oldest first)."""
    n = len(prices)
    step = days_span / max(n - 1, 1)
    return [_snap(p, days_span - i * step) for i, p in enumerate(prices)]


# Use small window for faster, easier-to-reason tests
_ANALYZER = HistoryAnalyzer(min_days=7, window_size=5)


# ── weighted harmonic mean ─────────────────────────────────────────────────────

def test_whm_equal_prices():
    assert _weighted_harmonic_mean([10.0, 10.0, 10.0], [1, 1, 1]) == pytest.approx(10.0)


def test_whm_two_values():
    # H(2, 4; w=1,1) = (1+1) / (1/2 + 1/4) = 2/0.75 ≈ 2.667
    result = _weighted_harmonic_mean([2.0, 4.0], [1, 1])
    assert result == pytest.approx(2.667, rel=1e-3)


def test_whm_zero_volume_treated_as_one():
    # volumes=[0,0] → treat as [1,1] — same as unweighted
    assert _weighted_harmonic_mean([10.0, 10.0], [0, 0]) == pytest.approx(10.0)


def test_whm_empty():
    assert _weighted_harmonic_mean([], []) == 0.0


# ── stability: stable series ───────────────────────────────────────────────────

def test_stable_series():
    prices = [10.0, 10.1, 9.9, 10.0, 10.1, 9.8, 10.2, 10.0, 9.9, 10.1,
              10.0, 10.1, 10.0, 9.9, 10.0, 10.1, 9.8, 10.0, 10.2, 10.1]
    snaps = _series(prices, days_span=10.0)
    result = _ANALYZER.analyze(snaps)
    assert result.history_insufficient is False
    assert result.is_stable is True
    assert result.stability_cv is not None
    assert result.stability_cv < 0.06


# ── stability: declining trend ────────────────────────────────────────────────

def test_declining_trend_not_stable():
    # clear downtrend — first window mean > last window mean by >1%
    prices = [10.0, 9.8, 9.6, 9.4, 9.2, 9.0, 8.8, 8.6, 8.4, 8.2,
              8.0, 7.8, 7.6, 7.4, 7.2, 7.0, 6.8, 6.6, 6.4, 6.2]
    snaps = _series(prices, days_span=10.0)
    result = _ANALYZER.analyze(snaps)
    assert result.history_insufficient is False
    assert result.is_stable is False


# ── stability: max spike (max window mean > median + 10%) ────────────────────

def test_max_spike_not_stable():
    # Step change halfway up: upper half is 20, so window means go up to 20.
    # max(window_means) >> median * 1.10  → max_ok fails
    prices = [10.0] * 10 + [20.0] * 10
    snaps = _series(prices, days_span=10.0)
    result = _ANALYZER.analyze(snaps)
    assert result.history_insufficient is False
    assert result.is_stable is False


# ── stability: high CV ────────────────────────────────────────────────────────

def test_high_cv_not_stable():
    # Extreme alternating [5, 15]: harmonic window means ≈ 6.82 / 8.33,
    # cv ≈ 0.10 > 0.06  →  cv_ok fails
    prices = [5.0, 15.0] * 10
    snaps = _series(prices, days_span=10.0)
    result = _ANALYZER.analyze(snaps)
    assert result.history_insufficient is False
    assert result.is_stable is False


# ── history_insufficient ──────────────────────────────────────────────────────

def test_insufficient_too_few_days():
    # Only 2 days of span (< min_days=7)
    snaps = [_snap(10.0, 0.5), _snap(10.0, 1.0), _snap(10.0, 1.5), _snap(10.0, 2.0)]
    result = _ANALYZER.analyze(snaps)
    assert result.history_insufficient is True
    assert result.is_stable is None


def test_insufficient_too_few_points():
    # 10 days span but only 3 sell_listing points (< window_size=5)
    snaps = [_snap(10.0, 9.0), _snap(10.0, 5.0), _snap(10.0, 1.0)]
    result = _ANALYZER.analyze(snaps)
    assert result.history_insufficient is True


def test_insufficient_empty():
    assert _ANALYZER.analyze([]).history_insufficient is True


def test_non_steam_snaps_ignored():
    # Only external/sell_listing rows — should not count for stability
    snaps = [
        FakeSnap(ts=datetime.now(timezone.utc) - timedelta(days=9 - i),
                 price=Decimal("10.0"), volume=50,
                 price_type="sell_listing", market="external")
        for i in range(20)
    ]
    result = _ANALYZER.analyze(snaps)
    assert result.history_insufficient is True


# ── percentile sell suggestion ────────────────────────────────────────────────

def test_percentile_sell_p50_below_100():
    # Prices 1..10; P50 = index int(0.5*10)=5 → sorted[5]=6
    prices = [float(i) for i in range(1, 11)]
    result = _percentile_sell(prices, ref_price=50.0)
    assert result == pytest.approx(6.0)


def test_percentile_sell_p20_above_100():
    # Prices 1..10; P20 = 80th-percentile = index int(0.8*10)=8 → sorted[8]=9
    prices = [float(i) for i in range(1, 11)]
    result = _percentile_sell(prices, ref_price=150.0)
    assert result == pytest.approx(9.0)


def test_percentile_sell_empty():
    assert _percentile_sell([], ref_price=10.0) is None


def test_suggested_sell_in_result():
    prices = [10.0] * 20
    snaps = _series(prices, days_span=10.0)
    result = _ANALYZER.analyze(snaps)
    assert result.suggested_sell_price is not None
    assert result.suggested_sell_price == pytest.approx(10.0)


# ── analyze_bulk ──────────────────────────────────────────────────────────────

def test_analyze_bulk_keys():
    prices_a = [10.0] * 20
    prices_b = [10.0, 9.8, 9.6, 9.4, 9.2, 9.0, 8.8, 8.6, 8.4, 8.2,
                8.0, 7.8, 7.6, 7.4, 7.2, 7.0, 6.8, 6.6, 6.4, 6.2]
    history = {
        "item_a": _series(prices_a),
        "item_b": _series(prices_b),
    }
    results = _ANALYZER.analyze_bulk(history)
    assert set(results.keys()) == {"item_a", "item_b"}
    assert isinstance(results["item_a"], HistoryResult)
    assert isinstance(results["item_b"], HistoryResult)
