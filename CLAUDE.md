# Steam Skin Scraper — заметки для Claude

Read-only price intelligence для скинов CS2. Полное описание — в [README.md](README.md),
ТЗ — в [TZ_steam_scrapper.md](TZ_steam_scrapper.md). Все 5 этапов ТЗ реализованы.

## Жёсткие ограничения (из ТЗ, не нарушать)

- Источники — только три API: Pricempire, SteamWebAPI, cs2.sh. Никакого прямого
  скрейпинга steamcommunity.com, прокси-ротации, user-agent пулов.
- Никаких Redis/RabbitMQ/Celery/Docker/MongoDB. Хранилище — SQLite,
  планировщик — asyncio-цикл в одном процессе.
- Никакой покупки/продажи/логина в Steam.
- Слоистая архитектура sources / services / models / storage / output — новые
  модули добавлять в существующие слои.
- Денежные расчёты комиссии — `Decimal` (services/steam_fee.py); тяжёлые
  зависимости (pandas/numpy) не вводить — статистика на stdlib.
- Новые параметры конфига — в config.py (pydantic settings) И в .env.example
  с комментарием.

## Поток данных

```
sources → Collector → Normalizer (MarketPrice) → MarketRepository (последние цены)
                                               → PriceSnapshotRepository (история)
MarketPrice'ы → LiquidityFilter → Scorer (+HistoryAnalyzer) → ScoredOpportunity
→ console/CSV/Telegram
```

- `roi_realistic` (через Steam buy order) — основная метрика, идёт в score;
  `roi_optimistic` (через sell listing) — справочная.
- `buy_market` в MarketPrice — маркетплейс лучшей внешней цены (buff163/skinport/…),
  не путать с `source` (API-провайдер). SteamWebAPI маркетплейс не сообщает → None.
- В `price_snapshots.market` пишется имя маркетплейса (или "external", если неизвестен);
  steam-цены всегда `market="steam"`.

## БД и миграции

Alembic нет. `init_db` (storage/db.py) делает `create_all` + дописывает недостающие
колонки через `_MIGRATION_COLUMNS` (PRAGMA table_info + ALTER TABLE ADD COLUMN).
Добавляешь колонку в модель — добавь её и в `_MIGRATION_COLUMNS`.
`market_prices` обязана хранить `steam_buy_order`/`buy_order_qty`/`buy_market` —
иначе `top` и бот (читают из БД) теряют roi_realistic.

## Тесты

`python -m pytest tests/ -q` — должно быть зелёным после любого изменения.
Сетевых вызовов в тестах нет; парсинг источников тестируется на фикстурах
в `tests/fixtures/*.json` — при изменении нормализатора обновляй фикстуры.
Грид round-trip тестов комиссии (test_steam_fee.py) трогать осторожно:
floor-арифметика Steam контринтуитивна, гэпы до $0.66 — норма.

## Прочее

- `.env` содержит реальные ключи — никогда не коммитить и не копировать его
  содержимое в .env.example.
- Windows-окружение, пути с кириллицей («d:\РОМАН\…») — в коде использовать
  pathlib, читать/писать файлы с `encoding="utf-8"`.
