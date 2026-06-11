"""Steam Skin Scraper — entry point.

Usage:
    python main.py scan     — one-shot: fetch prices, score, print top, save CSV
    python main.py collect  — daemon: collect every COLLECT_INTERVAL_MINUTES
    python main.py top      — print top from DB without any network calls
    python main.py bot      — Telegram bot (Stage 5, not yet implemented)
"""

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from config import settings
from output.console import print_incomplete, print_opportunities
from output.csv_exporter import export_to_csv
from services.collector import Collector
from services.filters import LiquidityFilter
from services.history_analyzer import HistoryAnalyzer
from services.normalizer import Normalizer
from services.scorer import Scorer
from sources.cs2sh import CS2SHSource
from sources.pricempire import PricempireSource
from sources.steamwebapi import SteamWebAPISource
from storage.db import create_engine_and_session, init_db
from storage.repositories import (
    BotMuteRepository,
    BotSettingsRepository,
    CollectRunRepository,
    MarketRepository,
    NotificationsSentRepository,
    PriceSnapshotRepository,
    RawRepository,
)

# Windows consoles often default to cp1251/cp866 which cannot print '→', '—'
# and similar characters used in the output; force UTF-8 instead of crashing.
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)-8s]  %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
# httpx logs full Telegram API URLs including the bot token — keep it quiet
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

_EXAMPLE_ITEMS = """\
# CS2 items to analyse — one market_hash_name per line.
# Lines starting with '#' are ignored.

AK-47 | Redline (Field-Tested)
AK-47 | Vulcan (Minimal Wear)
AK-47 | Asiimov (Field-Tested)
AWP | Asiimov (Field-Tested)
AWP | Lightning Strike (Factory New)
AWP | Medusa (Field-Tested)
M4A4 | Howl (Field-Tested)
M4A1-S | Hot Rod (Factory New)
M4A1-S | Cyrex (Field-Tested)
USP-S | Kill Confirmed (Field-Tested)
Glock-18 | Fade (Factory New)
Desert Eagle | Blaze (Factory New)
Desert Eagle | Cobalt Disruption (Factory New)
StatTrak™ AK-47 | Redline (Field-Tested)
StatTrak™ M4A1-S | Cyrex (Field-Tested)
Butterfly Knife | Fade (Factory New)
Karambit | Fade (Factory New)
M9 Bayonet | Doppler (Factory New)
Bayonet | Tiger Tooth (Factory New)
Flip Knife | Marble Fade (Factory New)
"""


# ── helpers ───────────────────────────────────────────────────────────────────

