from abc import ABC, abstractmethod
from typing import Any


class MarketSource(ABC):
    name: str

    @abstractmethod
    async def fetch_prices(self, items: list[str]) -> list[dict[str, Any]]:
        """Fetch current prices for the given market_hash_names.

        Returns a list of raw dicts — one entry per item found.
        Missing items are silently omitted.
        """
        raise NotImplementedError

    async def fetch_history(self, item: str) -> list[dict[str, Any]]:
        """Optional: fetch price history for one item. Returns [] by default."""
        return []

    async def close(self) -> None:
        """Release any held resources (HTTP sessions, etc.)."""
