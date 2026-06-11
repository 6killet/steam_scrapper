"""Tests for services/filters.py — table of accept/reject cases per filter.

Each case builds a minimal list[MarketPrice] and asserts which reject reason
(or none) LiquidityFilter assigns, plus the summary counters.
"""
from datetime import datetime, timezone

import pytest

from models.market import MarketPrice
from services.filters import LiquidityFilter

_NOW = datetime.now(timezone.utc)


def _mp(
    source: str = "steamwebapi",
    buy_price: float | None = 8.0,
    sell_price: float | None = 10.0,
    volume_24h: int | None = 50,
    buy_order_qty: int | None = None,
    steam_buy_order: float | None = None,
) -> MarketPrice:
    return MarketPrice(
        source=source,
        market_hash_name="Test Item",
        buy_price=buy_price,
        sell_price=sell_price,
        volume_24h=volume_24h,
        buy_order_qty=buy_order_qty,
        steam_buy_order=steam_buy_order,
        updated_at=_NOW,
    )


def _default_filter() -> LiquidityFilter:
    return LiquidityFilter(
        min_volume_24h=10,
        min_price_usd=1.0,
        max_price_usd=500.0,
        min_sources=2,
        max_source_divergence_pct=15.0,
        min_buy_order_qty=3,
    )


def _reason(prices: list[MarketPrice]) -> str:
    return _default_filter()._reject_reason(prices)


# ── table of cases ─────────────────────────────────────────────────────────────

GOOD_PAIR = [_mp(source="steamwebapi"), _mp(source="cs2sh", buy_order_qty=5)]


def test_good_item_passes():
    assert _reason(GOOD_PAIR) == ""


@pytest.mark.parametrize(
    "prices, expected",
    [
        # no source has volume data
        ([_mp(volume_24h=None), _mp(source="cs2sh", volume_24h=None)], "no_volume_data"),
        # best volume below threshold
        ([_mp(volume_24h=3), _mp(source="cs2sh", volume_24h=9)], "low_volume"),
        # price below corridor
        ([_mp(buy_price=0.50), _mp(source="cs2sh", buy_price=0.55)], "price_out_of_range"),
        # price above corridor
        ([_mp(buy_price=900.0), _mp(source="cs2sh", buy_price=910.0)], "price_out_of_range"),
        # only one independent source
        ([_mp(), _mp()], "insufficient_sources"),
        # sell prices diverge >15% between sources
        ([_mp(sell_price=10.0), _mp(source="cs2sh", sell_price=12.0)], "divergent_data"),
        # buy order qty known and below minimum
        ([_mp(buy_order_qty=1), _mp(source="cs2sh", buy_order_qty=2)], "low_buy_order_qty"),
    ],
)
def test_reject_reasons(prices, expected):
    assert _reason(prices) == expected


def test_volume_from_any_source_is_enough():
    # one source has no volume, the other does — max() wins
    prices = [_mp(volume_24h=None), _mp(source="cs2sh", volume_24h=25)]
    assert _reason(prices) == ""


def test_qty_unknown_passes_filter():
    # no source provides qty → filter must NOT reject (soft flag handled in Scorer)
    prices = [_mp(buy_order_qty=None), _mp(source="cs2sh", buy_order_qty=None)]
    assert _reason(prices) == ""


def test_divergence_at_threshold_passes():
    # exactly 15.0% is allowed (strictly greater rejects)
    prices = [_mp(sell_price=10.0), _mp(source="cs2sh", sell_price=11.5)]
    assert _reason(prices) == ""


def test_price_range_uses_sell_when_no_buy():
    prices = [
        _mp(buy_price=None, sell_price=0.40),
        _mp(source="cs2sh", buy_price=None, sell_price=0.45),
    ]
    assert _reason(prices) == "price_out_of_range"


# ── apply(): summary counters ──────────────────────────────────────────────────

def test_apply_counts_funnel():
    items = {
        "good": GOOD_PAIR,
        "thin": [_mp(volume_24h=1), _mp(source="cs2sh", volume_24h=2)],
        "lonely": [_mp()],
        "spread": [_mp(sell_price=10.0), _mp(source="cs2sh", sell_price=20.0)],
    }
    passed, summary = _default_filter().apply(items)

    assert set(passed) == {"good"}
    assert summary.total == 4
    assert summary.passed == 1
    assert summary.filtered == 3
    assert summary.low_volume == 1
    assert summary.insufficient_sources == 1
    assert summary.divergent_data == 1


def test_funnel_str_mentions_reasons():
    items = {"thin": [_mp(volume_24h=1), _mp(source="cs2sh", volume_24h=2)]}
    _, summary = _default_filter().apply(items)
    s = summary.funnel_str(top_n=20)
    assert "1 total" in s
    assert "low_volume=1" in s
