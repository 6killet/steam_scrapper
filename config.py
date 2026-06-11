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
