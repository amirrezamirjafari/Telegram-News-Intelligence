from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from bot.calendar.feed import CalendarFeedClient
from bot.calendar.models import CalendarEvent
from bot.calendar.notifications import enqueue_calendar_notifications
from bot.calendar.parser import CalendarParseError, parse_events
from bot.storage import StateStore


class CalendarParserTests(unittest.TestCase):
    def test_parses_timezone_and_all_optional_values(self) -> None:
        events = parse_events(
            json.dumps(
                [
                    {
                        "title": "CPI <headline>",
                        "country": "USD",
                        "date": "2030-01-03T08:30:00-05:00",
                        "impact": "High",
                        "forecast": "",
                        "previous": "3.1%",
                    },
                    {
                        "title": "Employment index",
                        "currency": "EUR",
                        "date": "2030-01-03T16:00:00",
                        "impact": "Medium",
                        "actual": 0,
                    },
                ]
            ),
            default_timezone=ZoneInfo("Asia/Tehran"),
        )

        self.assertEqual(len(events), 2)
        by_title = {event.title: event for event in events}
        cpi = by_title["CPI <headline>"]
        employment = by_title["Employment index"]
        self.assertEqual(
            cpi.event_at,
            datetime(2030, 1, 3, 13, 30, tzinfo=timezone.utc),
        )
        self.assertIsNotNone(cpi.event_at.tzinfo)
        self.assertIsNone(cpi.actual)
        self.assertIsNone(cpi.forecast)
        self.assertEqual(cpi.previous, "3.1%")
        self.assertEqual(
            employment.event_at,
            datetime(2030, 1, 3, 12, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(employment.actual, "0")
        self.assertEqual(employment.impact, "medium")

    def test_malformed_feed_is_rejected_as_a_whole(self) -> None:
        with self.assertRaises(CalendarParseError):
            parse_events(b'[{"title":"Missing date","impact":"High"}]')


class CalendarFeedCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_feed_failures_use_persisted_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "calendar.sqlite3"))
            await store.open()
            cached_event = CalendarEvent(
                "stable-event",
                "Policy decision",
                "USD",
                "high",
                datetime(2030, 1, 3, 13, 30, tzinfo=timezone.utc),
            )
            await store.replace_calendar_events((cached_event,))
            try:
                responses = (
                    httpx.Response(403),
                    httpx.Response(429),
                    httpx.Response(200, content=b"not-json"),
                )
                for response in responses:
                    async def handler(
                        request: httpx.Request,
                        response: httpx.Response = response,
                    ) -> httpx.Response:
                        return httpx.Response(
                            response.status_code,
                            headers=response.headers,
                            content=response.content,
                            request=request,
                        )

                    client = CalendarFeedClient(
                        "https://calendar.example/feed.json",
                        store,
                        transport=httpx.MockTransport(handler),
                    )
                    try:
                        with self.assertLogs("bot.calendar.feed", level="WARNING"):
                            events = await client.refresh()
                        self.assertEqual(events, (cached_event,))
                    finally:
                        await client.close()

                async def offline(request: httpx.Request) -> httpx.Response:
                    raise httpx.ConnectError("offline", request=request)

                client = CalendarFeedClient(
                    "https://calendar.example/feed.json",
                    store,
                    transport=httpx.MockTransport(offline),
                )
                try:
                    with self.assertLogs("bot.calendar.feed", level="WARNING"):
                        events = await client.refresh()
                    self.assertEqual(events, (cached_event,))
                finally:
                    await client.close()
            finally:
                await store.close()

    async def test_valid_feed_replaces_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "calendar.sqlite3"))
            await store.open()
            old_event = CalendarEvent(
                "old",
                "Old event",
                "EUR",
                "low",
                datetime(2030, 1, 2, 13, 30, tzinfo=timezone.utc),
            )
            await store.replace_calendar_events((old_event,))
            event_data = [
                {
                    "title": "Rate decision",
                    "country": "USD",
                    "date": "2030-01-03T13:30:00Z",
                    "impact": "High",
                    "actual": "4.5%",
                }
            ]

            async def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    200,
                    json=event_data,
                    request=request,
                )

            client = CalendarFeedClient(
                "https://calendar.example/feed.json",
                store,
                transport=httpx.MockTransport(handler),
            )
            try:
                events = await client.refresh()
                self.assertEqual(events, await store.calendar_events())
                self.assertEqual([event.title for event in events], ["Rate decision"])
                self.assertEqual(events[0].actual, "4.5%")
            finally:
                await client.close()
                await store.close()


class CalendarNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_paused_mode_suppresses_reminder_and_actual_notifications(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "paused-calendar.sqlite3"))
            await store.open()
            messages = RecordingSink()
            event = CalendarEvent(
                "paused-cpi-us",
                "CPI release",
                "USD",
                "high",
                datetime(2030, 1, 3, 12, 30, tzinfo=timezone.utc),
                actual="3.2%",
            )
            await store.replace_calendar_events((event,))
            try:
                await store.set_paused(True)
                await enqueue_calendar_notifications(
                    store,
                    messages,
                    ZoneInfo("Asia/Tehran"),
                    now=datetime(2030, 1, 3, 12, 0, tzinfo=timezone.utc),
                )
                await enqueue_calendar_notifications(
                    store,
                    messages,
                    ZoneInfo("Asia/Tehran"),
                    now=datetime(2030, 1, 3, 12, 31, tzinfo=timezone.utc),
                )
                self.assertEqual(messages.items, [])

                await store.set_paused(False)
                await enqueue_calendar_notifications(
                    store,
                    messages,
                    ZoneInfo("Asia/Tehran"),
                    now=datetime(2030, 1, 3, 12, 0, tzinfo=timezone.utc),
                )
                await enqueue_calendar_notifications(
                    store,
                    messages,
                    ZoneInfo("Asia/Tehran"),
                    now=datetime(2030, 1, 3, 12, 31, tzinfo=timezone.utc),
                )
                self.assertEqual(len(messages.items), 2)
                self.assertIn("یادآوری رویداد اقتصادی", messages.items[0][0])
                self.assertIn("نتیجهٔ رویداد اقتصادی", messages.items[1][0])
            finally:
                await store.close()

    async def test_reminder_and_actual_are_sent_once_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "calendar.sqlite3")
            store = StateStore(database_path)
            await store.open()
            messages = RecordingSink()
            event = CalendarEvent(
                "cpi-us-2030-01-03",
                "CPI <release>",
                "USD",
                "high",
                datetime(2030, 1, 3, 12, 30, tzinfo=timezone.utc),
                forecast=None,
                previous=None,
            )
            await store.replace_calendar_events((event,))
            reminder_time = datetime(2030, 1, 3, 12, 0, tzinfo=timezone.utc)
            try:
                await enqueue_calendar_notifications(
                    store,
                    messages,
                    ZoneInfo("Asia/Tehran"),
                    now=reminder_time,
                )
                await store.close()

                restarted_store = StateStore(database_path)
                await restarted_store.open()
                try:
                    await enqueue_calendar_notifications(
                        restarted_store,
                        messages,
                        ZoneInfo("Asia/Tehran"),
                        now=reminder_time,
                    )
                    self.assertEqual(len(messages.items), 1)
                    self.assertIn("&lt;release&gt;", messages.items[0][0])
                    self.assertIn("16:00", messages.items[0][0])

                    event_with_actual = CalendarEvent(
                        event.event_id,
                        event.title,
                        event.currency,
                        event.impact,
                        event.event_at,
                        actual="3.2%",
                    )
                    await restarted_store.replace_calendar_events(
                        (event_with_actual,)
                    )
                    after_release = datetime(
                        2030, 1, 3, 12, 31, tzinfo=timezone.utc
                    )
                    await enqueue_calendar_notifications(
                        restarted_store,
                        messages,
                        ZoneInfo("Asia/Tehran"),
                        now=after_release,
                    )
                    await enqueue_calendar_notifications(
                        restarted_store,
                        messages,
                        ZoneInfo("Asia/Tehran"),
                        now=after_release,
                    )
                    self.assertEqual(len(messages.items), 2)
                    actual_message, parse_mode = messages.items[1]
                    self.assertEqual(parse_mode, "HTML")
                    self.assertIn("3.2%", actual_message)
                    self.assertIn("پیش‌بینی:</b> نامشخص", actual_message)
                    self.assertIn("قبلی:</b> نامشخص", actual_message)
                finally:
                    await restarted_store.close()
            finally:
                if store._db is not None:
                    await store.close()


class RecordingSink:
    def __init__(self) -> None:
        self.items: list[tuple[str, str | None]] = []

    async def put(self, message: str, parse_mode: str | None = None) -> None:
        self.items.append((message, parse_mode))