def _load_items_from_file(path: str) -> list[str]:
    p = Path(path)
    if not p.exists():
        logger.info("items.txt not found — writing example list to %s", p)
        p.write_text(_EXAMPLE_ITEMS, encoding="utf-8")
    return [
        line.strip()
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


def _build_sources() -> list:
    sources = []
    skipped = []

    if settings.PRICEMPIRE_API_TOKEN:
        sources.append(PricempireSource())
    else:
        skipped.append("Pricempire (PRICEMPIRE_API_TOKEN not set)")

    if settings.STEAMWEBAPI_KEY:
        sources.append(SteamWebAPISource())
    else:
        skipped.append("SteamWebAPI (STEAMWEBAPI_KEY not set)")

    if settings.CS2SH_API_KEY:
        sources.append(CS2SHSource())
    else:
        skipped.append("cs2.sh (CS2SH_API_KEY not set)")

    for name in skipped:
        logger.warning("Skipped: %s", name)

    return sources


async def _get_items(sources: list) -> list[str]:
    """Resolve item list: file mode, or universe mode from the first source
    that supports a bulk item list (Pricempire, cs2.sh)."""
    if settings.ITEMS_MODE == "universe":
        for src in sources:
            fetch = getattr(src, "fetch_universe_items", None)
            if fetch is None:
                continue
            logger.info(
                "Universe mode: fetching items from %s ($%.0f–$%.0f, max %d)…",
                src.name, settings.MIN_PRICE_USD, settings.MAX_PRICE_USD,
                settings.UNIVERSE_MAX_ITEMS,
            )
            items = await fetch(
                settings.MIN_PRICE_USD,
                settings.MAX_PRICE_USD,
                settings.UNIVERSE_MAX_ITEMS,
            )
            if items:
                logger.info("Universe: %d items loaded from %s", len(items), src.name)
                return items
        logger.warning("Universe mode: no source provided an item list, falling back to file mode.")
    return _load_items_from_file(settings.ITEMS_FILE)


def _build_history_analyzer() -> HistoryAnalyzer:
    return HistoryAnalyzer(min_days=settings.STABILITY_MIN_DAYS)


def _build_filter() -> LiquidityFilter:
    return LiquidityFilter(
        min_volume_24h=settings.MIN_VOLUME_24H,
        min_price_usd=settings.MIN_PRICE_USD,
        max_price_usd=settings.MAX_PRICE_USD,
        min_sources=settings.MIN_SOURCES,
        max_source_divergence_pct=settings.MAX_SOURCE_DIVERGENCE_PCT,
        min_buy_order_qty=settings.MIN_BUY_ORDER_QTY,
    )


async def _setup(with_sources: bool = True):
    """Initialise DB, repos and (optionally) sources."""
    engine, sf = create_engine_and_session(settings.DB_PATH)
    await init_db(engine)
    sources = _build_sources() if with_sources else []
    return (
        engine,
        sources,
        RawRepository(sf),
        MarketRepository(sf),
        PriceSnapshotRepository(sf),
        CollectRunRepository(sf),
    )


async def _one_pass(
    items: list[str],
    sources: list,
    raw_repo: RawRepository,
    market_repo: MarketRepository,
    snapshot_repo: PriceSnapshotRepository,
    run_repo: CollectRunRepository,
) -> tuple[dict, int]:
    """Run one full collection pass. Returns (prices_by_item, snapshots_saved)."""
    ts = datetime.now(timezone.utc)
    run_id = await run_repo.start()
    errors: list[str] = []
    prices_by_item: dict = {}
    snapshots_saved = 0

    try:
        collector = Collector(sources, raw_repo, market_repo, Normalizer())
        prices_by_item = await collector.collect(items)
        all_prices = [p for ps in prices_by_item.values() for p in ps]
        snapshots_saved = await snapshot_repo.save_from_market_prices(all_prices, ts)
    except Exception as exc:
        errors.append(str(exc))
        logger.error("Collection pass failed: %s", exc)
    finally:
        await run_repo.finish(run_id, len(items), snapshots_saved, errors)

    return prices_by_item, snapshots_saved


# ── subcommands ───────────────────────────────────────────────────────────────

async def cmd_scan(show_incomplete: bool = False) -> None:
    print("\n========================================")
    print("  Steam Skin Scraper — scan")
    print("========================================\n")

    engine, sources, raw_repo, market_repo, snapshot_repo, run_repo = await _setup()
    if not sources:
        print("ERROR: No API keys configured. Fill in .env and retry.")
        await engine.dispose()
        sys.exit(1)

    try:
        items = await _get_items(sources)
        if not items:
            print("ERROR: No items found. Add market_hash_names to items.txt and re-run.")
            return

        print(f"Items: {len(items)} | Sources: {', '.join(s.name for s in sources)}\n")
        print("Fetching prices…")

        prices_by_item, snapshots = await _one_pass(
            items, sources, raw_repo, market_repo, snapshot_repo, run_repo
        )

        total = sum(len(v) for v in prices_by_item.values())
        if total == 0:
            print("\nNo price data returned. Check API keys and network connectivity.")
            return

        print(f"Prices: {total} records for {len(prices_by_item)} items | Snapshots saved: {snapshots}\n")

        liquidity = _build_filter()
        filtered_items, summary = liquidity.apply(prices_by_item)
        print(summary.funnel_str(top_n=settings.TOP_N_ITEMS))
        print()

        # Fetch accumulated history and run stability analysis
        hist_raw = await snapshot_repo.get_history_bulk(
            list(filtered_items.keys()),
            days=settings.STABILITY_MIN_DAYS * 2 + 7,
        )
        history_by_item = _build_history_analyzer().analyze_bulk(hist_raw)

        scorer = Scorer(
            steam_sell_fee=settings.STEAM_SELL_FEE,
            stability_weight=settings.STABILITY_WEIGHT,
        )
        all_opps = scorer.score_all(
            filtered_items,
            include_incomplete=show_incomplete,
            history_by_item=history_by_item,
        )
        top = _sort_top(all_opps)[: settings.TOP_N_ITEMS]

        print_opportunities(top)
        if show_incomplete:
            all_scored = scorer.score_all(
                filtered_items, include_incomplete=True, history_by_item=history_by_item
            )
            print_incomplete(all_scored)
        csv_path = export_to_csv(top, "data/output")
        print(f"\nCSV: {csv_path} | DB: {settings.DB_PATH}\n")

    except asyncio.CancelledError:
        print("\nInterrupted.")
    finally:
        for s in sources:
            await s.close()
        await engine.dispose()


async def cmd_collect() -> None:
    engine, sources, raw_repo, market_repo, snapshot_repo, run_repo = await _setup()
    if not sources:
        print("ERROR: No API keys configured.")
        await engine.dispose()
        sys.exit(1)

    interval = settings.COLLECT_INTERVAL_MINUTES * 60
    print(f"\nCollect daemon started.")
    print(f"Interval : {settings.COLLECT_INTERVAL_MINUTES} min")
    print(f"Sources  : {', '.join(s.name for s in sources)}")
    print(f"Items    : {settings.ITEMS_MODE} mode")
    print("Press Ctrl+C to stop.\n")

    shutdown = asyncio.Event()
    pass_num = 0

    try:
        while not shutdown.is_set():
            pass_num += 1
            started = datetime.now(timezone.utc)
            print(f"[{started.strftime('%H:%M:%S')}] Pass #{pass_num} starting…")

            deleted = await raw_repo.cleanup_old(settings.RAW_RETENTION_DAYS)
            if deleted:
                logger.info("Cleaned up %d old raw files", deleted)

            try:
                items = await _get_items(sources)
                prices_by_item, snapshots = await _one_pass(
                    items, sources, raw_repo, market_repo, snapshot_repo, run_repo
                )
                total = sum(len(v) for v in prices_by_item.values())
                elapsed = (datetime.now(timezone.utc) - started).total_seconds()
                print(
                    f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                    f"Pass #{pass_num} done — "
                    f"items: {len(items)}, prices: {total}, "
                    f"snapshots: {snapshots}, elapsed: {elapsed:.0f}s"
                )
            except Exception as exc:
                logger.error("Pass #%d failed: %s", pass_num, exc)

            # Sleep until next interval, wake early on shutdown
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=float(interval))
            except asyncio.TimeoutError:
                pass

    except asyncio.CancelledError:
        print("\nShutdown signal received.")
    finally:
        print("Stopping collect daemon…")
        for s in sources:
            await s.close()
        await engine.dispose()
        print("Done.")


