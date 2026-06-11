"""Telegram bot for the Steam skin arbitrage scraper.

Commands:
  /start            — welcome + main menu
  /top              — top opportunities (paginated, 5 per page)
  /filters          — per-chat filter settings
  /status           — collect daemon status
  /item <query>     — search items by name substring

Inline buttons handle pagination, detail cards, filter changes,
muting items, and search results.
"""

import hashlib
import html
import logging
from datetime import datetime, timezone
from urllib.parse import quote

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import Settings
from models.market import ScoredOpportunity
from services.filters import LiquidityFilter
from services.history_analyzer import HistoryAnalyzer
from services.scorer import Scorer
from storage.db import MarketPriceDB
from storage.repositories import (
    BotMuteRepository,
    BotSettingsRepository,
    CollectRunRepository,
    MarketRepository,
    NotificationsSentRepository,
    PriceSnapshotRepository,
)

logger = logging.getLogger(__name__)

_ROI_OPTIONS = [5, 10, 15, 20]
_VOL_OPTIONS = [5, 10, 25, 50]


# ── helpers ────────────────────────────────────────────────────────────────────

def _key(name: str) -> str:
    """10-char stable hash used in callback_data to reference item names."""
    return hashlib.md5(name.encode()).hexdigest()[:10]


def _esc(s: str) -> str:
    return html.escape(str(s))


def _fmt_usd(v: float | None) -> str:
    return f"${v:.2f}" if v is not None else "N/A"


def _fmt_roi(v: float | None) -> str:
    if v is None:
        return "N/A"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v * 100:.1f}%"


def _steam_url(name: str) -> str:
    return f"https://steamcommunity.com/market/listings/730/{quote(name)}"


def _format_card(opp: ScoredOpportunity, detail: bool = False) -> str:
    """HTML-formatted item card for Telegram."""
    buy_mkt = _esc((opp.buy_market or "ext").upper()[:8])
    roi_r = _fmt_roi(opp.roi_realistic)
    roi_o = _fmt_roi(opp.roi_optimistic)
    profit_str = _fmt_usd(opp.net_profit_usd)

    stab_str = "—"
    if opp.is_stable is True:
        cv = f" CV {opp.stability_cv * 100:.1f}%" if opp.stability_cv else ""
        stab_str = f"✅{cv}"
    elif opp.is_stable is False:
        cv = f" CV {opp.stability_cv * 100:.1f}%" if opp.stability_cv else ""
        stab_str = f"❌{cv}"
    elif opp.history_insufficient:
        stab_str = "📊 &lt;7d"

    lines = [
        f"🔫 <b>{_esc(opp.market_hash_name)}</b>",
        f"Купить: {buy_mkt} {_fmt_usd(opp.buy_price)} → BO {_fmt_usd(opp.steam_buy_order)}",
        f"ROI: {roi_r} реал. / {roi_o} оптим. | Профит: {profit_str}",
        f"Объём 24ч: {opp.volume_24h or 'N/A'} | Стаб: {stab_str}",
    ]

    if opp.suggested_sell_price is not None:
        ref = opp.steam_buy_order or opp.steam_sell_listing or 0
        pct_label = "P50" if ref < 100 else "P20"
        lines.append(f"Выставлять по: {_fmt_usd(opp.suggested_sell_price)} ({pct_label})")

    if detail:
        lines.append(f"Steam listing: {_fmt_usd(opp.steam_sell_listing)}")
        lines.append(f"Источники: {_esc(', '.join(opp.sources)) if opp.sources else '—'}")
        lines.append(f"Score: {opp.score}/100")
        flags = []
        if opp.incomplete:
            flags.append("⚠️ нет buy order")
        if opp.qty_unknown:
            flags.append("⚠️ qty неизв.")
        if flags:
            lines.append(" ".join(flags))

    return "\n".join(lines)


def _main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Топ", callback_data="top"),
            InlineKeyboardButton("⚙️ Фильтры", callback_data="fl"),
        ],
        [
            InlineKeyboardButton("📈 Статус", callback_data="st"),
            InlineKeyboardButton("🔍 Поиск", callback_data="sr"),
        ],
    ])


# ── SkinBot ────────────────────────────────────────────────────────────────────

