from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Protocol

from bot.calendar.models import CalendarEvent
from bot.storage import StateStore


class MessageSink(Protocol):
    async def put(self, message: str, parse_mode: str | None = None) -> None: ...


async def enqueue_calendar_notifications(
    state_store: StateStore,
    messages: MessageSink,
    display_timezone: tzinfo,
    reminder_minutes: int = 30,
    now: datetime | None = None,
) -> None:
    if await state_store.is_paused():
        return
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    current_time = current_time.astimezone(timezone.utc)

    for event in await state_store.calendar_events():
        reminder_time = event.event_at - timedelta(minutes=reminder_minutes)
        if (
            event.impact == "high"
            and reminder_time <= current_time < event.event_at
            and await state_store.claim_calendar_notification(
                event.event_id, "reminder"
            )
        ):
            await messages.put(
                _format_reminder(event, display_timezone),
                parse_mode="HTML",
            )

        if (
            current_time >= event.event_at
            and event.actual is not None
            and await state_store.claim_calendar_notification(event.event_id, "actual")
        ):
            await messages.put(
                _format_actual(event, display_timezone),
                parse_mode="HTML",
            )


def _format_reminder(event: CalendarEvent, display_timezone: tzinfo) -> str:
    return "\n".join(
        (
            "<b>یادآوری رویداد اقتصادی</b>",
            f"<b>{_escape(event.title)}</b>",
            f"<b>ارز:</b> {_escape(event.currency)}",
            f"<b>زمان:</b> {_event_time(event, display_timezone)}",
            f"<b>اهمیت:</b> بالا",
        )
    )


def _format_actual(event: CalendarEvent, display_timezone: tzinfo) -> str:
    return "\n".join(
        (
            "<b>نتیجهٔ رویداد اقتصادی</b>",
            f"<b>{_escape(event.title)}</b>",
            f"<b>ارز:</b> {_escape(event.currency)}",
            f"<b>زمان:</b> {_event_time(event, display_timezone)}",
            f"<b>واقعی:</b> {_escape(event.actual or 'نامشخص')}",
            f"<b>پیش‌بینی:</b> {_escape(event.forecast or 'نامشخص')}",
            f"<b>قبلی:</b> {_escape(event.previous or 'نامشخص')}",
        )
    )


def _event_time(event: CalendarEvent, display_timezone: tzinfo) -> str:
    formatted = event.event_at.astimezone(display_timezone).strftime(
        "%Y-%m-%d %H:%M %Z"
    )
    return _escape(formatted)


def _escape(value: str) -> str:
    return html.escape(value, quote=True)
