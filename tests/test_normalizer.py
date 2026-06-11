"""Tests for services/normalizer.py — parsing of saved JSON fixtures.

Fixtures in tests/fixtures/ mirror real response shapes of the three APIs,
including edge cases: missing names, zero/negative/garbage prices,
absent buy orders, alternative timestamp formats.
"""
import json
from datetime import timezone
from pathlib import Path

import pytest

from services.normalizer import Normalizer

_FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> list[dict]:
    return json.loads((_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def normalizer() -> Normalizer:
    return Normalizer()


# ── Pricempire ─────────────────────────────────────────────────────────────────

def test_pricempire_parses_fixture(normalizer):
    results = normalizer.normalize("pricempire", _load("pricempire"))
    by_name = {r.market_hash_name: r for r in results}

    ak = by_name["AK-47 | Redline (Field-Tested)"]
    # cents → USD
    assert ak.sell_price == pytest.approx(10.20)
    assert ak.steam_price == pytest.approx(10.20)
    # cheapest external = buff163 at 8.40
    assert ak.buy_price == pytest.approx(8.40)
    assert ak.buy_market == "buff163"
    assert ak.listings_count == 312
    assert ak.updated_at.tzinfo is not None


def test_pricempire_single_external_market(normalizer):
    results = normalizer.normalize("pricempire", _load("pricempire"))
    awp = next(r for r in results if "AWP" in r.market_hash_name)
    assert awp.buy_price == pytest.approx(82.00)
    assert awp.buy_market == "waxpeer"


def test_pricempire_steam_only_falls_back_to_steam_price(normalizer):
    results = normalizer.normalize("pricempire", _load("pricempire"))
    sticker = next(r for r in results if "Sticker" in r.market_hash_name)
    assert sticker.buy_price == pytest.approx(1.50)
    assert sticker.buy_market is None


def test_pricempire_zero_price_becomes_none(normalizer):
    results = normalizer.normalize("pricempire", _load("pricempire"))
    broken = next(r for r in results if "Broken" in r.market_hash_name)
    assert broken.sell_price is None
    assert broken.buy_price is None


# ── SteamWebAPI ────────────────────────────────────────────────────────────────

def test_steamwebapi_parses_fixture(normalizer):
    results = normalizer.normalize("steamwebapi", _load("steamwebapi"))
    by_name = {r.market_hash_name: r for r in results}

    ak = by_name["AK-47 | Redline (Field-Tested)"]
    assert ak.sell_price == pytest.approx(10.2)
    assert ak.buy_price == pytest.approx(8.4)
    assert ak.steam_buy_order == pytest.approx(9.85)
    assert ak.volume_24h == 142
    assert ak.volume_7d == 980
    assert ak.updated_at.tzinfo is not None


def test_steamwebapi_name_fallback_and_null_buy_order(normalizer):
    results = normalizer.normalize("steamwebapi", _load("steamwebapi"))
    # second item uses "name" instead of "market_hash_name"
    awp = next(r for r in results if "AWP" in r.market_hash_name)
    assert awp.steam_buy_order is None
    # "2026-06-10 12:01:30" (space-separated) must parse, not fall back blindly
    assert awp.updated_at.year == 2026


def test_steamwebapi_garbage_values_become_none(normalizer):
    results = normalizer.normalize("steamwebapi", _load("steamwebapi"))
    glock = next(r for r in results if "Glock" in r.market_hash_name)
    assert glock.sell_price is None      # "not-a-number"
    assert glock.buy_price is None       # negative → None
    assert glock.steam_buy_order is None # 0 → None
    assert glock.volume_24h is None      # "abc"


def test_steamwebapi_item_without_name_is_skipped(normalizer):
    results = normalizer.normalize("steamwebapi", _load("steamwebapi"))
    assert len(results) == 3  # fixture has 4 entries, one without any name


# ── cs2.sh ─────────────────────────────────────────────────────────────────────

def test_cs2sh_parses_fixture(normalizer):
    results = normalizer.normalize("cs2sh", _load("cs2sh"))
    by_name = {r.market_hash_name: r for r in results}

    ak = by_name["AK-47 | Redline (Field-Tested)"]
    assert ak.sell_price == pytest.approx(10.2)
    assert ak.steam_buy_order == pytest.approx(9.85)
    assert ak.buy_order_qty == 12
    # cheapest across buff/skinport/csfloat/youpin/c5game = buff 8.45
    assert ak.buy_price == pytest.approx(8.45)
    assert ak.buy_market == "buff163"
    assert ak.listings_count == 305
    # volume proxy from skinport sales history
    assert ak.volume_24h == 42
    assert ak.volume_7d == 260


def test_cs2sh_skinport_only_external(normalizer):
    results = normalizer.normalize("cs2sh", _load("cs2sh"))
    awp = next(r for r in results if "AWP" in r.market_hash_name)
    assert awp.buy_price == pytest.approx(84.0)
    assert awp.buy_market == "skinport"
    assert awp.steam_buy_order is None
    assert awp.buy_order_qty is None


def test_cs2sh_steam_only_has_no_external(normalizer):
    results = normalizer.normalize("cs2sh", _load("cs2sh"))
    knife = next(r for r in results if "Knife" in r.market_hash_name)
    assert knife.buy_price is None
    assert knife.buy_market is None
    assert knife.steam_buy_order == pytest.approx(240.0)


def test_cs2sh_item_without_name_is_skipped(normalizer):
    results = normalizer.normalize("cs2sh", _load("cs2sh"))
    assert len(results) == 3


# ── general ────────────────────────────────────────────────────────────────────

def test_unknown_source_returns_empty(normalizer):
    assert normalizer.normalize("nosuch", [{"market_hash_name": "x"}]) == []


def test_all_timestamps_are_utc_aware(normalizer):
    for src in ("pricempire", "steamwebapi", "cs2sh"):
        for mp in normalizer.normalize(src, _load(src)):
            assert mp.updated_at.tzinfo is not None
            assert mp.updated_at.utcoffset() == timezone.utc.utcoffset(None)
