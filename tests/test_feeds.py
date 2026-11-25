from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from bot.fetchers.base import FeedEntry, FetchResult, Source
from bot.fetchers.rss import RssFetcher
from bot.storage import StateStore


class FeedFetcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_conditional_header_and_parses_only_titles_and_links(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.sqlite3"))
            await store.open()
            fetcher = RssFetcher(store)
            try:
                await store.record_success("Example", '"cached"', "yesterday")
                await store.mark_seed_complete()
                response = httpx.Response(
                    200,
                    headers={"ETag": '"fresh"'},
                    content=(
                        b'<?xml version="1.0"?><rss version="2.0"><channel>'
                        b"<item><title>Dollar rises</title>"
                        b"<link>https://example.com/story</link>"
                        b"<description>Short feed summary</description>"
                        b"<pubDate>Wed, 02 Oct 2002 13:00:00 GMT</pubDate></item>"
                        b"</channel></rss>"
                    ),
                    request=httpx.Request("GET", "https://example.com/feed"),
                )
                with patch.object(
                    fetcher._client, "get", new_callable=AsyncMock, return_value=response
                ) as get:
                    result = await fetcher.fetch(
                        Source("Example", "forex", "rss", "https://example.com/feed")
                    )

                self.assertEqual(
                    result.entries,
                    (
                        FeedEntry(
                            "Dollar rises",
                            "https://example.com/story",
                            "Short feed summary",
                            published_at=datetime(
                                2002, 10, 2, 13, tzinfo=timezone.utc
                            ),
                        ),
                    ),
                )
                self.assertEqual(
                    get.await_args.kwargs["headers"]["If-None-Match"], '"cached"'
                )
                self.assertEqual((await store.get("Example")).etag, '"fresh"')
            finally:
                await fetcher.close()
                await store.close()

    async def test_403_is_recorded_without_raising_or_retrying(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.sqlite3"))
            await store.open()
            fetcher = RssFetcher(store)
            try:
                response = httpx.Response(
                    403,
                    request=httpx.Request("GET", "https://example.com/feed"),
                )
                with patch.object(
                    fetcher._client, "get", new_callable=AsyncMock, return_value=response
                ) as get:
                    result = await fetcher.fetch(
                        Source(
                            "Example",
                            "global_stock",
                            "rss",
                            "https://example.com/feed",
                        )
                    )

                self.assertFalse(result.disabled)
                self.assertEqual((await store.get("Example")).consecutive_failures, 1)
                get.assert_awaited_once()
            finally:
                await fetcher.close()
                await store.close()
