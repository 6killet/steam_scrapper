# Steam Skin Scraper

Read-only система price intelligence для скинов CS2: собирает цены из трёх API
(Pricempire, SteamWebAPI, cs2.sh), копит историю в SQLite, считает честный ROI
с учётом комиссии Steam и выдаёт сигналы арбитража — в консоль, CSV и Telegram.

Система **не** покупает, **не** продаёт и **не** логинится в Steam-аккаунты.

## Установка

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (Linux/macOS: source .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env          # и заполнить API-ключи
```

Для тестов дополнительно: `pip install pytest`, запуск — `python -m pytest tests/ -q`.

## Конфигурация (.env)

Все параметры описаны с комментариями в [.env.example](.env.example). Минимум для старта —
хотя бы один API-ключ:

| Переменная | Где взять |
|---|---|
| `PRICEMPIRE_API_TOKEN` | https://pricempire.com/api |
| `STEAMWEBAPI_KEY` | https://www.steamwebapi.com |
| `CS2SH_API_KEY` | https://cs2.sh (3-дневный бесплатный ключ) |

Ключевые настройки: `COLLECT_INTERVAL_MINUTES` (период сбора, по умолчанию 30),
`ITEMS_MODE` (`file` — список из items.txt; `universe` — весь рынок CS2 из Pricempire
или cs2.sh с фильтром по цене), пороги фильтров (`MIN_VOLUME_24H`, `MIN_SOURCES`,
`MAX_SOURCE_DIVERGENCE_PCT`, `MIN_BUY_ORDER_QTY`), Telegram (`TELEGRAM_BOT_TOKEN`,
`TELEGRAM_ALLOWED_CHAT_IDS`).

## CLI-режимы

```bash
python main.py scan                  # один проход: сбор → фильтры → скоринг → топ + CSV
python main.py collect               # демон: сбор каждые COLLECT_INTERVAL_MINUTES, история в БД
python main.py top                   # топ из накопленной БД, без сети
python main.py bot                   # Telegram-бот
python main.py bot --with-collect    # бот + фоновый сбор в одном процессе
python main.py backtest --days 30 --horizon 7 --top-k 10   # валидация скоринга на истории
```

`scan`/`top` принимают `--show-incomplete` — показать также предметы без данных
по Steam buy order (секция «нет данных по выкупу»).

## Как считается ROI

Комиссия Steam считается точно, floor-арифметикой как у самого Steam
([services/steam_fee.py](services/steam_fee.py)): 10% game fee + 5% steam fee,
каждая округляется вниз до цента, минимум $0.01; для цен ≤ $0.66 — точная
таблица соответствий.

Две метрики на предмет:

- **roi_realistic** — купить на внешнем маркете, продать мгновенно в Steam buy order:
  `(subtract_fee(steam_buy_order) − внешняя_цена) / внешняя_цена`. Идёт в score и сортировку.
- **roi_optimistic** — выставить листингом и ждать покупателя:
  `(subtract_fee(steam_sell_listing) − внешняя_цена) / внешняя_цена`. Справочная.

Предмет без buy order ни в одном источнике помечается incomplete и в топ не попадает.

## Формула score (0–100)

```
score = roi_score + liquidity_score − risk_penalty   (+ STABILITY_WEIGHT, если предмет стабилен)
```

| Компонент | Шкала |
|---|---|
| roi_score (по roi_realistic) | <0% → 0 · 0–1% → 10 · 1–3% → 25 · 3–5% → 40 · >5% → 60 |
| liquidity_score (объём 24ч) | <10 → 0 · 10–49 → 10 · 50–199 → 25 · ≥200 → 40 |
| risk_penalty | нет объёма или <5 → +30 · нет sell listing → +50 · нет внешней цены → +50 · нет buy order → +20 |

Вторичный ключ сортировки — `weighted_ratio`
(`buy_ratio·0.4 + sell_ratio·0.2 + trans_ratio·0.4`, меньше = лучше).

При ≥ `STABILITY_MIN_DAYS` (7) днях истории по предмету включается анализ стабильности
(скользящие гармонические средние, 4 условия) и рекомендация цены листинга
(перцентиль продаж за 7 дней: P50 для предметов < $100, P20 для дорогих).

## Схема БД (SQLite, data/scraper.db)

| Таблица | Назначение |
|---|---|
| `price_snapshots` | история цен: ts, предмет, source, market (steam/buff163/…), price_type (sell_listing/buy_order/avg_24h), price, volume |
| `market_prices` | последние нормализованные цены по (source, предмет) — из них строится `top` |
| `collect_runs` | журнал проходов: время, кол-во предметов/снапшотов, ошибки |
| `raw_responses` | сырые ответы API (ротация: `RAW_RETENTION_DAYS`, дублируются в `data/raw/*.json`) |
| `sell_history_points` | точки истории продаж (если API отдаёт) |
| `bot_settings`, `notifications_sent`, `bot_mutes` | per-chat настройки бота, антиспам, mute-список |

Миграций (alembic) нет: `init_db` создаёт недостающие таблицы и дописывает
недостающие колонки в существующие. Если что-то сломалось после обновления —
БД MVP можно просто пересоздать: удалить `data/*.db` (история потеряется).

## Telegram-бот

`/start` — меню · `/top` — топ-5 карточками с пагинацией · `/filters` — пороги
per-chat (min ROI, min объём) · `/status` — последний проход, размер БД ·
`/item <часть названия>` — поиск.

После каждого прохода `collect` (в режиме `--with-collect`) предметы, прошедшие
пороги чата, рассылаются уведомлениями. Антиспам: не чаще
`NOTIFY_COOLDOWN_MINUTES` по предмету, и повторно — только если ROI вырос ≥ 2 п.п.;
не более `NOTIFY_MAX_PER_RUN` за проход; кнопка «🔕» добавляет предмет в mute.
Чаты не из `TELEGRAM_ALLOWED_CHAT_IDS` игнорируются (пустой список = открыт всем).

## Структура проекта

```
sources/    адаптеры API (pricempire, steamwebapi, cs2sh) + rate limiting/backoff
services/   collector, normalizer, filters, scorer, steam_fee, history_analyzer
models/     pydantic-модели (MarketPrice, ScoredOpportunity)
storage/    SQLAlchemy-модели и репозитории
output/     консольный вывод и CSV-экспорт
bot/        Telegram-бот
tests/      pytest; fixtures/ — сохранённые примеры ответов API
```