async def cmd_top(show_incomplete: bool = False) -> None:
    print("\n========================================")
    print("  Steam Skin Scraper — top (from DB)")
    print("========================================\n")

    engine, _, _, market_repo, snapshot_repo, _ = await _setup(with_sources=False)
    try:
        prices_by_item = await market_repo.get_latest_by_item()
        if not prices_by_item:
            print("No data in DB. Run 'python main.py collect' first.")
            return

        liquidity = _build_filter()
        filtered_items, summary = liquidity.apply(prices_by_item)
        print(summary.funnel_str(top_n=settings.TOP_N_ITEMS))
        print()

        hist_raw = await snapshot_repo.get_history_bulk(
            list(filtered_items.keys()),
            days=settings.STABILITY_MIN_DAYS * 2 + 7,
        )
        history_by_item = _build_history_analyzer().analyze_bulk(hist_raw)

        scorer = Scorer(
            steam_sell_fee=settings.STEAM_SELL_FEE,
            stability_weight=settings.STABILITY_WEIGHT,
        )
        all_opps = scorer.score_all(
            filtered_items,
            include_incomplete=show_incomplete,
            history_by_item=history_by_item,
        )
        top = _sort_top(all_opps)[: settings.TOP_N_ITEMS]
        print_opportunities(top)
        if show_incomplete:
            all_scored = scorer.score_all(
                filtered_items, include_incomplete=True, history_by_item=history_by_item
            )
            print_incomplete(all_scored)
    finally:
        await engine.dispose()


