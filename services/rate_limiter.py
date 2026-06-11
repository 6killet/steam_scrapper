import asyncio
import time
import logging

logger = logging.getLogger(__name__)


class RateLimiter:
    def __init__(self, name: str, min_delay: float) -> None:
        self.name = name
        self.min_delay = min_delay
        self._last_call: float = 0.0
        self._lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def acquire(self) -> None:
        async with self._get_lock():
            now = time.monotonic()
            elapsed = now - self._last_call
            if elapsed < self.min_delay:
                wait = self.min_delay - elapsed
                logger.debug("[%s] Rate limit: waiting %.2fs", self.name, wait)
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()
