"""Plain-text console output — no external dependencies."""

from models.market import ScoredOpportunity


def print_opportunities(opportunities: list[ScoredOpportunity]) -> None:
    if not opportunities:
        print("\nNo opportunities found — check API keys and items.txt.")
        return

    width = 60
    print("\n" + "=" * width)
    print(f"  TOP {len(opportunities)} OPPORTUNITIES")
    print("=" * width)

    for rank, opp in enumerate(opportunities, start=1):
        print(f"\n#{rank}  {opp.market_hash_name}")
        print("-" * width)

        _row("Buy price",     _fmt_usd(opp.buy_price))
        _row("Expected sell", _fmt_usd(opp.sell_price))
        _row("Net sell (-13% fee)", _fmt_usd(opp.net_sell))

        if opp.profit is not None:
            sign = "+" if opp.profit >= 0 else ""
            _row("Profit", f"{sign}{opp.profit:.4f} USD")
        else:
            _row("Profit", "N/A")

        if opp.roi is not None:
            sign = "+" if opp.roi >= 0 else ""
            _row("ROI", f"{sign}{opp.roi * 100:.2f}%")
        else:
            _row("ROI", "N/A")

        _row("Volume 24h", str(opp.volume_24h) if opp.volume_24h is not None else "N/A")
        _row("Sources",    ", ".join(opp.sources) if opp.sources else "—")
        _row("Score",      f"{opp.score}/100")
        _row("Reason",     opp.reason)

    print("\n" + "=" * width)


def _row(label: str, value: str, width: int = 20) -> None:
    print(f"  {label:<{width}} {value}")


def _fmt_usd(v: float | None) -> str:
    return f"{v:.4f} USD" if v is not None else "N/A"
