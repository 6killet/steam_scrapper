"""Steam Market fee calculation using floor arithmetic.

Steam charges two separate fees, each floored to the nearest cent with a
$0.01 minimum:
  - Publisher fee (CS2/Valve): 10%
  - Steam transaction fee:      5%

Buyer pays:  seller_price + publisher_fee + steam_fee
Seller gets: buyer_price  - publisher_fee - steam_fee  = seller_price

For prices ≤ $0.66 the iterative back-calculation is unreliable because
floor rounding creates gaps (e.g. no seller price maps to buyer $0.22).
A lookup table pre-computed at import time handles this range exactly.
"""

from decimal import Decimal, ROUND_FLOOR
from functools import lru_cache

_CENT = Decimal("0.01")
_EIGHTY_PCT = Decimal("0.80")


def floor_cents(price: Decimal) -> Decimal:
    """Floor a Decimal to the nearest cent."""
    return (price / _CENT).to_integral_value(rounding=ROUND_FLOOR) * _CENT


def add_fee(price: Decimal) -> Decimal:
    """Return what the buyer pays given the seller's listing price."""
    game = max(floor_cents(price * Decimal("0.10")), _CENT)
    steam = max(floor_cents(price * Decimal("0.05")), _CENT)
    return price + game + steam


# ── Low-price lookup table ────────────────────────────────────────────────────

def _build_low_price_table() -> dict[int, Decimal]:
    """Map buyer_cents → seller_price for all seller prices $0.01–$0.66.

    Built once at import; covers all edge cases where floor gaps exist.
    Multiple seller prices can map to the same buyer price (e.g. after a fee
    step-up); we store only the first (lowest) matching seller price so that
    subtract_fee is conservative.
    """
    table: dict[int, Decimal] = {}
    for cents in range(1, 67):          # seller $0.01 … $0.66
        seller = Decimal(cents) * _CENT
        buyer_cents = int(add_fee(seller) * 100)
        if buyer_cents not in table:    # keep first (lowest) match
            table[buyer_cents] = seller
    return table


_LOW_TABLE: dict[int, Decimal] = _build_low_price_table()
_LOW_TABLE_MAX_BUYER: int = max(_LOW_TABLE)   # = 75 (buyer price for seller $0.66)


# ── Public API ────────────────────────────────────────────────────────────────

@lru_cache(maxsize=4096)
def subtract_fee(price: Decimal) -> Decimal:
    """Return the seller's net proceeds given the buyer-pays price.

    Uses the precomputed lookup table for buyer prices ≤ table max;
    iterative convergence (step up from 80%) for higher prices.
    """
    if price <= _CENT:
        return _CENT

    buyer_cents = int(price * 100)

    # ── Low-price path: exact lookup with gap fallback ──
    if buyer_cents <= _LOW_TABLE_MAX_BUYER:
        if buyer_cents in _LOW_TABLE:
            return _LOW_TABLE[buyer_cents]
        # Gap: return seller price for nearest lower valid buyer price
        for bc in range(buyer_cents - 1, 2, -1):
            if bc in _LOW_TABLE:
                return _LOW_TABLE[bc]
        return _CENT

    # ── High-price path: iterative convergence ──
    est = floor_cents(price * _EIGHTY_PCT)
    if est < _CENT:
        est = _CENT

    while add_fee(est) < price:
        est += _CENT

    if add_fee(est) == price:
        return est
    return max(est - _CENT, _CENT)