class SkinBot:
    def __init__(
        self,
        token: str,
        allowed_chat_ids: list[int],
        session_factory,
        cfg: Settings,
    ) -> None:
        self.token = token
        self.allowed_ids = set(allowed_chat_ids)
        self._factory = session_factory
        self._cfg = cfg

        self._market_repo = MarketRepository(session_factory)
        self._snapshot_repo = PriceSnapshotRepository(session_factory)
        self._run_repo = CollectRunRepository(session_factory)
        self._bot_cfg_repo = BotSettingsRepository(session_factory)
        self._notif_repo = NotificationsSentRepository(session_factory)
        self._mute_repo = BotMuteRepository(session_factory)

    # ── build ────────────────────────────────────────────────────────────────

    def build_application(self) -> Application:
        app = Application.builder().token(self.token).build()
        app.add_handler(CommandHandler("start", self._start))
        app.add_handler(CommandHandler("top", self._cmd_top))
        app.add_handler(CommandHandler("filters", self._cmd_filters))
        app.add_handler(CommandHandler("status", self._cmd_status))
        app.add_handler(CommandHandler("item", self._cmd_item))
        app.add_handler(CallbackQueryHandler(self._handle_callback))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text))
        return app

    # ── auth ────────────────────────────────────────────────────────────────

    def _allowed(self, update: Update) -> bool:
        if not self.allowed_ids:
            return True
        cid = update.effective_chat.id if update.effective_chat else None
        return cid in self.allowed_ids

    # ── command handlers ─────────────────────────────────────────────────────

    async def _start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        await update.message.reply_text(
            "👋 Steam CS2 арбитраж-скраппер\n\nВыберите действие:",
            reply_markup=_main_menu(),
        )

    async def _cmd_top(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        await self._show_top(update, context, page=0, refresh=True)

    async def _cmd_filters(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        await self._show_filters(update, context)

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        await self._show_status(update, context)

    async def _cmd_item(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        query = " ".join(context.args or []).strip()
        if not query:
            await update.message.reply_text("Укажите часть названия: /item AK-47")
            return
        await self._do_search(update, context, query)

    async def _handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        if context.user_data.pop("waiting_search", False):
            await self._do_search(update, context, update.message.text)

    # ── callback router ──────────────────────────────────────────────────────

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        q = update.callback_query
        await q.answer()
        data = q.data or ""

        if data == "top":
            await self._show_top(update, context, page=0, refresh=False)
        elif data == "top_refresh":
            await self._show_top(update, context, page=0, refresh=True)
        elif data.startswith("tp:"):
            await self._show_top(update, context, page=int(data[3:]))
        elif data.startswith("dt:"):
            await self._show_detail(update, context, data[3:])
        elif data.startswith("mu:"):
            await self._do_mute(update, context, data[3:])
        elif data in ("fl", "filters"):
            await self._show_filters(update, context)
        elif data.startswith("fr:"):
            await self._set_roi(update, context, int(data[3:]))
        elif data.startswith("fv:"):
            await self._set_vol(update, context, int(data[3:]))
        elif data == "st":
            await self._show_status(update, context)
        elif data == "sr":
            context.user_data["waiting_search"] = True
            await self._send_or_edit(
                update,
                "🔍 Введите часть названия предмета:",
                InlineKeyboardMarkup([[InlineKeyboardButton("« Меню", callback_data="menu")]]),
            )
        elif data == "menu":
            await self._send_or_edit(update, "Выберите действие:", _main_menu())
        elif data == "noop":
            pass
        else:
            logger.debug("Unknown callback_data: %s", data)

    # ── top list ─────────────────────────────────────────────────────────────

    async def _load_top(self, chat_id: int) -> list[ScoredOpportunity]:
        prices_by_item = await self._market_repo.get_latest_by_item()
        if not prices_by_item:
            return []

        chat_cfg = await self._bot_cfg_repo.get_or_create(chat_id)
        lf = LiquidityFilter(
            min_volume_24h=chat_cfg.min_volume_24h,
            min_price_usd=chat_cfg.min_price_usd,
            max_price_usd=chat_cfg.max_price_usd,
            min_sources=self._cfg.MIN_SOURCES,
            max_source_divergence_pct=self._cfg.MAX_SOURCE_DIVERGENCE_PCT,
            min_buy_order_qty=self._cfg.MIN_BUY_ORDER_QTY,
        )
        filtered, _ = lf.apply(prices_by_item)

        hist_raw = await self._snapshot_repo.get_history_bulk(
            list(filtered.keys()),
            days=self._cfg.STABILITY_MIN_DAYS * 2 + 7,
        )
        hist = HistoryAnalyzer(min_days=self._cfg.STABILITY_MIN_DAYS).analyze_bulk(hist_raw)

        scorer = Scorer(
            steam_sell_fee=self._cfg.STEAM_SELL_FEE,
            stability_weight=self._cfg.STABILITY_WEIGHT,
        )
        opps = scorer.score_all(filtered, history_by_item=hist)

        if chat_cfg.min_roi_pct > 0:
            opps = [o for o in opps if o.roi_realistic is not None and o.roi_realistic * 100 >= chat_cfg.min_roi_pct]

        return sorted(opps, key=lambda o: (-o.score, o.weighted_ratio or 999))

    async def _show_top(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        page: int = 0,
        refresh: bool = False,
    ) -> None:
        chat_id = update.effective_chat.id

        if refresh or "top_opps" not in context.chat_data:
            opps = await self._load_top(chat_id)
            context.chat_data["top_opps"] = opps

        opps: list[ScoredOpportunity] = context.chat_data.get("top_opps", [])
        if not opps:
            text = (
                "📭 Нет данных или все отфильтрованы.\n\n"
                "Запустите <code>python main.py collect</code> или смягчите фильтры /filters"
            )
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Обновить", callback_data="top_refresh"),
                InlineKeyboardButton("⚙️ Фильтры", callback_data="fl"),
            ]])
            await self._send_or_edit(update, text, kb)
            return

        per_page = self._cfg.BOT_TOP_N
        total_pages = max(1, (len(opps) + per_page - 1) // per_page)
        page = max(0, min(page, total_pages - 1))
        context.chat_data["top_page"] = page
        start = page * per_page
        page_opps = opps[start: start + per_page]

        imap: dict = context.bot_data.setdefault("item_map", {})
        for opp in page_opps:
            imap[_key(opp.market_hash_name)] = opp.market_hash_name

        parts = []
        for idx, opp in enumerate(page_opps, start=start + 1):
            parts.append(f"<b>#{idx}</b>\n{_format_card(opp)}")
        text = "\n\n──────────\n\n".join(parts)

        # Detail + Steam buttons per item
        item_rows = []
        for opp in page_opps:
            k = _key(opp.market_hash_name)
            short = _esc(opp.market_hash_name[:28])
            item_rows.append([
                InlineKeyboardButton(f"🔍 {short}", callback_data=f"dt:{k}"),
                InlineKeyboardButton("Steam ↗", url=_steam_url(opp.market_hash_name)),
            ])

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"tp:{page - 1}"))
        nav.append(InlineKeyboardButton(f"стр {page + 1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"tp:{page + 1}"))

        all_rows = item_rows
        if nav:
            all_rows = all_rows + [nav]
        all_rows += [
            [InlineKeyboardButton("🔄 Обновить", callback_data="top_refresh")],
            [InlineKeyboardButton("« Меню", callback_data="menu")],
        ]
        await self._send_or_edit(update, text, InlineKeyboardMarkup(all_rows))

    # ── detail card ──────────────────────────────────────────────────────────

    async def _show_detail(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, item_key: str
    ) -> None:
        name = context.bot_data.get("item_map", {}).get(item_key)
        if not name:
            await update.callback_query.answer(
                "Устаревший кэш — обновите /top", show_alert=True
            )
            return

        opps: list[ScoredOpportunity] = context.chat_data.get("top_opps", [])
        opp = next((o for o in opps if o.market_hash_name == name), None)

        if opp is None:
            # Fallback: score fresh from DB
            prices_by_item = await self._market_repo.get_latest_by_item()
            prices = prices_by_item.get(name)
            if not prices:
                await update.callback_query.answer("Нет данных по предмету", show_alert=True)
                return
            scorer = Scorer(steam_sell_fee=self._cfg.STEAM_SELL_FEE)
            scored = scorer.score_all({name: prices}, include_incomplete=True)
            opp = scored[0] if scored else None

        if opp is None:
            await update.callback_query.answer("Ошибка загрузки", show_alert=True)
            return

        text = f"<b>📋 Подробнее</b>\n\n{_format_card(opp, detail=True)}"
        k = _key(name)
        prev_page = context.chat_data.get("top_page", 0)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Steam ↗", url=_steam_url(name))],
            [
                InlineKeyboardButton("🔕 Не присылать", callback_data=f"mu:{k}"),
                InlineKeyboardButton("« Назад", callback_data=f"tp:{prev_page}"),
            ],
        ])
        await self._send_or_edit(update, text, kb)

    # ── mute ────────────────────────────────────────────────────────────────

    async def _do_mute(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, item_key: str
    ) -> None:
        name = context.bot_data.get("item_map", {}).get(item_key)
        if not name:
            await update.callback_query.answer("Предмет не найден", show_alert=True)
            return
        await self._mute_repo.mute(update.effective_chat.id, name)
        short = name[:35]
        await update.callback_query.answer(f"🔕 «{short}» добавлен в игнор", show_alert=True)

    # ── filters ──────────────────────────────────────────────────────────────

    async def _show_filters(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        chat_id = update.effective_chat.id
        cfg = await self._bot_cfg_repo.get_or_create(chat_id)

        text = (
            f"⚙️ <b>Фильтры для этого чата</b>\n\n"
            f"Min ROI: <b>{cfg.min_roi_pct:.0f}%</b>\n"
            f"Min объём 24ч: <b>{cfg.min_volume_24h}</b>\n"
            f"Диапазон цен: <b>${cfg.min_price_usd:.0f} – ${cfg.max_price_usd:.0f}</b>"
        )
        roi_row = [
            InlineKeyboardButton(
                f"{'✅ ' if cfg.min_roi_pct == v else ''}{v}%",
                callback_data=f"fr:{v}",
            )
            for v in _ROI_OPTIONS
        ]
        vol_row = [
            InlineKeyboardButton(
                f"{'✅ ' if cfg.min_volume_24h == v else ''}{v}",
                callback_data=f"fv:{v}",
            )
            for v in _VOL_OPTIONS
        ]
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📈 Min ROI", callback_data="noop")],
            roi_row,
            [InlineKeyboardButton("📊 Min объём 24ч", callback_data="noop")],
            vol_row,
            [InlineKeyboardButton("« Меню", callback_data="menu")],
        ])
        await self._send_or_edit(update, text, kb)

    async def _set_roi(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, value: int
    ) -> None:
        await self._bot_cfg_repo.update(update.effective_chat.id, min_roi_pct=float(value))
        context.chat_data.pop("top_opps", None)  # invalidate cached top
        await self._show_filters(update, context)

    async def _set_vol(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, value: int
    ) -> None:
        await self._bot_cfg_repo.update(update.effective_chat.id, min_volume_24h=value)
        context.chat_data.pop("top_opps", None)
        await self._show_filters(update, context)

    # ── status ───────────────────────────────────────────────────────────────

    async def _show_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        from pathlib import Path

        db_path = Path(self._cfg.DB_PATH)
        db_kb = db_path.stat().st_size // 1024 if db_path.exists() else 0
        run = await self._run_repo.get_last()

        if run:
            ts = run.finished_at or run.started_at
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            secs = (datetime.now(timezone.utc) - ts).total_seconds()
            age = f"{int(secs // 60)}м назад" if secs < 3600 else f"{int(secs // 3600)}ч назад"
            errs = len(run.errors_json or [])
            text = (
                f"📈 <b>Статус</b>\n\n"
                f"Последний сбор: <b>{age}</b>\n"
                f"Предметов: {run.items_requested or 0} | "
                f"Снапшотов: {run.prices_saved or 0}\n"
                f"Ошибок: {errs} | БД: {db_kb} KB"
            )
        else:
            text = f"📈 <b>Статус</b>\n\nСбор ещё не запускался.\nБД: {db_kb} KB"

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄", callback_data="st"),
            InlineKeyboardButton("« Меню", callback_data="menu"),
        ]])
        await self._send_or_edit(update, text, kb)

    # ── search ───────────────────────────────────────────────────────────────

    async def _do_search(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, query: str
    ) -> None:
        from sqlalchemy import distinct, select

        stmt = (
            select(distinct(MarketPriceDB.market_hash_name))
            .where(MarketPriceDB.market_hash_name.ilike(f"%{query}%"))
            .limit(5)
        )
        async with self._factory() as session:
            names = list((await session.execute(stmt)).scalars().all())

        if not names:
            await self._send_or_edit(
                update,
                f"🔍 По запросу <code>{_esc(query)}</code> ничего не найдено.",
                InlineKeyboardMarkup([[InlineKeyboardButton("🔍 Поиск снова", callback_data="sr"), InlineKeyboardButton("« Меню", callback_data="menu")]]),
            )
            return

        imap: dict = context.bot_data.setdefault("item_map", {})
        rows = []
        for name in names:
            k = _key(name)
            imap[k] = name
            rows.append([InlineKeyboardButton(_esc(name[:50]), callback_data=f"dt:{k}")])
        rows.append([InlineKeyboardButton("🔍 Ещё раз", callback_data="sr"), InlineKeyboardButton("« Меню", callback_data="menu")])

        await self._send_or_edit(
            update,
            f"🔍 Результаты по <code>{_esc(query)}</code>:",
            InlineKeyboardMarkup(rows),
        )

    # ── notifications ────────────────────────────────────────────────────────

    async def send_notifications(
        self, app: Application, opps: list[ScoredOpportunity]
    ) -> None:
        if not opps:
            return
        try:
            chat_cfgs = await self._bot_cfg_repo.get_all()
        except Exception as exc:
            logger.error("Failed to load bot settings: %s", exc)
            return

        now = datetime.now(timezone.utc)
        cooldown = self._cfg.NOTIFY_COOLDOWN_MINUTES * 60

        for chat_cfg in chat_cfgs:
            chat_id = chat_cfg.chat_id
            try:
                muted = await self._mute_repo.get_muted(chat_id)
                to_send: list[ScoredOpportunity] = []

                for opp in opps:
                    if opp.market_hash_name in muted:
                        continue
                    if opp.roi_realistic is None:
                        continue
                    roi_pct = opp.roi_realistic * 100
                    if roi_pct < chat_cfg.min_roi_pct:
                        continue
                    if opp.volume_24h is not None and opp.volume_24h < chat_cfg.min_volume_24h:
                        continue

                    last = await self._notif_repo.get(chat_id, opp.market_hash_name)
                    if last:
                        ts = last.last_sent_at
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        # Hard cooldown; a repeat also requires ROI to have grown ≥2 pp
                        age_ok = (now - ts).total_seconds() >= cooldown
                        roi_ok = last.last_roi_pct is None or roi_pct >= last.last_roi_pct + 2.0
                        if not (age_ok and roi_ok):
                            continue

                    to_send.append(opp)
                    if len(to_send) >= self._cfg.NOTIFY_MAX_PER_RUN:
                        break

                for opp in to_send:
                    k = _key(opp.market_hash_name)
                    app.bot_data.setdefault("item_map", {})[k] = opp.market_hash_name
                    text = f"🚨 <b>Новая возможность</b>\n\n{_format_card(opp)}"
                    kb = InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton("Подробнее", callback_data=f"dt:{k}"),
                            InlineKeyboardButton("Steam ↗", url=_steam_url(opp.market_hash_name)),
                        ],
                        [InlineKeyboardButton("🔕 Не присылать", callback_data=f"mu:{k}")],
                    ])
                    await app.bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=kb,
                    )
                    await self._notif_repo.upsert(
                        chat_id,
                        opp.market_hash_name,
                        opp.roi_realistic * 100 if opp.roi_realistic else None,
                    )
            except Exception as exc:
                logger.warning("Notification error for chat %d: %s", chat_id, exc)

    # ── internal ────────────────────────────────────────────────────────────

    async def _send_or_edit(
        self,
        update: Update,
        text: str,
        reply_markup: InlineKeyboardMarkup,
        parse_mode: str = ParseMode.HTML,
    ) -> None:
        if update.callback_query:
            try:
                await update.callback_query.edit_message_text(
                    text, reply_markup=reply_markup, parse_mode=parse_mode
                )
            except BadRequest as exc:
                if "not modified" not in str(exc).lower():
                    logger.warning("edit_message_text failed: %s", exc)
        else:
            await update.effective_message.reply_text(
                text, reply_markup=reply_markup, parse_mode=parse_mode
            )
