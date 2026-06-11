# Steam Skin Scraper — инструкции для Claude Code

## Контекст проекта

Мы делаем MVP-скрипт для анализа CS2/Steam skin market.

Цель первого этапа — не автоматическая торговля, а **агрегатор данных и радар возможностей**.

Скрипт должен:

1. Получать данные из 3 источников:
   - Pricempire
   - SteamWebAPI
   - cs2.sh
2. Приводить данные к единому формату.
3. Сохранять raw-ответы и нормализованные данные.
4. Считать базовую математику: spread, net profit, ROI, ликвидность, риск.
5. Выводить понятный список потенциально интересных предметов.

Не нужно пока делать:

- автоматическую покупку;
- автоматическую продажу;
- работу со Steam Guard;
- управление аккаунтами;
- сложную ML-модель;
- идеальное предсказание очереди buy orders.

---

## Используемые источники данных для MVP

### 1. Pricempire

Документация: `https://pricempire.com/docs`

Назначение:

- агрегированные цены по разным маркетам;
- Steam price;
- данные по другим площадкам;
- исторические данные, если доступны по тарифу.

Пример эндпоинта:

```http
GET https://api.pricempire.com/v4/paid/items/prices
  ?api_token=YOUR_TOKEN
  &source=steam,buff,skinport,csfloat,waxpeer
  &currency=USD
```

Ограничения:

- нужен API token;
- rate limits зависят от тарифа;
- при превышении лимита возможен HTTP 429;
- нельзя зашивать лимиты жёстко в код, лучше вынести в config.

Для MVP использовать осторожный дефолт:

```text
min_delay_seconds = 1.0
cache_ttl_seconds = 300
```

---

### 2. SteamWebAPI

Документация: `https://www.steamwebapi.com/api/doc`

Назначение:

- данные Steam Market;
- данные по предметам CS2;
- агрегированные данные по нескольким маркетам;
- удобный API вместо прямого скрапинга Steam.

Пример эндпоинта:

```http
GET https://www.steamwebapi.com/steam/api/items
  ?key=YOUR_API_KEY
  &game=csgo
  &search=AK-47 Redline
```

Ограничения:

- нужен API key;
- rate limits зависят от тарифа;
- лимит нужно держать в config;
- при HTTP 429 делать backoff и не ронять весь collector.

Для MVP использовать осторожный дефолт:

```text
min_delay_seconds = 1.0
cache_ttl_seconds = 300
```

---

### 3. cs2.sh

Документация / страницы API:

```text
https://cs2.sh/steam
https://cs2.sh/buff
https://cs2.sh/docs/introduction
```

Назначение:

- Steam prices;
- BUFF prices;
- исторические данные;
- быстрый внешний источник для сравнения с другими API.

Особенность:

- данные обновляются не real-time, а примерно раз в несколько минут;
- поэтому нет смысла дергать одни и те же данные чаще, чем раз в 5 минут.

Для MVP использовать дефолт:

```text
min_delay_seconds = 1.0
cache_ttl_seconds = 300
```

---

## Архитектура проекта

Сделать архитектуру через адаптеры источников, чтобы потом легко добавить Buff163, Skinport, DMarket, CSFloat, Waxpeer, BitSkins и другие площадки.

Рекомендуемая структура:

```text
steam_skin_scraper/
  main.py
  config.py

  sources/
    __init__.py
    base.py
    pricempire.py
    steamwebapi.py
    cs2sh.py

  models/
    __init__.py
    market.py

  services/
    __init__.py
    collector.py
    normalizer.py
    scorer.py
    rate_limiter.py

  storage/
    __init__.py
    db.py
    repositories.py

  output/
    __init__.py
    console.py
    csv_exporter.py

  data/
    raw/
    output/
```

---

## Базовый интерфейс источника

Все источники должны реализовывать одинаковый интерфейс.

```python
from abc import ABC, abstractmethod
from typing import Any


class MarketSource(ABC):
    name: str

    @abstractmethod
    async def fetch_prices(self, items: list[str]) -> list[dict[str, Any]]:
        """Fetch current prices for provided market_hash_names."""
        raise NotImplementedError

    async def fetch_history(self, item: str) -> list[dict[str, Any]]:
        """Optional: fetch price/history data for one item."""
        return []
```

Collector не должен знать детали конкретного API. Он должен работать так:

```python
for source in sources:
    raw_data = await source.fetch_prices(items)
    await raw_repository.save(source.name, raw_data)

    normalized_data = normalizer.normalize(source.name, raw_data)
    await market_repository.save_many(normalized_data)
```

---

## Единый формат данных

Нормализованные данные должны приводиться к одному формату.

