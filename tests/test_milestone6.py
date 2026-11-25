from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

from bot.calendar.models import CalendarEvent
from bot.calendar.notifications import enqueue_calendar_notifications
from bot.commands import (
    MARKETS,
    calendar_command,
    digest_command,
    markets_command,
    pause_command,
    register_handlers,
    resume_command,
    sources_command,
    start_command,
    status_command,
    toggle_callback,
)
from bot.digest import send_deferred_digest
from bot.fetchers.base import FeedEntry, FetchResult, Source
from bot.scheduler import (
    OwnerMessageQueue,
    fetch_and_enqueue,
    refresh_calendar_feed,
    send_scheduled_digest,
    start_scheduler,
)
from bot.sources import SOURCES
from bot.storage import StateStore


class RecordingSink:
    def __init__(self) -> None:
        self.items: list[tuple[str, str | None, object | None]] = []

    async def put(
        self,
        message: str,
        parse_mode: str | None = None,
        reply_markup: object | None = None,
    ) -> None:
        self.items.append((message, parse_mode, reply_markup))


def make_context(settings: object, store: object, sink: object) -> SimpleNamespace:
    return SimpleNamespace(
        application=SimpleNamespace(
            bot_data={
                "settings": settings,
                "state_store": store,
                "owner_messages": sink,
            }
        )
    )


def make_update(chat_id: int) -> SimpleNamespace:
    return SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id))


def analysis_json() -> str:
    return json.dumps(
        {
            "summary_fa": "خبر کم‌تأثیر است. واکنش بازار محدود ارزیابی می‌شود.",
            "assets": ["USD"],
            "sentiment": "neutral",
            "impact": "low",
            "reasoning_fa": "داده‌ها اثر محدودی دارند.",
        },
        ensure_ascii=False,
    )


class OwnerCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_commands_and_callbacks_ignore_other_chats(self) -> None:
        store = AsyncMock()
        sink = AsyncMock()
        context = make_context(
            SimpleNamespace(owner_chat_id=123),
            store,
            sink,
        )
        update = make_update(456)
        handlers = (
            start_command,
            status_command,
            sources_command,
            markets_command,
            calendar_command,
            digest_command,
            pause_command,
            resume_command,
        )

        for handler in handlers:
            await handler(update, context)

        query = SimpleNamespace(
            data="source:0",
            answer=AsyncMock(),
            edit_message_reply_markup=AsyncMock(),
        )
        callback_update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=456),
            callback_query=query,
        )
        await toggle_callback(callback_update, context)

        sink.put.assert_not_awaited()
        store.set_paused.assert_not_awaited()
        store.toggle_source_enabled.assert_not_awaited()
        store.toggle_market_enabled.assert_not_awaited()
        query.edit_message_reply_markup.assert_not_awaited()
        query.answer.assert_awaited_once()

    async def test_source_and_market_toggles_persist_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "settings.sqlite3")
            store = StateStore(db_path)
            await store.open()
            sink = RecordingSink()
            settings = SimpleNamespace(owner_chat_id=123)
            context = make_context(settings, store, sink)
            owner_update = make_update(123)
            try:
                await sources_command(owner_update, context)
                await markets_command(owner_update, context)
                self.assertIsNotNone(sink.items[0][2])
                self.assertEqual(MARKETS[0][0], "forex")

                source = SOURCES[0]
                query = SimpleNamespace(
                    data="source:0",
                    answer=AsyncMock(),
                    edit_message_reply_markup=AsyncMock(),
                )
                await toggle_callback(
                    SimpleNamespace(
                        effective_chat=SimpleNamespace(id=123),
                        callback_query=query,
                    ),
                    context,
                )
                self.assertFalse(await store.source_enabled(source.name))

                query.data = "market:forex"
                await toggle_callback(
                    SimpleNamespace(
                        effective_chat=SimpleNamespace(id=123),
                        callback_query=query,
                    ),
                    context,
                )
                self.assertFalse(await store.market_enabled("forex"))
                await store.close()

                restarted = StateStore(db_path)
                await restarted.open()
                try:
                    self.assertFalse(await restarted.source_enabled(source.name))
                    self.assertFalse(await restarted.market_enabled("forex"))
                finally:
                    await restarted.close()
            finally:
                if store._db is not None:
                    await store.close()

    async def test_persistent_toggles_change_the_fetch_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "pipeline.sqlite3"))
            await store.open()
            source = Source("Toggle test", "macro", "rss", "https://example.com/feed")

            class Fetcher:
                def __init__(self) -> None:
                    self.received_sources: list[Source] = []

                async def fetch(self, requested_source: Source) -> FetchResult:
                    self.received_sources.append(requested_source)
                    return FetchResult(
                        requested_source,
                        (
                            (
                                FeedEntry(
                                    "Federal Reserve interest rate decision",
                                    "https://example.com/story",
                                ),
                            )
                            if requested_source.enabled
                            else ()
                        ),
                    )

            fetcher = Fetcher()
            queue = RecordingSink()
            try:
                await store.set_source_enabled(source.name, False)
                await fetch_and_enqueue(
                    {"rss": fetcher},  # type: ignore[dict-item]
                    (source,),
                    queue,  # type: ignore[arg-type]
                    store,
                    AsyncMock(),  # type: ignore[arg-type]
                )
                self.assertFalse(fetcher.received_sources[-1].enabled)
                self.assertTrue(await store.seed_complete())

                await store.set_source_enabled(source.name, True)
                await store.toggle_market_enabled("macro")
                await fetch_and_enqueue(
                    {"rss": fetcher},  # type: ignore[dict-item]
                    (source,),
                    queue,  # type: ignore[arg-type]
                    store,
                    AsyncMock(),  # type: ignore[arg-type]
                )
                cursor = await store._connection().execute(
                    "SELECT COUNT(*) FROM seen_stories"
                )
                self.assertEqual((await cursor.fetchone())[0], 0)
                await cursor.close()
                self.assertEqual(queue.items, [])
            finally:
                await store.close()

    async def test_handlers_are_registered(self) -> None:
        application = SimpleNamespace(add_handler=Mock())
        register_handlers(application)  # type: ignore[arg-type]
        self.assertEqual(application.add_handler.call_count, 9)


class DigestCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_and_scheduled_digest_send_deferred_items_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "digest.sqlite3"))
            await store.open()
            sink = RecordingSink()
            source = Source("Example", "macro", "rss", "https://example.com/feed")
            await store.record_story(
                source.name,
                "Economic outlook headline",
                "https://example.com/story",
                market="macro",
            )
            cursor = await store._connection().execute(
                "SELECT url_hash FROM seen_stories"
            )
            url_hash = (await cursor.fetchone())[0]
            await cursor.close()
            await store.save_analysis(url_hash, analysis_json(), "deferred")
            try:
                await store.set_paused(True)
                await send_scheduled_digest(store, sink)  # skipped while paused
                self.assertEqual(sink.items, [])

                settings = SimpleNamespace(owner_chat_id=123)
                context = make_context(settings, store, sink)
                await digest_command(make_update(123), context)
                self.assertEqual(len(sink.items), 1)
                self.assertIn("خبرهای کم‌تأثیر", sink.items[0][0])
                self.assertIn("Economic outlook headline", sink.items[0][0])
                self.assertEqual(sink.items[0][1], "HTML")

                cursor = await store._connection().execute(
                    "SELECT analysis_status FROM seen_stories WHERE url_hash = ?",
                    (url_hash,),
                )
                self.assertEqual((await cursor.fetchone())[0], "digest_delivered")
                await cursor.close()
                self.assertEqual(await send_deferred_digest(store, sink), 0)
            finally:
                await store.close()

    async def test_digest_cron_uses_configured_time_and_timezone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "scheduler.sqlite3"))
            await store.open()
            sink = RecordingSink()
            scheduler = start_scheduler(
                {},
                (),
                OwnerMessageQueue(AsyncMock(), 123),  # type: ignore[arg-type]
                store,
                AsyncMock(),  # type: ignore[arg-type]
                60,
                ZoneInfo("Asia/Tehran"),
                digest_time="21:15",
            )
            try:
                job = scheduler.get_job("daily-low-impact-digest")
                self.assertIsNotNone(job)
                next_run = job.trigger.get_next_fire_time(
                    None, datetime.now(ZoneInfo("Asia/Tehran"))
                )
                self.assertEqual((next_run.hour, next_run.minute), (21, 15))
                self.assertEqual(next_run.tzinfo, ZoneInfo("Asia/Tehran"))
            finally:
                scheduler.shutdown(wait=False)
                await store.close()

    async def test_scheduled_digest_delivers_when_automation_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "scheduled-digest.sqlite3"))
            await store.open()
            sink = RecordingSink()
            try:
                await store.record_story(
                    "Example",
                    "Low impact market update",
                    "https://example.com/low",
                    market="macro",
                )
                cursor = await store._connection().execute(
                    "SELECT url_hash FROM seen_stories"
                )
                url_hash = (await cursor.fetchone())[0]
                await cursor.close()
                await store.save_analysis(url_hash, analysis_json(), "deferred")

                await send_scheduled_digest(store, sink)
                self.assertEqual(len(sink.items), 1)
                cursor = await store._connection().execute(
                    "SELECT analysis_status FROM seen_stories WHERE url_hash = ?",
                    (url_hash,),
                )
                self.assertEqual((await cursor.fetchone())[0], "digest_delivered")
                await cursor.close()
            finally:
                await store.close()


class CalendarCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_calendar_command_filters_by_local_day_and_high_impact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "calendar.sqlite3"))
            await store.open()
            local_timezone = ZoneInfo("Asia/Tehran")
            today_event = CalendarEvent(
                "today",
                "Today CPI",
                "USD",
                "high",
                datetime(2030, 1, 3, 13, 30, tzinfo=timezone.utc),
            )
            tomorrow_event = CalendarEvent(
                "tomorrow",
                "Tomorrow CPI",
                "USD",
                "high",
                datetime(2030, 1, 3, 21, 0, tzinfo=timezone.utc),
            )
            medium_event = CalendarEvent(
                "medium",
                "Medium event",
                "EUR",
                "medium",
                today_event.event_at,
            )
            await store.replace_calendar_events(
                (today_event, tomorrow_event, medium_event)
            )
            sink = RecordingSink()
            context = make_context(
                SimpleNamespace(owner_chat_id=123, timezone=local_timezone),
                store,
                sink,
            )

            class FixedDateTime(datetime):
                @classmethod
                def now(cls, tz: object = None) -> datetime:
                    return datetime(2030, 1, 3, 16, 0, tzinfo=local_timezone)

            try:
                with patch("bot.commands.datetime", FixedDateTime):
                    await calendar_command(make_update(123), context)
                self.assertEqual(len(sink.items), 1)
                self.assertIn("Today CPI", sink.items[0][0])
                self.assertNotIn("Tomorrow CPI", sink.items[0][0])
                self.assertNotIn("Medium event", sink.items[0][0])
            finally:
                await store.close()


class PauseResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_pause_persists_and_skips_automation_but_resume_restores_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "paused.sqlite3")
            store = StateStore(db_path)
            await store.open()
            sink = RecordingSink()
            context = make_context(SimpleNamespace(owner_chat_id=123), store, sink)
            try:
                await pause_command(make_update(123), context)
                self.assertTrue(await store.is_paused())
                await store.close()

                restarted = StateStore(db_path)
                await restarted.open()
                try:
                    self.assertTrue(await restarted.is_paused())
                    fetcher = AsyncMock()
                    await fetch_and_enqueue(
                        {"rss": fetcher},
                        (Source("Example", "macro", "rss", "https://example.com"),),
                        OwnerMessageQueue(AsyncMock(), 123),  # type: ignore[arg-type]
                        restarted,
                        AsyncMock(),  # type: ignore[arg-type]
                    )
                    fetcher.fetch.assert_not_awaited()

                    calendar_client = SimpleNamespace(refresh=AsyncMock())
                    await refresh_calendar_feed(restarted, calendar_client)  # type: ignore[arg-type]
                    calendar_client.refresh.assert_not_awaited()

                    await resume_command(make_update(123), context_for_store(context, restarted))
                    self.assertFalse(await restarted.is_paused())
                    await send_scheduled_digest(restarted, sink)
                finally:
                    await restarted.close()
            finally:
                if store._db is not None:
                    await store.close()


def context_for_store(
    context: SimpleNamespace, store: StateStore
) -> SimpleNamespace:
    context.application.bot_data["state_store"] = store
    return context
