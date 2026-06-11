"""Plain-text console output — no external dependencies."""

from models.market import ScoredOpportunity


def print_opportunities(
    opportunities: list[ScoredOpportunity],
    title: str = "TOP OPPORTUNITIES",
) -> None:
    if not opportunities:
        print("\nNo opportunities found — check API keys and items.txt.")
        return

    width = 62
    print("\n" + "=" * width)
    print(f"  {title} ({len(opportunities)} items)")
    print("=" * width)

    for rank, opp in enumerate(opportunities, start=1):
        label = opp.market_hash_name
        if opp.incomplete:
            label += "  [no cashout data]"
        print(f"\n#{rank}  {label}")
        print("-" * width)

        buy_label = f"Buy ({opp.buy_market})" if opp.buy_market else "Buy price"
        _row(buy_label,         _fmt_usd(opp.buy_price))
        _row("Steam buy order", _fmt_usd(opp.steam_buy_order) + "  ← instant cashout")
        _row("Steam listing",   _fmt_usd(opp.steam_sell_listing) + "  ← list & wait")

        if opp.roi_realistic is not None:
            sign = "+" if opp.roi_realistic >= 0 else ""
            _row("ROI realistic",  f"{sign}{opp.roi_realistic * 100:.2f}%  (sort key)")
        else:
            _row("ROI realistic",  "N/A")

        if opp.roi_optimistic is not None:
            sign = "+" if opp.roi_optimistic >= 0 else ""
            _row("ROI optimistic", f"{sign}{opp.roi_optimistic * 100:.2f}%")
        else:
            _row("ROI optimistic", "N/A")

        if opp.net_profit_usd is not None:
            sign = "+" if opp.net_profit_usd >= 0 else ""
            _row("Profit (order)", f"{sign}{opp.net_profit_usd:.4f} USD")
        else:
            _row("Profit (order)", "N/A")

        _row("Volume 24h", str(opp.volume_24h) if opp.volume_24h is not None else "N/A")
        _row("Sources",    ", ".join(opp.sources) if opp.sources else "—")
        _row("Score",      f"{opp.score}/100")

        if opp.history_insufficient:
            _row("History",    "insufficient (<7d data)")
        elif opp.is_stable is not None:
            cv_str = f"CV {opp.stability_cv:.3f}" if opp.stability_cv is not None else ""
            _row("Stability",  ("stable " if opp.is_stable else "unstable ") + cv_str)
            if opp.suggested_sell_price is not None:
                _row("Suggest sell", _fmt_usd(opp.suggested_sell_price))

        _row("Reason",     opp.reason)

    print("\n" + "=" * width)


def print_incomplete(opportunities: list[ScoredOpportunity]) -> None:
    """Print items that had no Steam buy order data."""
    incomplete = [o for o in opportunities if o.incomplete]
    if not incomplete:
        return
    print_opportunities(incomplete, title="NO CASHOUT DATA (--show-incomplete)")


def _row(label: str, value: str, width: int = 18) -> None:
    print(f"  {label:<{width}} {value}")


def _fmt_usd(v: float | None) -> str:
    return f"${v:.4f}" if v is not None else "N/A"
