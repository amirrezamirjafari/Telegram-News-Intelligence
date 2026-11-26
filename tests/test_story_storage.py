from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import aiosqlite

from bot.storage import StateStore


class StoryMetadataStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_in_progress_stage_recovers_as_observable_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "interrupted.sqlite3")
            store = StateStore(database_path)
            await store.open()
            await store.record_story(
                "Example",
                "Interrupted synthetic story",
                "https://example.com/interrupted",
                "Short source summary.",
                market="macro",
            )
            await store._connection().execute(
                """
                UPDATE seen_stories
                SET gemini_status = 'in_progress'
                WHERE link = 'https://example.com/interrupted'
                """
            )
            await store._connection().commit()
            await store.close()

            recovered = StateStore(database_path)
            await recovered.open()
            try:
                self.assertEqual(await recovered.pending_stories(), [])
                counts = await recovered.story_counts()
                self.assertEqual(counts.failed_analysis, 1)
                self.assertEqual(counts.pending_analysis, 0)
                cursor = await recovered._connection().execute(
                    """
                    SELECT gemini_status, analysis_status
                    FROM seen_stories WHERE link = 'https://example.com/interrupted'
                    """
                )
                self.assertEqual(await cursor.fetchone(), ("failed", "failed"))
                await cursor.close()
            finally:
                await recovered.close()

    async def test_persists_title_link_and_caps_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.sqlite3"))
            await store.open()
            try:
                title = "Federal Reserve signals a policy change"
                link = "https://example.com/story?ref=feed"
                summary = "Short summary. " + ("extra details " * 60)
                await store.record_story(
                    "CNBC",
                    title,
                    link,
                    summary,
                    market="macro",
                    published_at="2025-01-02T03:04:00+00:00",
                )

                cursor = await store._connection().execute(
                    """
                    SELECT title, summary, link, market, published_at, analysis_status
                    FROM seen_stories
                    """
                )
                row = await cursor.fetchone()
                await cursor.close()

                self.assertEqual(row[0], title)
                self.assertEqual(len(row[1]), 500)
                self.assertEqual(row[2], link)
                self.assertEqual(row[3:5], ("macro", "2025-01-02T03:04:00+00:00"))
                self.assertEqual(row[5], "pending")
            finally:
                await store.close()

    async def test_additive_migration_backfills_existing_story_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "legacy.sqlite3")
            connection = await aiosqlite.connect(database_path)
            await connection.execute(
                """
                CREATE TABLE seen_stories (
                    url_hash TEXT PRIMARY KEY,
                    title_hash TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    normalized_url TEXT NOT NULL,
                    normalized_title TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    analysis_status TEXT,
                    analysis_json TEXT,
                    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await connection.execute(
                """
                INSERT INTO seen_stories
                    (url_hash, title_hash, content_hash, normalized_url,
                     normalized_title, source_name)
                VALUES ('url-hash', 'title-hash', 'content-hash',
                        'https://example.com/story', 'normalized headline', 'Source')
                """
            )
            await connection.execute(
                """
                INSERT INTO seen_stories
                    (url_hash, title_hash, content_hash, normalized_url,
                     normalized_title, source_name, analysis_status, analysis_json)
                VALUES ('legacy-processed', 'legacy-title', 'legacy-content',
                        'https://example.com/old-story', 'old headline', 'Source',
                        'deferred', '{}')
                """
            )
            await connection.commit()
            await connection.close()

            store = StateStore(database_path)
            await store.open()
            try:
                cursor = await store._connection().execute(
                    """
                    SELECT title, summary, link, analysis_status,
                           gemini_status, deepseek_status
                    FROM seen_stories
                    WHERE url_hash = 'url-hash'
                    """
                )
                row = await cursor.fetchone()
                await cursor.close()
                self.assertEqual(
                    row,
                    (
                        "normalized headline",
                        None,
                        "https://example.com/story",
                        "seeded",
                        "skipped",
                        "skipped",
                    ),
                )
                cursor = await store._connection().execute(
                    """
                    SELECT gemini_status, deepseek_status
                    FROM seen_stories WHERE url_hash = 'legacy-processed'
                    """
                )
                self.assertEqual(await cursor.fetchone(), ("skipped", "skipped"))
                await cursor.close()
                self.assertEqual(await store.pending_stories(), [])
            finally:
                await store.close()