# ── helpers ───────────────────────────────────────────────────────────────────

def _sort_top(opps: list) -> list:
    """Primary sort: score (desc). Secondary: weighted_ratio (asc, lower = better)."""
    return sorted(
        opps,
        key=lambda o: (o.score, -(o.weighted_ratio if o.weighted_ratio is not None else 999)),
        reverse=True,
    )


def _snapshots_to_prices_by_item(snapshots: list) -> dict[str, list]:
    """Reconstruct a minimal prices_by_item dict from snapshot rows for backtest."""
    from models.market import MarketPrice
    from collections import defaultdict

    # Group by (market_hash_name, source) to create one MarketPrice per (item, source)
    groups: dict[tuple, dict] = defaultdict(lambda: {
        "sell_price": None, "buy_price": None, "steam_buy_order": None,
        "volume_24h": None, "ts": None,
    })
    for s in snapshots:
        key = (s.market_hash_name, s.source)
        g = groups[key]
        if s.market == "steam" and s.price_type == "sell_listing":
            g["sell_price"] = float(s.price)
            g["steam_price"] = float(s.price)
            if s.volume:
                g["volume_24h"] = s.volume
        elif s.market != "steam" and s.price_type == "sell_listing":
            # external marketplace ask (market = buff163/skinport/… or legacy "external")
            if g["buy_price"] is None or float(s.price) < g["buy_price"]:
                g["buy_price"] = float(s.price)
        elif s.market == "steam" and s.price_type == "buy_order":
            g["steam_buy_order"] = float(s.price)
        g["ts"] = max(g["ts"] or s.ts, s.ts)

    by_item: dict[str, list] = {}
    for (name, source), g in groups.items():
        mp = MarketPrice(
            source=source,
            market_hash_name=name,
            buy_price=g.get("buy_price"),
            sell_price=g.get("sell_price"),
            steam_price=g.get("sell_price"),
            steam_buy_order=g.get("steam_buy_order"),
            volume_24h=g.get("volume_24h"),
            updated_at=g["ts"] or datetime.now(timezone.utc),
        )
        by_item.setdefault(name, []).append(mp)
    return by_item


def _avg_sell_price(prices: list) -> float | None:
    vals = [p.sell_price for p in prices if p.sell_price]
    return sum(vals) / len(vals) if vals else None


