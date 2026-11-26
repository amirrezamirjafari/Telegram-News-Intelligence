from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from bot.analyzer import InvalidAnalysisResponse, NewsAnalysis
from bot.fetchers.base import FeedEntry, FetchResult, Source
from bot.formatter import format_analysis_message
from bot.pipeline import (
    DeepSeekAssessment,
    DeepSeekAnalyzer,
    GeminiAnalyzer,
    NewsPipeline,
    ProviderResponseError,
)
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
    async def test_gemini_structured_request_and_validation(self) -> None:
        requests: list[dict[str, object]] = []
        api_keys: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            api_keys.append(request.headers["x-goog-api-key"])
            content = json.dumps(
                {**valid_analysis_payload(), "confidence": 0.81},
                ensure_ascii=False,
            )
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {"content": {"parts": [{"text": content}]}}
                    ]
                },
                request=request,
            )

        analyzer = GeminiAnalyzer(
            "test-gemini-key",
            "gemini-test",
            512,
            transport=httpx.MockTransport(handler),
        )
        try:
            async def allowed() -> bool:
                return True

            result = await analyzer.analyze(
                "Fed rate decision", "Rates may change.", allowed
            )
        finally:
            await analyzer.close()

        self.assertEqual(len(requests), 1)
        self.assertEqual(api_keys, ["test-gemini-key"])
        self.assertTrue(requests[0]["generationConfig"]["responseSchema"])
        self.assertEqual(requests[0]["generationConfig"]["maxOutputTokens"], 512)
        user_message = requests[0]["contents"][0]["parts"][0]["text"]
        self.assertEqual(
            json.loads(user_message),
            {"title": "Fed rate decision", "summary": "Rates may change."},
        )
        self.assertEqual(result.impact, "medium")
        self.assertEqual(result.assets, ("EUR/USD", "طلا"))
        self.assertEqual(result.confidence, 0.81)

    async def test_invalid_gemini_schema_fails_without_fallback(self) -> None:
        invalid = valid_analysis_payload()
        invalid["impact"] = "urgent"
        calls = 0
        budget_calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": json.dumps(
                                            {**invalid, "confidence": 0.8},
                                            ensure_ascii=False,
                                        )
                                    }
                                ]
                            }
                        }
                    ]
                },
                request=request,
            )

        analyzer = GeminiAnalyzer(
            "test-gemini-key",
            "gemini-test",
            512,
            transport=httpx.MockTransport(handler),
        )
        try:
            async def allowed() -> bool:
                nonlocal budget_calls
                budget_calls += 1
                return True

            with self.assertRaises(InvalidAnalysisResponse):
                await analyzer.analyze("title", "summary", allowed)
        finally:
            await analyzer.close()
        self.assertEqual(calls, 2)
        self.assertEqual(budget_calls, 2)

    async def test_gemini_retries_invalid_schema_and_accepts_valid_second_response(self) -> None:
        calls = 0
        budget_calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            payload = {**valid_analysis_payload(), "confidence": 0.8}
            if calls == 1:
                payload["impact"] = "urgent"
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {"content": {"parts": [{"text": json.dumps(payload, ensure_ascii=False)}]}}
                    ]
                },
                request=request,
            )

        analyzer = GeminiAnalyzer(
            "test-gemini-key",
            "gemini-test",
            512,
            transport=httpx.MockTransport(handler),
        )
        try:
            async def allowed() -> bool:
                nonlocal budget_calls
                budget_calls += 1
                return True

            result = await analyzer.analyze("title", "summary", allowed)
        finally:
            await analyzer.close()
        self.assertEqual(result.impact, "medium")
        self.assertEqual(calls, 2)
        self.assertEqual(budget_calls, 2)

    async def test_invalid_gemini_response_is_failed_if_retry_budget_is_exhausted(self) -> None:
        calls = 0
        budget_calls = 0
        invalid = valid_analysis_payload()
        invalid["impact"] = "urgent"

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "text": json.dumps(
                                            {**invalid, "confidence": 0.8},
                                            ensure_ascii=False,
                                        )
                                    }
                                ]
                            }
                        }
                    ]
                },
                request=request,
            )

        analyzer = GeminiAnalyzer(
            "test-gemini-key",
            "gemini-test",
            512,
            transport=httpx.MockTransport(handler),
        )
        try:
            async def allowed_once() -> bool:
                nonlocal budget_calls
                budget_calls += 1
                return budget_calls == 1

            with self.assertRaises(InvalidAnalysisResponse):
                await analyzer.analyze("title", "summary", allowed_once)
        finally:
            await analyzer.close()
        self.assertEqual(calls, 1)
        self.assertEqual(budget_calls, 2)

    async def test_deepseek_transient_http_error_retries_once(self) -> None:
        calls = 0
        budget_calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(503, request=request)
            return completion_response(
                json.dumps(
                    {
                        "possible_impact_fa": "ممکن است در صورت تأیید خبر، نوسان ایجاد شود.",
                        "confidence": 0.6,
                        "caveat_fa": "این سناریو تأییدنشده است.",
                    },
                    ensure_ascii=False,
                ),
                request,
            )

        analyzer = DeepSeekAnalyzer(
            "test-deepseek-key",
            "deepseek-test",
            512,
            transport=httpx.MockTransport(handler),
        )
        try:
            async def allowed() -> bool:
                nonlocal budget_calls
                budget_calls += 1
                return True

            assessment = await analyzer.analyze("source title", "source summary", allowed)
        finally:
            await analyzer.close()
        self.assertEqual(calls, 2)
        self.assertEqual(budget_calls, 2)
        self.assertEqual(assessment.confidence, 0.6)

    async def test_deepseek_non_text_content_is_rejected_without_coercion(self) -> None:
        calls = 0
        budget_calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": {"text": "not accepted"}}}]},
                request=request,
            )

        analyzer = DeepSeekAnalyzer(
            "test-deepseek-key",
            "deepseek-test",
            512,
            transport=httpx.MockTransport(handler),
        )
        try:
            async def allowed() -> bool:
                nonlocal budget_calls
                budget_calls += 1
                return True

            with self.assertRaises(ProviderResponseError):
                await analyzer.analyze("source title", "source summary", allowed)
        finally:
            await analyzer.close()
        self.assertEqual(calls, 2)
        self.assertEqual(budget_calls, 2)

    async def test_deepseek_retries_invalid_json_and_validates_second_response(self) -> None:
        calls = 0
        budget_calls = 0
        requests: list[tuple[str, str]] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            body = json.loads(request.content)
            requests.append((str(request.url), body["model"]))
            if calls == 1:
                return completion_response("not valid json", request)
            return completion_response(
                json.dumps(
                    {
                        "possible_impact_fa": "ممکن است در صورت تأیید خبر، نوسان ایجاد شود.",
                        "confidence": 0.65,
                        "caveat_fa": "این سناریو تأییدنشده است.",
                    },
                    ensure_ascii=False,
                ),
                request,
            )

        analyzer = DeepSeekAnalyzer(
            "test-deepseek-key",
            "manageit/deepseek-v4.1-flash",
            512,
            base_url="https://ai.manageit.dev/api/v1/",
            transport=httpx.MockTransport(handler),
        )
        try:
            async def allowed() -> bool:
                nonlocal budget_calls
                budget_calls += 1
                return True

            result = await analyzer.analyze("title", "summary", allowed)
        finally:
            await analyzer.close()
        self.assertEqual(result.confidence, 0.65)
        self.assertEqual(calls, 2)
        self.assertEqual(budget_calls, 2)
        self.assertEqual(
            requests,
            [
                (
                    "https://ai.manageit.dev/api/v1/chat/completions",
                    "manageit/deepseek-v4.1-flash",
                )
            ] * 2,
        )

    async def test_deepseek_validates_content_without_using_reasoning_content(self) -> None:
        content_json = json.dumps(
            {
                "possible_impact_fa": "ممکن است در سناریوی ساختگی نوسان ایجاد شود.",
                "confidence": 0.7,
                "caveat_fa": "این سناریوی ساختگی تأییدنشده است.",
            },
            ensure_ascii=False,
        )
        reasoning_json = json.dumps(
            {
                "possible_impact_fa": "این متن نباید به‌عنوان پاسخ استفاده شود.",
                "confidence": 0.99,
                "caveat_fa": "این مقدار نیز نباید استفاده شود.",
            },
            ensure_ascii=False,
        )

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": content_json,
                                "reasoning_content": reasoning_json,
                            },
                        }
                    ]
                },
                request=request,
            )

        analyzer = DeepSeekAnalyzer(
            "test-deepseek-key",
            "manageit/deepseek-v4.1-flash",
            512,
            base_url="https://ai.manageit.dev/api/v1",
            transport=httpx.MockTransport(handler),
        )
        try:
            async def allowed() -> bool:
                return True

            assessment = await analyzer.analyze("synthetic title", "synthetic summary", allowed)
        finally:
            await analyzer.close()
        self.assertEqual(
            assessment.possible_impact_fa,
            "ممکن است در سناریوی ساختگی نوسان ایجاد شود.",
        )
        self.assertEqual(assessment.confidence, 0.7)

    async def test_deepseek_schema_failure_stops_after_one_retry(self) -> None:
        calls = 0
        budget_calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return completion_response(
                json.dumps(
                    {
                        "possible_impact_fa": "unsupported english output",
                        "confidence": 0.65,
                        "caveat_fa": "این سناریو تأییدنشده است.",
                    },
                    ensure_ascii=False,
                ),
                request,
            )

        analyzer = DeepSeekAnalyzer(
            "test-deepseek-key",
            "deepseek-test",
            512,
            transport=httpx.MockTransport(handler),
        )
        try:
            async def allowed() -> bool:
                nonlocal budget_calls
                budget_calls += 1
                return True

            with self.assertRaises(InvalidAnalysisResponse):
                await analyzer.analyze("title", "summary", allowed)
        finally:
            await analyzer.close()
        self.assertEqual(calls, 2)
        self.assertEqual(budget_calls, 2)

    async def test_pipeline_persists_provider_failures_without_rebill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "provider-error.sqlite3"))
            await store.open()
            title = "Synthetic provider failure story"
            await store.record_story(
                "Test source",
                title,
                "https://example.test/provider-error",
                "Short synthetic source summary.",
                market="macro",
            )

            class FailingGemini:
                calls = 0

                async def analyze(self, title: str, summary: str | None, budget: object):
                    for _ in range(2):
                        if not await budget():  # type: ignore[operator]
                            break
                        self.calls += 1
                    raise InvalidAnalysisResponse("Gemini schema remained invalid")

                async def close(self) -> None:
                    return None

            class UnusedDeepSeek:
                async def analyze(self, *args: object) -> DeepSeekAssessment:
                    raise AssertionError("DeepSeek must not run after Gemini failure")

                async def close(self) -> None:
                    return None

            class RecordingMessages:
                async def put(
                    self, message: str, parse_mode: str | None = None
                ) -> None:
                    raise AssertionError("Failed analysis must not be delivered")

            gemini = FailingGemini()
            pipeline = NewsPipeline(
                gemini,  # type: ignore[arg-type]
                UnusedDeepSeek(),  # type: ignore[arg-type]
                gemini_calls_per_cycle=3,
                deepseek_daily_call_cap=3,
            )
            try:
                await pipeline.process_pending_stories(store, RecordingMessages())
                await pipeline.process_pending_stories(store, RecordingMessages())
                pending = await store.pending_stories()
                self.assertEqual(gemini.calls, 2)
                self.assertEqual(pending, [])
                cursor = await store._connection().execute(
                    "SELECT gemini_status, analysis_status FROM seen_stories WHERE title = ?",
                    (title,),
                )
                self.assertEqual(await cursor.fetchone(), ("failed", "failed"))
                await cursor.close()
                counts = await store.story_counts()
                self.assertEqual(counts.failed_analysis, 1)
                self.assertEqual(counts.pending_analysis, 0)
            finally:
                await pipeline.close()
                await store.close()

    async def test_failed_deepseek_stage_is_not_left_pending_or_reprocessed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "deepseek-error.sqlite3"))
            await store.open()
            title = "Synthetic high-impact provider failure"
            await store.record_story(
                "Test source",
                title,
                "https://example.test/deepseek-error",
                "Short synthetic source summary.",
                market="macro",
            )

            class PassingGemini:
                async def analyze(self, title: str, summary: str | None, budget: object):
                    if not await budget():  # type: ignore[operator]
                        raise AssertionError("Gemini request budget should allow")
                    return NewsAnalysis(
                        summary_fa="منبع ادعای مهمی را مطرح می‌کند. این ادعا تأیید نشده است.",
                        assets=("USD",),
                        sentiment="neutral",
                        impact="high",
                        reasoning_fa="این ارزیابی فقط بر متن منبع متکی است.",
                        confidence=0.8,
                    )

                async def close(self) -> None:
                    return None

            class FailingDeepSeek:
                calls = 0

                async def analyze(self, title: str, summary: str | None, budget: object):
                    for _ in range(2):
                        if not await budget():  # type: ignore[operator]
                            break
                        self.calls += 1
                    raise InvalidAnalysisResponse("DeepSeek schema remained invalid")

                async def close(self) -> None:
                    return None

            class RecordingMessages:
                async def put(
                    self, message: str, parse_mode: str | None = None
                ) -> None:
                    raise AssertionError("Failed analysis must not be delivered")

            deepseek = FailingDeepSeek()
            pipeline = NewsPipeline(
                PassingGemini(),  # type: ignore[arg-type]
                deepseek,  # type: ignore[arg-type]
                gemini_calls_per_cycle=3,
                deepseek_daily_call_cap=3,
            )
            try:
                await pipeline.process_pending_stories(store, RecordingMessages())
                await pipeline.process_pending_stories(store, RecordingMessages())
                self.assertEqual(deepseek.calls, 2)
                self.assertEqual(await store.pending_stories(), [])
                cursor = await store._connection().execute(
                    "SELECT gemini_status, deepseek_status, analysis_status "
                    "FROM seen_stories WHERE title = ?",
                    (title,),
                )
                self.assertEqual(
                    await cursor.fetchone(), ("completed", "failed", "failed")
                )
                await cursor.close()
                counts = await store.story_counts()
                self.assertEqual(counts.failed_analysis, 1)
                self.assertEqual(counts.pending_analysis, 0)
            finally:
                await pipeline.close()
                await store.close()


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

            class Gemini:
                def __init__(self) -> None:
                    self.calls: list[tuple[str, str | None]] = []

                async def analyze(
                    self,
                    title: str,
                    summary: str | None,
                    before_request: object,
                ) -> NewsAnalysis:
                    await before_request()  # type: ignore[operator]
                    self.calls.append((title, summary))
                    return NewsAnalysis(
                        summary_fa="خبر اثر محدودی دارد. روندهای بازار را دنبال کنید.",
                        assets=("USD",),
                        sentiment="neutral",
                        impact="low",
                        reasoning_fa="داده‌ها اثر محدودی بر بازار دارند.",
                        confidence=0.8,
                    )

                async def close(self) -> None:
                    return None

            class DeepSeek:
                async def analyze(
                    self, title: str, summary: str | None, before_request: object
                ) -> DeepSeekAssessment:
                    raise AssertionError("DeepSeek must not receive low-impact stories")

                async def close(self) -> None:
                    return None

            class RecordingBot:
                def __init__(self) -> None:
                    self.messages: list[str] = []

                async def send_message(
                    self, chat_id: int, text: str, **kwargs: object
                ) -> None:
                    self.messages.append(text)

            analyzer = Gemini()
            pipeline = NewsPipeline(
                analyzer,  # type: ignore[arg-type]
                DeepSeek(),  # type: ignore[arg-type]
                gemini_calls_per_cycle=3,
                deepseek_daily_call_cap=3,
            )
            bot = RecordingBot()
            queue = OwnerMessageQueue(bot, owner_chat_id=123)  # type: ignore[arg-type]
            delivery_task = asyncio.create_task(queue.run(interval_seconds=0))
            try:
                await fetch_and_enqueue(
                    {"rss": Fetcher()},  # type: ignore[dict-item]
                    (source,),
                    queue,
                    store,
                    pipeline,
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
