from __future__ import annotations

import html
from datetime import datetime
from typing import Final

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from bot.calendar.models import CalendarEvent
from bot.config import Settings
from bot.digest import send_deferred_digest
from bot.fetchers.base import Source
from bot.scheduler import OwnerMessageQueue
from bot.sources import SOURCES
from bot.storage import SourceState, StateStore

MARKETS: Final[tuple[tuple[str, str], ...]] = (
    ("forex", "فارکس"),
    ("global_stock", "سهام جهانی"),
    ("macro", "اقتصاد کلان"),
)


def register_handlers(application: Application) -> None:
    for command, handler in (
        ("start", start_command),
        ("status", status_command),
        ("sources", sources_command),
        ("markets", markets_command),
        ("calendar", calendar_command),
        ("digest", digest_command),
        ("pause", pause_command),
        ("resume", resume_command),
    ):
        application.add_handler(CommandHandler(command, handler))
    application.add_handler(
        CallbackQueryHandler(toggle_callback, pattern=r"^(source|market):")
    )


async def start_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if await _authorized_settings(update, context) is None:
        return
    await _messages(context).put(
        "ربات اخبار بازار فعال است.\n"
        "/status — وضعیت منابع و خبرها\n"
        "/sources — مدیریت منابع\n"
        "/markets — مدیریت بازارها\n"
        "/calendar — رویدادهای مهم امروز\n"
        "/digest — ارسال فوری خلاصهٔ خبرهای کم‌تأثیر\n"
        "/pause و /resume — توقف یا ادامهٔ پایش خودکار"
    )


async def status_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    settings = await _authorized_settings(update, context)
    if settings is None:
        return

    store = _store(context)
    counts = await store.story_counts()
    paused = await store.is_paused()
    lines = [
        f"<b>وضعیت پایش:</b> {'متوقف' if paused else 'فعال'}",
        f"<b>خبرهای ذخیره‌شده:</b> {counts.total}",
        f"<b>تحلیل‌شده:</b> {counts.processed}",
        f"<b>کم‌تأثیر در انتظار digest:</b> {counts.deferred}",
        f"<b>ارسال‌شده در digest:</b> {counts.digest_delivered}",
        "<b>بر اساس بازار:</b>",
    ]
    lines.extend(
        f"• {label}: {counts.by_market.get(market, 0)}"
        for market, label in MARKETS
    )
    lines.append("<b>آخرین دریافت هر منبع:</b>")
    for source in SOURCES:
        state = await store.get(source.name)
        enabled = await store.source_enabled(source.name, source.enabled)
        lines.append(
            _source_status_line(source, state, enabled, settings)
        )
    failing = [
        source.name
        for source in SOURCES
        if (state := await store.get(source.name)).consecutive_failures > 0
        or state.disabled
    ]
    lines.append(
        "<b>منابع دارای خطا:</b> "
        + (", ".join(html.escape(name) for name in failing) if failing else "ندارد")
    )
    await _messages(context).put("\n".join(lines), parse_mode="HTML")


async def sources_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if await _authorized_settings(update, context) is None:
        return
    enabled_map = {
        index: await _store(context).source_enabled(source.name, source.enabled)
        for index, source in enumerate(SOURCES)
    }
    await _messages(context).put(
        "برای روشن یا خاموش‌کردن هر منبع، دکمهٔ آن را انتخاب کنید:",
        reply_markup=_source_keyboard(enabled_map),
    )


async def markets_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if await _authorized_settings(update, context) is None:
        return
    enabled_map = {
        market: await _store(context).market_enabled(market)
        for market, _label in MARKETS
    }
    await _messages(context).put(
        "بازارهای فعال برای دریافت خبر:",
        reply_markup=_market_keyboard(enabled_map),
    )


async def calendar_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    settings = await _authorized_settings(update, context)
    if settings is None:
        return

    now = datetime.now(settings.timezone)
    todays_events = [
        event
        for event in await _store(context).calendar_events()
        if event.impact == "high"
        and event.event_at.astimezone(settings.timezone).date() == now.date()
    ]
    if not todays_events:
        await _messages(context).put("امروز رویداد اقتصادی پر‌اهمیت ثبت نشده است.")
        return

    await _messages(context).put(
        "\n\n".join(_format_calendar_event(event, settings) for event in todays_events),
        parse_mode="HTML",
    )


