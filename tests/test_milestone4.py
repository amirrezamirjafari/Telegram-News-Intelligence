from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from bot.analyzer import AnalysisError, NewsAnalysis, NewsAnalyzer
from bot.fetchers.base import FeedEntry, FetchResult, Source
from bot.formatter import format_analysis_message
from bot.scheduler import OwnerMessageQueue, fetch_and_enqueue
from bot.storage import StateStore


def valid_analysis_payload() -> dict[str, object]:
    return {
        "summary_fa": "بازار ارز امروز واکنش نشان داد. معامله‌گران نرخ‌ها را دنبال می‌کنند.",
        "assets": ["EUR/USD", "طلا"],
        "sentiment": "neutral",
        "impact": "medium",
        "reasoning_fa": "تغییر نرخ بهره می‌تواند بر ارزها اثر بگذارد.",
    }


def completion_response(content: str, request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": content}}]},
        request=request,
    )


class AnalysisClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_json_retries_once_and_sends_only_title_and_summary(self) -> None:
        contents = ["not json", json.dumps(valid_analysis_payload(), ensure_ascii=False)]
        requests: list[dict[str, object]] = []
        authorization_headers: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            authorization_headers.append(request.headers["Authorization"])
            return completion_response(contents.pop(0), request)

        analyzer = NewsAnalyzer(
            "secret-test-key",
            "test-model",
            transport=httpx.MockTransport(handler),
        )
        try:
            result = await analyzer.analyze("Fed rate decision", "Rates may change.")
        finally:
            await analyzer.close()

        self.assertEqual(len(requests), 2)
        self.assertEqual(authorization_headers, ["Bearer secret-test-key"] * 2)
        self.assertEqual(requests[0]["model"], "test-model")
        user_message = requests[0]["messages"][1]["content"]
        self.assertEqual(
            json.loads(user_message),
            {"title": "Fed rate decision", "summary": "Rates may change."},
        )
        self.assertEqual(result.impact, "medium")
        self.assertEqual(result.assets, ("EUR/USD", "طلا"))

    async def test_invalid_schema_retries_once_then_fails(self) -> None:
        invalid = valid_analysis_payload()
        invalid["impact"] = "urgent"
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return completion_response(
                json.dumps(invalid, ensure_ascii=False),
                request,
            )

        analyzer = NewsAnalyzer(
            "secret-test-key",
            "test-model",
            transport=httpx.MockTransport(handler),
        )
        try:
            with self.assertRaises(AnalysisError):
                await analyzer.analyze("title", "summary")
        finally:
            await analyzer.close()
        self.assertEqual(calls, 2)


class FormatterTests(unittest.TestCase):
    def test_escapes_dynamic_html_and_formats_all_analysis_fields(self) -> None:
        analysis = NewsAnalysis(
            summary_fa="بازار <سریع> واکنش نشان داد. دلار & یورو تغییر کردند.",
            assets=("<EUR/USD>",),
            sentiment="negative",
            impact="high",
            reasoning_fa="دلیل <متن> & اطلاعات.",
        )

        message = format_analysis_message(
            title="<Headline & details>",
            market="forex",
            source_name="News <source> & more",
            published_at="2025-01-02T03:04:00+03:30",
            analysis=analysis,
            link="https://example.com/story?a=1&b=2",
        )

        self.assertIn("&lt;Headline &amp; details&gt;", message)
        self.assertIn("News &lt;source&gt; &amp; more", message)
        self.assertIn("&lt;سریع&gt;", message)
        self.assertIn("&lt;EUR/USD&gt;", message)
        self.assertIn("&lt;متن&gt; &amp; اطلاعات.", message)
        self.assertIn("2025-01-01 23:34 UTC", message)
        self.assertIn("🔴 زیاد", message)
        self.assertIn("منفی", message)
        self.assertIn(
            'href="https://example.com/story?a=1&amp;b=2"',
            message,
        )
        self.assertIn("توصیهٔ سرمایه‌گذاری نیست", message)
        self.assertNotIn("<Headline", message)


class LowImpactRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_low_impact_analysis_is_saved_without_immediate_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.sqlite3"))
            await store.open()
            await store.mark_seed_complete()
            source = Source("Market News", "macro", "rss", "https://example.com/feed")
            entry = FeedEntry(
                "Federal Reserve policy update",
                "https://example.com/story",
                "The Federal Reserve discussed interest rates.",
            )

            class Fetcher:
                async def fetch(self, requested_source: Source) -> FetchResult:
                    return FetchResult(requested_source, (entry,))

            class Analyzer:
                def __init__(self) -> None:
                    self.calls: list[tuple[str, str | None]] = []

                async def analyze(
                    self, title: str, summary: str | None
                ) -> NewsAnalysis:
                    self.calls.append((title, summary))
                    return NewsAnalysis(
                        summary_fa="خبر اثر محدودی دارد. روندهای بازار را دنبال کنید.",
                        assets=("USD",),
                        sentiment="neutral",
                        impact="low",
                        reasoning_fa="داده‌ها اثر محدودی بر بازار دارند.",
                    )

            class RecordingBot:
                def __init__(self) -> None:
                    self.messages: list[str] = []

                async def send_message(
                    self, chat_id: int, text: str, **kwargs: object
                ) -> None:
                    self.messages.append(text)

            analyzer = Analyzer()
            bot = RecordingBot()
            queue = OwnerMessageQueue(bot, owner_chat_id=123)  # type: ignore[arg-type]
            delivery_task = asyncio.create_task(queue.run(interval_seconds=0))
            try:
                await fetch_and_enqueue(
                    {"rss": Fetcher()},  # type: ignore[dict-item]
                    (source,),
                    queue,
                    store,
                    analyzer,  # type: ignore[arg-type]
                )
                await queue.stop()
                await delivery_task

                cursor = await store._connection().execute(
                    "SELECT analysis_json, analysis_status FROM seen_stories"
                )
                row = await cursor.fetchone()
                await cursor.close()

                self.assertEqual(bot.messages, [])
                self.assertEqual(analyzer.calls, [(entry.title, entry.summary)])
                self.assertEqual(row[1], "deferred")
                self.assertEqual(json.loads(row[0])["impact"], "low")
            finally:
                if not delivery_task.done():
                    delivery_task.cancel()
                    try:
                        await delivery_task
                    except asyncio.CancelledError:
                        pass
                await store.close()