```python
from dataclasses import dataclass
from datetime import datetime


@dataclass
class MarketPrice:
    source: str
    market_hash_name: str
    currency: str
    buy_price: float | None
    sell_price: float | None
    steam_price: float | None
    volume_24h: int | None
    volume_7d: int | None
    listings_count: int | None
    updated_at: datetime
    raw: dict
```

Поля могут быть `None`, потому что разные источники возвращают разные данные.

---

## Что сохранять

Сохранять два слоя данных.

### 1. Raw data

Сохранять полный ответ источника без изменений.

Зачем:

- можно будет перепарсить позже;
- можно дебажить ошибки нормализации;
- можно сравнить, где источник отдаёт странные данные.

### 2. Normalized data

Сохранять приведённые к единому виду данные, с которыми работает аналитика.

---

## Базовые расчёты

Для Steam продажи использовать грубую комиссию 13%.

```python
STEAM_SELL_FEE = 0.13

net_sell = sell_price * (1 - STEAM_SELL_FEE)
profit = net_sell - buy_price
roi = profit / buy_price
```

Важно:

- комиссию потом можно уточнять по игре/предмету;
- сейчас достаточно грубой оценки;
- если `buy_price` или `sell_price` отсутствует, item нельзя считать полностью.

---

## Базовый скоринг

MVP scoring должен быть простым и объяснимым.

```text
score = roi_score + liquidity_score + stability_score - risk_score
```

Пример логики:

```text
roi_score:
  ROI < 0%       -> 0
  ROI 0-1%       -> 10
  ROI 1-3%       -> 25
  ROI 3-5%       -> 40
  ROI > 5%       -> 60

liquidity_score:
  volume_24h < 10      -> 0
  volume_24h 10-50     -> 10
  volume_24h 50-200    -> 25
  volume_24h > 200     -> 40

risk_score:
  no volume data       -> +30
  no sell price        -> +50
  no buy price         -> +50
  very low volume      -> +30
```

---

## Вывод результата

Для MVP достаточно консоли и CSV.

Пример вывода:

```text
#1 AK-47 | Redline (Field-Tested)
Buy order: 11.40 USD
Expected sell: 13.25 USD
Net sell after fee: 11.53 USD
Profit: 0.13 USD
ROI: 1.14%
Volume 24h: 850
Sources: Pricempire, SteamWebAPI, cs2.sh
Score: 72/100
Reason: positive spread, high volume, acceptable risk
```

CSV поля:

```text
market_hash_name, buy_price, sell_price, net_sell, profit, roi, volume_24h, score, sources, updated_at
```

---

## Rate limiting и retry

Нужен отдельный rate limiter на каждый source.

Минимальная логика:

- не отправлять запросы чаще, чем разрешено в config;
- при HTTP 429 ждать и повторять;
- использовать exponential backoff;
- если один источник упал, остальные продолжают работу.

Пример config:

```python
SOURCE_CONFIG = {
    "pricempire": {
        "min_delay_seconds": 1.0,
        "cache_ttl_seconds": 300,
    },
    "steamwebapi": {
        "min_delay_seconds": 1.0,
        "cache_ttl_seconds": 300,
    },
    "cs2sh": {
        "min_delay_seconds": 1.0,
        "cache_ttl_seconds": 300,
    },
}
```

---

## Правила разработки

1. Не хардкодить API keys в коде.
2. Ключи хранить в `.env`.
3. Все source adapters должны иметь одинаковый интерфейс.
4. Raw data не выбрасывать.
5. Ошибка одного источника не должна ломать весь запуск.
6. Все цены приводить к одной валюте, для MVP — USD.
7. Основной идентификатор предмета — `market_hash_name`.
8. Не делать автоматическую торговлю на этом этапе.
9. Не скрапить Steam напрямую на этом этапе.
10. Сначала сделать понятный MVP, потом расширять источники.

---

## Первый milestone

Сделать скрипт, который:

1. Загружает список `market_hash_name` из файла.
2. Получает данные из Pricempire, SteamWebAPI и cs2.sh.
3. Сохраняет raw responses.
4. Нормализует данные.
5. Считает spread, net profit, ROI и score.
6. Выводит top opportunities в консоль.
7. Экспортирует результат в CSV.

---

## Второй milestone

После первого рабочего MVP:

1. Добавить SQLite/PostgreSQL.
2. Начать сохранять snapshots по времени.
3. Сравнивать изменения цены.
4. Добавить простую оценку стабильности цены.
5. Добавить Telegram alert.

---

## Что считать успехом MVP

MVP успешен, если он может ответить:

```text
Какие предметы прямо сейчас выглядят интересными по spread, ROI и ликвидности?
```

И показать не просто цифру, а объяснение:

```text
Почему этот предмет попал в топ?
Какие данные использовались?
Какие риски есть?
```