async def digest_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if await _authorized_settings(update, context) is None:
        return
    delivered = await send_deferred_digest(_store(context), _messages(context))
    if delivered == 0:
        await _messages(context).put("خبر کم‌تأثیرِ آمادهٔ ارسال وجود ندارد.")


async def pause_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if await _authorized_settings(update, context) is None:
        return
    await _store(context).set_paused(True)
    await _messages(context).put(
        "پایش خودکار متوقف شد. برای ادامه از /resume استفاده کنید."
    )


async def resume_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if await _authorized_settings(update, context) is None:
        return
    await _store(context).set_paused(False)
    await _messages(context).put("پایش خودکار از سر گرفته شد.")


async def toggle_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if query is None:
        return
    if await _authorized_settings(update, context) is None:
        await query.answer()
        return
    data = query.data or ""
    if data.startswith("source:"):
        try:
            index = int(data.partition(":")[2])
            source = SOURCES[index]
        except (IndexError, ValueError):
            await query.answer("منبع نامعتبر است.", show_alert=True)
            return
        enabled = await _store(context).toggle_source_enabled(
            source.name, source.enabled
        )
        enabled_map = {
            source_index: await _store(context).source_enabled(
                item.name, item.enabled
            )
            for source_index, item in enumerate(SOURCES)
        }
        await query.edit_message_reply_markup(
            reply_markup=_source_keyboard(enabled_map)
        )
        await query.answer(f"{source.name}: {'روشن' if enabled else 'خاموش'}")
        return

    market = data.partition(":")[2]
    if market not in {item[0] for item in MARKETS}:
        await query.answer("بازار نامعتبر است.", show_alert=True)
        return
    enabled = await _store(context).toggle_market_enabled(market)
    enabled_map = {
        key: await _store(context).market_enabled(key)
        for key, _label in MARKETS
    }
    await query.edit_message_reply_markup(
        reply_markup=_market_keyboard(enabled_map)
    )
    await query.answer(f"{_market_label(market)}: {'روشن' if enabled else 'خاموش'}")


async def _authorized_settings(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> Settings | None:
    settings: Settings = context.application.bot_data["settings"]
    chat = update.effective_chat
    if chat is None or chat.id != settings.owner_chat_id:
        return None
    return settings


def _store(context: ContextTypes.DEFAULT_TYPE) -> StateStore:
    return context.application.bot_data["state_store"]


def _messages(context: ContextTypes.DEFAULT_TYPE) -> OwnerMessageQueue:
    return context.application.bot_data["owner_messages"]


def _source_keyboard(enabled_map: dict[int, bool]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"{'✅' if enabled_map[index] else '❌'} {source.name}",
                    callback_data=f"source:{index}",
                )
            ]
            for index, source in enumerate(SOURCES)
        ]
    )


def _market_keyboard(enabled_map: dict[str, bool]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"{'✅' if enabled_map[market] else '❌'} {label}",
                    callback_data=f"market:{market}",
                )
            ]
            for market, label in MARKETS
        ]
    )


def _source_status_line(
    source: Source,
    state: SourceState,
    enabled: bool,
    settings: Settings,
) -> str:
    if not enabled:
        state_label = "خاموش"
    elif state.disabled:
        state_label = "غیرفعال پس از خطا"
    elif state.consecutive_failures:
        state_label = f"خطا {state.consecutive_failures}"
    else:
        state_label = "فعال"
    last_fetched = _format_time(state.last_fetched_at, settings)
    error = (
        f" — {html.escape(state.last_error)}"
        if state.last_error and state.consecutive_failures
        else ""
    )
    return (
        f"• {html.escape(source.name)}: {state_label}; "
        f"آخرین دریافت: {last_fetched}{error}"
    )


def _format_calendar_event(event: CalendarEvent, settings: Settings) -> str:
    event_time = event.event_at.astimezone(settings.timezone).strftime(
        "%H:%M %Z"
    )
    return (
        f"<b>{html.escape(event.title)}</b>\n"
        f"{html.escape(event.currency)} — {event_time}"
    )


def _format_time(value: str | None, settings: Settings) -> str:
    if value is None:
        return "هنوز دریافت نشده"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return "نامشخص"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=settings.timezone)
    return parsed.astimezone(settings.timezone).strftime("%Y-%m-%d %H:%M")


def _market_label(market: str) -> str:
    return next(label for key, label in MARKETS if key == market)
