from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from bot.analyzer import NewsAnalysis
from bot.fetchers.base import FeedEntry, FetchResult, Source
from bot.scheduler import OwnerMessageQueue, fetch_and_enqueue
from bot.storage import FAILURE_LIMIT, StateStore, normalize_url


class StaticFetcher:
    def __init__(self, *results: FetchResult) -> None:
        self.results = {result.source.name: result for result in results}

    async def fetch(self, source: Source) -> FetchResult:
        return self.results[source.name]


class RecordingBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []
        self.parse_modes: list[str | None] = []

    async def send_message(
        self, chat_id: int, text: str, parse_mode: str | None = None
    ) -> None:
        self.messages.append((chat_id, text))
        self.parse_modes.append(parse_mode)


class StaticAnalyzer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def analyze(self, title: str, summary: str | None) -> NewsAnalysis:
        self.calls.append((title, summary))
        return NewsAnalysis(
            summary_fa="خبر بر بازار اثر می‌گذارد. واکنش بازار را دنبال کنید.",
            assets=("EUR/USD",),
            sentiment="neutral",
            impact="high",
            reasoning_fa="خبر می‌تواند بر انتظارات نرخ بهره اثر بگذارد.",
        )


class StoryDeduplicationTests(unittest.IsolatedAsyncioTestCase):
    def test_root_urls_normalize_to_the_same_identity(self) -> None:
        self.assertEqual(
            normalize_url("https://example.com"),
            normalize_url("https://www.example.com/"),
        )

    async def test_first_run_is_silent_and_new_story_is_delivered_after_seed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.sqlite3"))
            await store.open()
            source = Source("Source A", "forex", "rss", "https://example.com/feed")
            old_entry = FeedEntry(
                "Existing EUR/USD market headline",
                "https://example.com/old",
                "USD/JPY remains stable.",
            )
            fetcher = StaticFetcher(FetchResult(source, (old_entry,)))
            bot = RecordingBot()
            queue = OwnerMessageQueue(bot, owner_chat_id=123)
            delivery_task = asyncio.create_task(queue.run(interval_seconds=0))
            analyzer = StaticAnalyzer()
            try:
                await fetch_and_enqueue(
                    {"rss": fetcher},
                    (source,),
                    queue,
                    store,  # type: ignore[arg-type]
                    analyzer,  # type: ignore[arg-type]
                )
                self.assertEqual(bot.messages, [])
                self.assertEqual(analyzer.calls, [])
                self.assertTrue(await store.seed_complete())
                cursor = await store._connection().execute(
                    "SELECT summary FROM seen_stories WHERE link = ?",
                    (old_entry.link,),
                )
                seeded_summary = await cursor.fetchone()
                await cursor.close()
                self.assertEqual(seeded_summary[0], old_entry.summary)

                new_entry = FeedEntry(
                    "New central bank policy headline",
                    "https://example.com/new",
                    "The Federal Reserve signaled a change in interest rates.",
                )
                irrelevant_entry = FeedEntry(
                    "Local council approves a new park",
                    "https://example.com/local",
                )
                fetcher.results[source.name] = FetchResult(
                    source, (old_entry, new_entry, irrelevant_entry)
                )
                await fetch_and_enqueue(
                    {"rss": fetcher},
                    (source,),
                    queue,
                    store,  # type: ignore[arg-type]
                    analyzer,  # type: ignore[arg-type]
                )
                await queue.stop()

                self.assertEqual(len(bot.messages), 1)
                self.assertEqual(bot.messages[0][0], 123)
                self.assertIn(new_entry.title, bot.messages[0][1])
                self.assertIn("<b>بازار:</b> اقتصاد کلان", bot.messages[0][1])
                self.assertNotIn(old_entry.title, bot.messages[0][1])
                self.assertNotIn(irrelevant_entry.title, bot.messages[0][1])
                self.assertNotIn(new_entry.summary, bot.messages[0][1])
                self.assertEqual(bot.parse_modes, ["HTML"])
                cursor = await store._connection().execute(
                    "SELECT summary FROM seen_stories WHERE link = ?",
                    (new_entry.link,),
                )
                delivered_summary = await cursor.fetchone()
                await cursor.close()
                self.assertEqual(delivered_summary[0], new_entry.summary)
            finally:
                if not delivery_task.done():
                    delivery_task.cancel()
                    try:
                        await delivery_task
                    except asyncio.CancelledError:
                        pass
                await store.close()

    async def test_failed_feed_keeps_initial_seeding_silent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.sqlite3"))
            await store.open()
            source_a = Source("Source A", "forex", "rss", "https://example.com/a")
            source_b = Source("Source B", "macro", "rss", "https://example.com/b")
            result_a = FetchResult(
                source_a,
                (FeedEntry("Existing forex headline", "https://example.com/a/1"),),
            )
            fetcher = StaticFetcher(
                result_a,
                FetchResult(source_b, (), succeeded=False),
            )
            bot = RecordingBot()
            queue = OwnerMessageQueue(bot, owner_chat_id=123)
            delivery_task = asyncio.create_task(queue.run(interval_seconds=0))
            try:
                await fetch_and_enqueue(
                    {"rss": fetcher},
                    (source_a, source_b),
                    queue,
                    store,  # type: ignore[arg-type]
                    StaticAnalyzer(),  # type: ignore[arg-type]
                )
                self.assertFalse(await store.seed_complete())
                self.assertEqual(bot.messages, [])

                fetcher.results[source_b.name] = FetchResult(
                    source_b,
                    (
                        FeedEntry(
                            "Existing macro headline", "https://example.com/b/1"
                        ),
                    ),
                )
                await fetch_and_enqueue(
                    {"rss": fetcher},
                    (source_a, source_b),
                    queue,
                    store,  # type: ignore[arg-type]
                    StaticAnalyzer(),  # type: ignore[arg-type]
                )
                self.assertTrue(await store.seed_complete())
                self.assertEqual(bot.messages, [])
                await queue.stop()
            finally:
                if not delivery_task.done():
                    delivery_task.cancel()
                    try:
                        await delivery_task
                    except asyncio.CancelledError:
                        pass
                await store.close()

    async def test_disabled_feed_warning_is_deferred_until_after_silent_seed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.sqlite3"))
            await store.open()
            source = Source("Source A", "forex", "rss", "https://example.com/feed")
            for _ in range(FAILURE_LIMIT):
                await store.record_failure(source.name)
            fetcher = StaticFetcher(
                FetchResult(source, (), disabled=True, succeeded=True)
            )
            bot = RecordingBot()
            queue = OwnerMessageQueue(bot, owner_chat_id=123)
            delivery_task = asyncio.create_task(queue.run(interval_seconds=0))
            try:
                await fetch_and_enqueue(
                    {"rss": fetcher},
                    (source,),
                    queue,
                    store,  # type: ignore[arg-type]
                    StaticAnalyzer(),  # type: ignore[arg-type]
                )
                self.assertTrue(await store.seed_complete())
                self.assertEqual(bot.messages, [])

                await fetch_and_enqueue(
                    {"rss": fetcher},
                    (source,),
                    queue,
                    store,  # type: ignore[arg-type]
                    StaticAnalyzer(),  # type: ignore[arg-type]
                )
                await fetch_and_enqueue(
                    {"rss": fetcher},
                    (source,),
                    queue,
                    store,  # type: ignore[arg-type]
                    StaticAnalyzer(),  # type: ignore[arg-type]
                )
                await queue.stop()

                self.assertEqual(len(bot.messages), 1)
                self.assertIn("disabled after five consecutive failures", bot.messages[0][1])
            finally:
                if not delivery_task.done():
                    delivery_task.cancel()
                    try:
                        await delivery_task
                    except asyncio.CancelledError:
                        pass
                await store.close()

    async def test_normalized_url_and_exact_title_are_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.sqlite3"))
            await store.open()
            try:
                self.assertFalse(
                    await store.record_story(
                        "Source A",
                        "Central Bank Raises Rates!",
                        "https://www.example.com/story/?utm_source=feed",
                    )
                )
                cursor = await store._connection().execute(
                    """
                    SELECT url_hash, title_hash, content_hash,
                           normalized_url, normalized_title
                    FROM seen_stories
                    """
                )
                row = await cursor.fetchone()
                await cursor.close()
                self.assertEqual(len(row[0]), 64)
                self.assertEqual(len(row[1]), 64)
                self.assertEqual(len(row[2]), 64)
                self.assertEqual(row[3], "https://example.com/story")
                self.assertEqual(row[4], "central bank raises rates")

                self.assertTrue(
                    await store.record_story(
                        "Source B",
                        "Updated wording",
                        "https://example.com/story",
                    )
                )
                self.assertTrue(
                    await store.record_story(
                        "Source B",
                        "central bank raises rates",
                        "https://example.com/another-story",
                    )
                )
            finally:
                await store.close()

    async def test_near_duplicate_titles_are_suppressed_across_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.sqlite3"))
            await store.open()
            try:
                self.assertFalse(
                    await store.record_story(
                        "Source A",
                        "Central bank cuts interest rates by 25 basis points",
                        "https://example.com/story-base",
                    )
                )
                self.assertFalse(
                    await store.record_story(
                        "Source A",
                        "Central bank cuts interest rate by 25 basis points",
                        "https://example.com/story-same-source",
                    )
                )
                self.assertTrue(
                    await store.record_story(
                        "Source B",
                        "Central bank cuts interest rates by 25 basis point",
                        "https://example.com/story-one",
                    )
                )
            finally:
                await store.close()
