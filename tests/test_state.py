from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import aiosqlite

from bot.storage import FAILURE_LIMIT, StateStore


class StateStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_feed_disables_after_five_consecutive_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.sqlite3"))
            await store.open()
            try:
                for failure_number in range(1, FAILURE_LIMIT + 1):
                    state = await store.record_failure("test-feed")
                    self.assertEqual(state.consecutive_failures, failure_number)
                    self.assertEqual(state.disabled, failure_number == FAILURE_LIMIT)
                    self.assertIsNotNone(state.last_fetched_at)
                    self.assertEqual(state.last_error, None)
            finally:
                await store.close()

    async def test_success_resets_failures_and_saves_conditional_headers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.sqlite3"))
            await store.open()
            try:
                await store.record_failure("test-feed")
                await store.record_success("test-feed", '"tag"', "yesterday")
                state = await store.get("test-feed")
                self.assertEqual(state.consecutive_failures, 0)
                self.assertEqual(state.etag, '"tag"')
                self.assertEqual(state.last_modified, "yesterday")
                self.assertIsNotNone(state.last_fetched_at)
                self.assertIsNotNone(state.last_success_at)
                self.assertIsNone(state.last_error)
            finally:
                await store.close()

    async def test_source_fetch_status_columns_migrate_additively(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "legacy.sqlite3")
            connection = await aiosqlite.connect(db_path)
            await connection.execute(
                """
                CREATE TABLE source_state (
                    source_name TEXT PRIMARY KEY,
                    etag TEXT,
                    last_modified TEXT,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    disabled INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            await connection.commit()
            await connection.close()

            store = StateStore(db_path)
            await store.open()
            try:
                state = await store.record_failure("legacy-feed", "HTTP 403")
                self.assertEqual(state.last_error, "HTTP 403")
                self.assertIsNotNone(state.last_fetched_at)
            finally:
                await store.close()
