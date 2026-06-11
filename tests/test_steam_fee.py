"""Tests for services/steam_fee.py"""

import pytest
from decimal import Decimal

from services.steam_fee import add_fee, subtract_fee, floor_cents


class TestFloorCents:
    def test_rounds_down(self):
        assert floor_cents(Decimal("0.999")) == Decimal("0.99")

    def test_rounds_down_mid(self):
        assert floor_cents(Decimal("0.019")) == Decimal("0.01")

    def test_exact_cent_unchanged(self):
        assert floor_cents(Decimal("0.50")) == Decimal("0.50")

    def test_zero(self):
        assert floor_cents(Decimal("0")) == Decimal("0")


class TestAddFee:
    def test_one_dollar(self):
        # game=0.10, steam=0.05 → buyer pays $1.15
        assert add_fee(Decimal("1.00")) == Decimal("1.15")

    def test_minimum_seller_fees_at_floor(self):
        # seller $0.01: game=max(floor(0.001),0.01)=0.01, steam=0.01 → buyer=0.03
        assert add_fee(Decimal("0.01")) == Decimal("0.03")

    def test_floor_kicks_in_at_20_cents(self):
        # seller $0.20: game=floor(0.02)=0.02, steam=floor(0.01)=0.01 → 0.23
        assert add_fee(Decimal("0.20")) == Decimal("0.23")

    def test_ten_dollar(self):
        # game=1.00, steam=0.50 → buyer pays $11.50
        assert add_fee(Decimal("10.00")) == Decimal("11.50")

    def test_monotone_increasing(self):
        prev = add_fee(Decimal("0.01"))
        for cents in range(2, 201):
            curr = add_fee(Decimal(cents) * Decimal("0.01"))
            assert curr >= prev, f"add_fee not monotone at ${cents/100:.2f}"
            prev = curr


class TestSubtractFee:
    # ── Known pairs from TZ ─────────────────────────────────────────────────
    def test_known_1_15_to_1_00(self):
        assert subtract_fee(Decimal("1.15")) == Decimal("1.00")

    def test_known_0_23_to_0_20(self):
        assert subtract_fee(Decimal("0.23")) == Decimal("0.20")

    def test_known_0_03_to_0_01(self):
        assert subtract_fee(Decimal("0.03")) == Decimal("0.01")

    # ── Additional spot checks ───────────────────────────────────────────────
    def test_11_50_to_10_00(self):
        assert subtract_fee(Decimal("11.50")) == Decimal("10.00")

    def test_0_04_to_0_02(self):
        assert subtract_fee(Decimal("0.04")) == Decimal("0.02")

    # ── Round-trip: subtract_fee(add_fee(x)) == x ───────────────────────────
    @pytest.mark.parametrize("cents", range(1, 501))  # $0.01 – $5.00, every cent
    def test_round_trip_low_range(self, cents):
        seller = Decimal(cents) * Decimal("0.01")
        buyer = add_fee(seller)
        recovered = subtract_fee(buyer)
        assert recovered == seller, (
            f"Round-trip failed at seller ${seller}: "
            f"add_fee={buyer}, subtract_fee={recovered}"
        )

    @pytest.mark.parametrize("price", [
        "5.50", "10.00", "25.00", "50.00", "99.99",
        "100.00", "199.99", "250.00", "499.99", "500.00",
    ])
    def test_round_trip_high_range(self, price):
        seller = Decimal(price)
        buyer = add_fee(seller)
        recovered = subtract_fee(buyer)
        assert recovered == seller, (
            f"Round-trip failed at seller ${price}: "
            f"add_fee={buyer}, subtract_fee={recovered}"
        )

    # ── Boundary: ≤ $0.66 table coverage ────────────────────────────────────
    @pytest.mark.parametrize("cents", range(1, 67))  # all seller prices in table
    def test_round_trip_table_range(self, cents):
        seller = Decimal(cents) * Decimal("0.01")
        buyer = add_fee(seller)
        recovered = subtract_fee(buyer)
        assert recovered == seller, (
            f"Table round-trip failed at seller ${seller}: "
            f"add_fee={buyer}, subtract_fee={recovered}"
        )
