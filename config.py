from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # API keys
    PRICEMPIRE_API_TOKEN: str = ""
    STEAMWEBAPI_KEY: str = ""
    CS2SH_API_KEY: str = ""

    # App
    ITEMS_FILE: str = "items.txt"
    DB_PATH: str = "data/scraper.db"
    TOP_N_ITEMS: int = 20

    # Steam sell fee (13% gross commission)
    STEAM_SELL_FEE: float = 0.13

    # Per-source rate limiting
    PRICEMPIRE_MIN_DELAY: float = 1.0
    PRICEMPIRE_CACHE_TTL: int = 300

    STEAMWEBAPI_MIN_DELAY: float = 1.0
    STEAMWEBAPI_CACHE_TTL: int = 300

    CS2SH_MIN_DELAY: float = 1.0
    CS2SH_CACHE_TTL: int = 300

    # Retry settings
    MAX_RETRIES: int = 3
    RETRY_BASE_DELAY: float = 2.0

    # Collect daemon
    COLLECT_INTERVAL_MINUTES: int = 30
    RAW_RETENTION_DAYS: int = 14

    # Items universe (ITEMS_MODE=file uses items.txt; ITEMS_MODE=universe fetches from Pricempire)
    ITEMS_MODE: str = "file"
    MIN_PRICE_USD: float = 1.0
    MAX_PRICE_USD: float = 500.0
    UNIVERSE_MAX_ITEMS: int = 2000

    # Liquidity filters (Stage 3)
    MIN_VOLUME_24H: int = 10
    MIN_BUY_ORDER_QTY: int = 3
    MIN_SOURCES: int = 2
    MAX_SOURCE_DIVERGENCE_PCT: float = 15.0

    # Historical analytics (Stage 4)
    STABILITY_MIN_DAYS: int = 7
    STABILITY_WEIGHT: int = 10
    BACKTEST_HORIZON_DAYS: int = 7

    # Telegram bot (Stage 5)
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_ALLOWED_CHAT_IDS: str = ""   # comma-separated int IDs; empty = allow all
    NOTIFY_COOLDOWN_MINUTES: int = 360
    NOTIFY_MAX_PER_RUN: int = 5
    BOT_TOP_N: int = 5                    # items per page in /top


settings = Settings()

SOURCE_CONFIG: dict[str, dict] = {
    "pricempire": {
        "min_delay_seconds": settings.PRICEMPIRE_MIN_DELAY,
        "cache_ttl_seconds": settings.PRICEMPIRE_CACHE_TTL,
    },
    "steamwebapi": {
        "min_delay_seconds": settings.STEAMWEBAPI_MIN_DELAY,
        "cache_ttl_seconds": settings.STEAMWEBAPI_CACHE_TTL,
    },
    "cs2sh": {
        "min_delay_seconds": settings.CS2SH_MIN_DELAY,
        "cache_ttl_seconds": settings.CS2SH_CACHE_TTL,
    },
}
