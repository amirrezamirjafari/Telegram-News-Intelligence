from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from dataclasses import replace
from datetime import tzinfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot, InlineKeyboardMarkup
from telegram.error import TelegramError

from bot.calendar.feed import CalendarFeedClient
from bot.calendar.notifications import enqueue_calendar_notifications
from bot.classification import classify_entry
from bot.digest import send_deferred_digest
from bot.fetchers.base import BaseFetcher, FetchResult, Source
from bot.formatter import format_analysis_message
from bot.pipeline import NewsPipeline
from bot.storage import StateStore

LOGGER = logging.getLogger(__name__)
MESSAGE_INTERVAL_SECONDS = 1.0
CALENDAR_REFRESH_MINUTES = 15
CALENDAR_CHECK_INTERVAL_MINUTES = 1


class OwnerMessageQueue:
    def __init__(self, bot: Bot, owner_chat_id: int) -> None:
        self._bot = bot
        self._owner_chat_id = owner_chat_id
        self._queue: asyncio.Queue[
            tuple[str, str | None, InlineKeyboardMarkup | None] | None
        ] = asyncio.Queue()

    async def put(
        self,
        message: str,
        parse_mode: str | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        await self._queue.put((message, parse_mode, reply_markup))

    async def run(self, interval_seconds: float = MESSAGE_INTERVAL_SECONDS) -> None:
        loop = asyncio.get_running_loop()
        last_request_at: float | None = None
        while True:
            item = await self._queue.get()
            try:
                if item is None:
                    return

                if last_request_at is not None:
                    delay = interval_seconds - (loop.time() - last_request_at)
                    if delay > 0:
                        await asyncio.sleep(delay)

                last_request_at = loop.time()
                message, parse_mode, reply_markup = item
                try:
                    if parse_mode is not None and reply_markup is not None:
                        await self._bot.send_message(
                            chat_id=self._owner_chat_id,
                            text=message,
                            parse_mode=parse_mode,
                            reply_markup=reply_markup,
                        )
                    elif parse_mode is not None:
                        await self._bot.send_message(
                            chat_id=self._owner_chat_id,
                            text=message,
                            parse_mode=parse_mode,
                        )
                    elif reply_markup is not None:
                        await self._bot.send_message(
                            chat_id=self._owner_chat_id,
                            text=message,
                            reply_markup=reply_markup,
                        )
                    else:
                        await self._bot.send_message(
                            chat_id=self._owner_chat_id,
                            text=message,
                        )
                except TelegramError as exc:
                    LOGGER.warning(
                        "Telegram delivery failed (%s)", type(exc).__name__
                    )
            finally:
                self._queue.task_done()

    async def stop(self) -> None:
        await self._queue.put(None)
        await self._queue.join()


async def fetch_and_enqueue(
    fetchers: dict[str, BaseFetcher],
    sources: Sequence[Source],
    messages: OwnerMessageQueue,
    state_store: StateStore,
    pipeline: NewsPipeline,
) -> None:
    if await state_store.is_paused():
        return

    effective_sources: list[Source] = []
    for source in sources:
        enabled = await state_store.source_enabled(source.name, source.enabled)
        effective_sources.append(replace(source, enabled=enabled))
    sources = tuple(effective_sources)

    results: list[FetchResult] = []
    for source in sources:
        try:
            result = await fetchers[source.type].fetch(source)
            results.append(result)
        except Exception:
            LOGGER.exception("Unexpected error while processing feed %s", source.name)

    classified_results: list[FetchResult] = []
    for result in results:
        classified_entries = []
        for entry in result.entries:
            classified = classify_entry(entry)
            if classified is not None and await state_store.market_enabled(
                classified.market or result.source.market
            ):
                classified_entries.append(classified)
        classified_results.append(replace(result, entries=tuple(classified_entries)))
    results = classified_results

    if not await state_store.seed_complete():
        result_by_name = {result.source.name: result for result in results}
        for result in results:
            if result.succeeded:
                for entry in result.entries:
                    await state_store.record_story(
                        result.source.name,
                        entry.title,
                        entry.link,
                        entry.summary,
                        market=entry.market,
                        published_at=(
                            entry.published_at.isoformat()
                            if entry.published_at is not None
                            else None
                        ),
                        analysis_status="seeded",
                    )

        seed_ready = all(
            not source.enabled
            or (
                source.name in result_by_name
                and (
                    result_by_name[source.name].succeeded
                    or result_by_name[source.name].disabled
                )
            )
            for source in sources
        )
        if seed_ready:
            await state_store.mark_seed_complete()
        return

    for source in sources:
        state = await state_store.get(source.name)
        notice_sent = await state_store.disable_notice_sent(source.name)
        if source.enabled and state.disabled and not notice_sent:
            await messages.put(
                f"Feed disabled after five consecutive failures: {source.name}"
            )
            await state_store.mark_disable_notice_sent(source.name)

    for result in results:
        if not result.succeeded:
            continue
        for entry in result.entries:
            await state_store.record_story(
                result.source.name,
                entry.title,
                entry.link,
                entry.summary,
                market=entry.market,
                published_at=(
                    entry.published_at.isoformat()
                    if entry.published_at is not None
                    else None
                ),
                analysis_status="pending",
            )
    await pipeline.process_pending_stories(state_store, messages)


def start_scheduler(
    fetchers: dict[str, BaseFetcher],
    sources: Sequence[Source],
    messages: OwnerMessageQueue,
    state_store: StateStore,
    pipeline: NewsPipeline,
    interval_minutes: int,
    timezone: tzinfo,
    calendar_feed_client: CalendarFeedClient | None = None,
    calendar_reminder_minutes: int = 30,
    digest_time: str = "21:00",
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=timezone)
    scheduler.add_job(
        fetch_and_enqueue,
        trigger="interval",
        minutes=interval_minutes,
        args=[fetchers, sources, messages, state_store, pipeline],
        id="fetch-news-feeds",
        coalesce=True,
        max_instances=1,
    )
    digest_hour, digest_minute = (int(part) for part in digest_time.split(":"))
    scheduler.add_job(
        send_scheduled_digest,
        trigger="cron",
        hour=digest_hour,
        minute=digest_minute,
        timezone=timezone,
        args=[state_store, messages],
        id="daily-low-medium-digest",
        coalesce=True,
        max_instances=1,
    )
    if calendar_feed_client is not None:
        scheduler.add_job(
            refresh_calendar_feed,
            trigger="interval",
            minutes=CALENDAR_REFRESH_MINUTES,
            args=[state_store, calendar_feed_client],
            id="refresh-economic-calendar",
            coalesce=True,
            max_instances=1,
        )
        scheduler.add_job(
            enqueue_calendar_notifications,
            trigger="interval",
            minutes=CALENDAR_CHECK_INTERVAL_MINUTES,
            args=[
                state_store,
                messages,
                timezone,
                calendar_reminder_minutes,
            ],
            id="economic-calendar-notifications",
            coalesce=True,
            max_instances=1,
        )
    scheduler.start()
    return scheduler


async def refresh_calendar_feed(
    state_store: StateStore,
    calendar_feed_client: CalendarFeedClient,
) -> None:
    if not await state_store.is_paused():
        await calendar_feed_client.refresh()


async def send_scheduled_digest(
    state_store: StateStore,
    messages: OwnerMessageQueue,
) -> None:
    if not await state_store.is_paused():
        await send_deferred_digest(state_store, messages)
