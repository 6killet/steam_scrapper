"""Steam Skin Scraper — entry point.

Usage:
    python main.py

Reads item names from items.txt (one market_hash_name per line).
Fetches prices from enabled sources, normalises, scores, and prints results.
Results are also saved to data/output/<timestamp>.csv and to SQLite.
"""

import asyncio
import logging
import sys
from pathlib import Path

from config import settings
from models.market import ScoredOpportunity
from output.console import print_opportunities
from output.csv_exporter import export_to_csv
from services.collector import Collector
from services.normalizer import Normalizer
from services.scorer import Scorer
from sources.cs2sh import CS2SHSource
from sources.pricempire import PricempireSource
from sources.steamwebapi import SteamWebAPISource
from storage.db import create_engine_and_session, init_db
from storage.repositories import MarketRepository, RawRepository

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)-8s]  %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Default items.txt content (written only if the file is missing)
# ------------------------------------------------------------------
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


def _load_items(path: str) -> list[str]:
    p = Path(path)
    if not p.exists():
        logger.info("items.txt not found — writing example list to %s", p)
        p.write_text(_EXAMPLE_ITEMS, encoding="utf-8")

    items = [
        line.strip()
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    return items


def _build_sources():
    sources = []
    warnings = []

    if settings.PRICEMPIRE_API_TOKEN:
        sources.append(PricempireSource())
    else:
        warnings.append("PRICEMPIRE_API_TOKEN not set — Pricempire skipped")

    if settings.STEAMWEBAPI_KEY:
        sources.append(SteamWebAPISource())
    else:
        warnings.append("STEAMWEBAPI_KEY not set  — SteamWebAPI skipped")

    if settings.CS2SH_API_KEY:
        sources.append(CS2SHSource())
    else:
        warnings.append("CS2SH_API_KEY not set    — cs2.sh skipped")

    for w in warnings:
        print(f"  WARNING: {w}")

    return sources


async def run() -> None:
    print("\n========================================")
    print("  Steam Skin Scraper — MVP")
    print("========================================\n")

    # Load items
    items = _load_items(settings.ITEMS_FILE)
    if not items:
        print("ERROR: items.txt is empty. Add market_hash_names and re-run.")
        sys.exit(1)
    print(f"Items loaded: {len(items)}")

    # Build sources
    sources = _build_sources()
    if not sources:
        print(
            "\nERROR: No API keys configured.\n"
            "Copy .env.example to .env and fill in at least one API key."
        )
        sys.exit(1)
    print(f"Active sources: {', '.join(s.name for s in sources)}\n")

    # Storage
    engine, session_factory = create_engine_and_session(settings.DB_PATH)
    await init_db(engine)

    raw_repo = RawRepository(session_factory)
    market_repo = MarketRepository(session_factory)

    # Collect
    normalizer = Normalizer()
    collector = Collector(sources, raw_repo, market_repo, normalizer)

    print("Fetching prices…")
    prices_by_item = await collector.collect(items)

    total_prices = sum(len(v) for v in prices_by_item.values())
    if total_prices == 0:
        print("\nNo price data returned. Check API keys and network connectivity.")
        return

    items_with_data = len(prices_by_item)
    print(f"Received data: {total_prices} price records for {items_with_data} items.\n")

    # Score
    scorer = Scorer(steam_sell_fee=settings.STEAM_SELL_FEE)
    opportunities: list[ScoredOpportunity] = scorer.score_all(prices_by_item)
    opportunities.sort(key=lambda o: o.score, reverse=True)
    top = opportunities[: settings.TOP_N_ITEMS]

    # Output
    print_opportunities(top)

    csv_path = export_to_csv(top, "data/output")
    print(f"\nCSV saved: {csv_path}")
    print(f"DB  saved: {settings.DB_PATH}\n")

    # Cleanup
    for src in sources:
        await src.close()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
