"""Exports ScoredOpportunity list to a timestamped CSV file."""

import csv
import logging
import os
from datetime import datetime
from pathlib import Path

from models.market import ScoredOpportunity

logger = logging.getLogger(__name__)

_FIELDS = [
    "market_hash_name",
    "buy_price",
    "sell_price",
    "net_sell",
    "profit",
    "roi",
    "volume_24h",
    "score",
    "sources",
    "updated_at",
]


def export_to_csv(opportunities: list[ScoredOpportunity], out_dir: str) -> str:
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(out_dir, f"opportunities_{ts}.csv")

    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_FIELDS)
        writer.writeheader()
        for opp in opportunities:
            writer.writerow({
                "market_hash_name": opp.market_hash_name,
                "buy_price":  _f4(opp.buy_price),
                "sell_price": _f4(opp.sell_price),
                "net_sell":   _f4(opp.net_sell),
                "profit":     _f4(opp.profit),
                "roi":        f"{opp.roi * 100:.4f}%" if opp.roi is not None else "",
                "volume_24h": opp.volume_24h if opp.volume_24h is not None else "",
                "score":      opp.score,
                "sources":    ",".join(opp.sources),
                "updated_at": opp.updated_at.isoformat(),
            })

    logger.info("CSV exported: %s (%d rows)", path, len(opportunities))
    return path


def _f4(v: float | None) -> str:
    return f"{v:.4f}" if v is not None else ""