async def cmd_backtest(days: int, horizon_days: int, top_k: int) -> None:
    from collections import defaultdict

    print(f"\nBacktest: evaluating {days} days of history | horizon {horizon_days}d | top-{top_k} signals\n")

    engine, _, _, _, snapshot_repo, _ = await _setup(with_sources=False)
    try:
        all_snaps = await snapshot_repo.get_all_for_backtest(days + horizon_days)
        if not all_snaps:
            print("No snapshot data found. Run 'python main.py collect' first.")
            return

        # Group by calendar date (UTC)
        by_date: dict = defaultdict(list)
        for snap in all_snaps:
            by_date[snap.ts.date()].append(snap)

        dates = sorted(by_date.keys())
        scorer = Scorer(steam_sell_fee=settings.STEAM_SELL_FEE)
        signal_deltas: list[float] = []
        baseline_deltas: list[float] = []
        evaluated_days = 0

        for eval_date in dates:
            future = [d for d in dates if d.toordinal() >= eval_date.toordinal() + horizon_days]
            if not future:
                continue
            horizon_date = future[0]

            eval_prices = _snapshots_to_prices_by_item(by_date[eval_date])
            if not eval_prices:
                continue

            opps = scorer.score_all(eval_prices, include_incomplete=True)
            if not opps:
                continue

            # Rank exactly like the production top (score desc, weighted_ratio tiebreak)
            opps = _sort_top(opps)
            signal_names = {o.market_hash_name for o in opps[:top_k]}
            all_names = {o.market_hash_name for o in opps}

            horizon_prices = _snapshots_to_prices_by_item(by_date[horizon_date])

            for name in all_names:
                eval_sell = _avg_sell_price(eval_prices.get(name, []))
                horizon_sell = _avg_sell_price(horizon_prices.get(name, []))
                if eval_sell and horizon_sell and eval_sell > 0:
                    delta = (horizon_sell - eval_sell) / eval_sell
                    baseline_deltas.append(delta)
                    if name in signal_names:
                        signal_deltas.append(delta)

            evaluated_days += 1

        if evaluated_days == 0:
            print("Not enough history. Need at least 2× horizon_days of collected data.")
            return

        print(f"Evaluated: {evaluated_days} days\n")

        def _stats(deltas: list[float], label: str, n: int) -> None:
            if not deltas:
                print(f"{label}: no data")
                return
            avg = sum(deltas) / len(deltas)
            mid = sorted(deltas)[len(deltas) // 2]
            wins = sum(1 for d in deltas if d > 0)
            print(f"{label} ({n} signals, {len(deltas)} data points):")
            print(f"  avg delta  {avg * 100:+.2f}%  |  median {mid * 100:+.2f}%  |  win-rate {wins / len(deltas) * 100:.1f}%")

        _stats(signal_deltas, f"Top-{top_k} signals", top_k)
        print()
        _stats(baseline_deltas, "Random baseline (all items)", 0)

    finally:
        await engine.dispose()


async def _bot_collect_loop(
    sources: list,
    raw_repo: RawRepository,
    market_repo: MarketRepository,
    snapshot_repo: PriceSnapshotRepository,
    run_repo: CollectRunRepository,
    notify_cb,
) -> None:
    """Background collect loop used by 'bot --with-collect'."""
    interval = settings.COLLECT_INTERVAL_MINUTES * 60
    pass_num = 0
    try:
        while True:
            pass_num += 1
            items = await _get_items(sources)
            prices_by_item, _ = await _one_pass(
                items, sources, raw_repo, market_repo, snapshot_repo, run_repo
            )
            if prices_by_item:
                lf = _build_filter()
                filtered, _ = lf.apply(prices_by_item)
                scorer = Scorer(
                    steam_sell_fee=settings.STEAM_SELL_FEE,
                    stability_weight=settings.STABILITY_WEIGHT,
                )
                opps = scorer.score_all(filtered)
                opps = _sort_top(opps)
                try:
                    await notify_cb(opps)
                except Exception as exc:
                    logger.error("Notification callback failed: %s", exc)
            try:
                await asyncio.wait_for(asyncio.Event().wait(), timeout=float(interval))
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        pass


async def cmd_bot(with_collect: bool = False) -> None:
    if not settings.TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in .env")
        sys.exit(1)

    allowed_ids = [
        int(x.strip())
        for x in settings.TELEGRAM_ALLOWED_CHAT_IDS.split(",")
        if x.strip().lstrip("-").isdigit()
    ]
    if allowed_ids:
        print(f"Allowed chat IDs: {allowed_ids}")
    else:
        print("WARNING: TELEGRAM_ALLOWED_CHAT_IDS is empty — bot responds to everyone.")

    engine, sf = create_engine_and_session(settings.DB_PATH)
    await init_db(engine)

    from bot.bot import SkinBot

    skin_bot = SkinBot(
        token=settings.TELEGRAM_BOT_TOKEN,
        allowed_chat_ids=allowed_ids,
        session_factory=sf,
        cfg=settings,
    )
    app = skin_bot.build_application()

    sources: list = []
    if with_collect:
        sources = _build_sources()
        if not sources:
            print("WARNING: No API keys configured — collect daemon will be idle.")

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    print(f"\nBot started (@{app.bot.username if hasattr(app.bot, 'username') else '?'})")
    if with_collect:
        print(f"Collect daemon: every {settings.COLLECT_INTERVAL_MINUTES} min")
    print("Press Ctrl+C to stop.\n")

    collect_task = None
    if with_collect:
        raw_repo = RawRepository(sf)
        market_repo = MarketRepository(sf)
        snapshot_repo = PriceSnapshotRepository(sf)
        run_repo = CollectRunRepository(sf)

        async def _notify(opps):
            await skin_bot.send_notifications(app, opps)

        collect_task = asyncio.create_task(
            _bot_collect_loop(sources, raw_repo, market_repo, snapshot_repo, run_repo, _notify)
        )

    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        if collect_task:
            collect_task.cancel()
            try:
                await collect_task
            except asyncio.CancelledError:
                pass
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        for s in sources:
            await s.close()
        await engine.dispose()
        print("Bot stopped.")


# ── CLI ───────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python main.py",
        description="Steam skin price intelligence scraper.",
    )
    subs = p.add_subparsers(dest="command", metavar="COMMAND")

    scan_p = subs.add_parser("scan", help="One-shot: fetch prices, score, print top, save CSV.")
    scan_p.add_argument("--show-incomplete", action="store_true",
                        help="Also show items with no Steam buy order data.")

    subs.add_parser("collect", help="Daemon: collect prices every COLLECT_INTERVAL_MINUTES.")

    top_p = subs.add_parser("top", help="Print top opportunities from DB (no network).")
    top_p.add_argument("--show-incomplete", action="store_true",
                       help="Also show items with no Steam buy order data.")

    bot_p = subs.add_parser("bot", help="Telegram bot (Stage 5).")
    bot_p.add_argument(
        "--with-collect",
        action="store_true",
        help="Also run collect daemon in background (same process).",
    )

    bt_p = subs.add_parser("backtest", help="Backtest scorer against accumulated history.")
    bt_p.add_argument("--days", type=int, default=30,
                      help="How many past days to evaluate (default 30).")
    bt_p.add_argument("--horizon", type=int, default=settings.BACKTEST_HORIZON_DAYS,
                      help="Days ahead to measure price outcome (default BACKTEST_HORIZON_DAYS).")
    bt_p.add_argument("--top-k", type=int, default=10,
                      help="Number of top signals to track per day (default 10).")
    return p


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "scan":
        try:
            asyncio.run(cmd_scan(show_incomplete=getattr(args, "show_incomplete", False)))
        except KeyboardInterrupt:
            print("\nInterrupted.")

    elif args.command == "collect":
        try:
            asyncio.run(cmd_collect())
        except KeyboardInterrupt:
            print("\nInterrupted.")

    elif args.command == "top":
        asyncio.run(cmd_top(show_incomplete=getattr(args, "show_incomplete", False)))

    elif args.command == "bot":
        try:
            asyncio.run(cmd_bot(with_collect=getattr(args, "with_collect", False)))
        except KeyboardInterrupt:
            print("\nInterrupted.")

    elif args.command == "backtest":
        asyncio.run(cmd_backtest(
            days=args.days,
            horizon_days=args.horizon,
            top_k=args.top_k,
        ))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
