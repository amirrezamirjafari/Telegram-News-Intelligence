from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bot.analyzer import NewsAnalysis
from bot.pipeline import (
    CallBudgetExceeded,
    DeepSeekAssessment,
    NewsPipeline,
)
from bot.storage import StateStore


class RecordingMessages:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def put(self, message: str, parse_mode: str | None = None) -> None:
        self.messages.append(message)


class FakeGemini:
    def __init__(self, impacts: dict[str, str]) -> None:
        self.impacts = impacts
        self.calls: list[tuple[str, str | None]] = []

    async def analyze(self, title: str, summary: str | None, budget: object):
        if not await budget():  # type: ignore[operator]
            raise CallBudgetExceeded("Gemini test budget exhausted")
        self.calls.append((title, summary))
        impact = self.impacts[title]
        return NewsAnalysis(
            summary_fa="منبع ادعای تغییر شرایط بازار را مطرح می‌کند. این ادعا مستقلاً تأیید نشده است.",
            assets=("USD",),
            sentiment="neutral",
            impact=impact,  # type: ignore[arg-type]
            reasoning_fa="این ارزیابی فقط بر متن منبع تکیه دارد.",
            confidence=0.72,
        )

    async def close(self) -> None:
        return None


class FakeDeepSeek:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def analyze(self, title: str, summary: str | None, budget: object):
        if not await budget():  # type: ignore[operator]
            raise CallBudgetExceeded("DeepSeek test budget exhausted")
        self.calls.append((title, summary))
        return DeepSeekAssessment(
            "ممکن است در صورت تأیید مستقل خبر، نوسان کوتاه‌مدت رخ دهد.",
            0.61,
            "منبع و این سناریو مستقلاً راستی‌آزمایی نشده‌اند.",
        )

    async def close(self) -> None:
        return None


class NewsPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StateStore(str(Path(self.temp_dir.name) / "pipeline.sqlite3"))
        await self.store.open()
        self.messages = RecordingMessages()

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self.temp_dir.cleanup()

    async def add_story(self, title: str, suffix: str) -> str:
        await self.store.record_story(
            "Test source",
            title,
            f"https://example.test/{suffix}",
            f"Original feed summary for {suffix}.",
            market="macro",
        )
        cursor = await self.store._connection().execute(
            "SELECT url_hash FROM seen_stories WHERE link = ?",
            (f"https://example.test/{suffix}",),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row[0]

    async def test_only_high_impact_reaches_deepseek_with_original_source(self) -> None:
        high_title = "High impact synthetic source"
        medium_title = "Medium impact synthetic source"
        await self.add_story(high_title, "high")
        await self.add_story(medium_title, "medium")
        gemini = FakeGemini({high_title: "high", medium_title: "medium"})
        deepseek = FakeDeepSeek()
        pipeline = NewsPipeline(
            gemini,
            deepseek,
            gemini_calls_per_cycle=5,
            deepseek_daily_call_cap=5,
        )

        await pipeline.process_pending_stories(self.store, self.messages)

        self.assertEqual(len(gemini.calls), 2)
        self.assertEqual(
            deepseek.calls,
            [(high_title, "Original feed summary for high.")],
        )
        self.assertEqual(len(self.messages.messages), 1)
        message = self.messages.messages[0]
        self.assertIn("اثر احتمالی", message)
        self.assertIn("راستی‌آزمایی نشده‌اند", message)
        self.assertIn("توصیهٔ سرمایه‌گذاری نیست", message)
        self.assertNotIn("Original feed summary", message)

        high = await self.store.pending_stories()
        self.assertEqual(high, [])
        cursor = await self.store._connection().execute(
            "SELECT analysis_status FROM seen_stories WHERE title = ?",
            (medium_title,),
        )
        self.assertEqual((await cursor.fetchone())[0], "deferred")
        await cursor.close()

    async def test_completed_stages_are_not_rebilled_after_duplicate_or_rerun(self) -> None:
        title = "High impact duplicate-safe source"
        url = "https://example.test/deduped"
        summary = "Original source summary."
        await self.store.record_story("Source", title, url, summary, market="macro")
        gemini = FakeGemini({title: "high"})
        deepseek = FakeDeepSeek()
        pipeline = NewsPipeline(
            gemini,
            deepseek,
            gemini_calls_per_cycle=4,
            deepseek_daily_call_cap=4,
        )

        await pipeline.process_pending_stories(self.store, self.messages)
        duplicate = await self.store.record_story(
            "Source", title, url, summary, market="macro"
        )
        await pipeline.process_pending_stories(self.store, self.messages)

        self.assertTrue(duplicate)
        self.assertEqual(len(gemini.calls), 1)
        self.assertEqual(len(deepseek.calls), 1)
        self.assertEqual(len(self.messages.messages), 1)

    async def test_deepseek_daily_cap_is_reserved_before_call_and_resumable(self) -> None:
        title_a = "High priority story A"
        title_b = "High priority story B"
        hash_a = await self.add_story(title_a, "cap-a")
        hash_b = await self.add_story(title_b, "cap-b")
        gemini = FakeGemini({title_a: "high", title_b: "high"})
        deepseek = FakeDeepSeek()
        pipeline = NewsPipeline(
            gemini,
            deepseek,
            gemini_calls_per_cycle=5,
            deepseek_daily_call_cap=1,
        )

        await pipeline.process_pending_stories(self.store, self.messages)
        self.assertEqual(len(deepseek.calls), 1)
        rows = {
            row[0]: (row[1], row[2])
            for row in await (await self.store._connection().execute(
                "SELECT url_hash, gemini_status, deepseek_status FROM seen_stories"
            )).fetchall()
        }
        self.assertEqual(rows[hash_a][0], "completed")
        self.assertEqual(rows[hash_b][0], "completed")
        completed_deepseek = [
            story_hash for story_hash in (hash_a, hash_b)
            if rows[story_hash][1] == "completed"
        ]
        waiting_hashes = [
            story_hash for story_hash in (hash_a, hash_b)
            if rows[story_hash][1] == "pending"
        ]
        self.assertEqual(len(completed_deepseek), 1)
        self.assertEqual(len(waiting_hashes), 1)

        await pipeline.process_pending_stories(self.store, self.messages)
        self.assertEqual(len(gemini.calls), 2)
        self.assertEqual(len(deepseek.calls), 1)
        pending = await self.store.pending_stories()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].url_hash, waiting_hashes[0])
        self.assertEqual(await self.store.reserve_daily_provider_call("deepseek", 1), False)

    async def test_gemini_cycle_cap_stops_new_stage_requests(self) -> None:
        first = "Cycle capped story one"
        second = "Cycle capped story two"
        await self.add_story(first, "cycle-a")
        await self.add_story(second, "cycle-b")
        gemini = FakeGemini({first: "medium", second: "medium"})
        pipeline = NewsPipeline(
            gemini,
            FakeDeepSeek(),
            gemini_calls_per_cycle=1,
            deepseek_daily_call_cap=3,
        )

        await pipeline.process_pending_stories(self.store, self.messages)

        self.assertEqual(len(gemini.calls), 1)
        self.assertEqual(len(await self.store.pending_stories()), 1)

    async def test_saved_analysis_keeps_each_stage_and_source_claims_unverified(self) -> None:
        title = "Persisted high impact source"
        await self.add_story(title, "persist")
        pipeline = NewsPipeline(
            FakeGemini({title: "high"}),
            FakeDeepSeek(),
            gemini_calls_per_cycle=2,
            deepseek_daily_call_cap=2,
        )
        await pipeline.process_pending_stories(self.store, self.messages)
        cursor = await self.store._connection().execute(
            """
            SELECT gemini_json, deepseek_json, analysis_json, analysis_status,
                   gemini_status, deepseek_status
            FROM seen_stories WHERE title = ?
            """,
            (title,),
        )
        row = await cursor.fetchone()
        await cursor.close()

        self.assertEqual(row[3:], ("processed", "completed", "completed"))
        self.assertEqual(json.loads(row[0])["confidence"], 0.72)
        self.assertEqual(json.loads(row[1])["deep_confidence"], 0.61)
        self.assertIn("possible_impact_fa", json.loads(row[2]))
