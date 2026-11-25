from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.config import Settings
from bot.sources import SOURCES


class ConfigAndSourceTests(unittest.TestCase):
    def test_db_path_can_be_configured_for_persistent_volume(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "test-token",
                "OWNER_CHAT_ID": "123",
                "API_KEY": "test-api-key",
                "DB_PATH": "/mnt/storage/newsbot.sqlite3",
            },
            clear=True,
        ):
            settings = Settings.from_env()

        self.assertEqual(settings.db_path, Path("/mnt/storage/newsbot.sqlite3"))
        self.assertEqual(
            settings.calendar_feed_url,
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
        )
        self.assertEqual(settings.calendar_reminder_minutes, 30)

    def test_all_sources_use_supported_market_categories(self) -> None:
        allowed_markets = {"forex", "global_stock", "macro"}
        self.assertTrue(SOURCES)
        self.assertTrue(all(source.market in allowed_markets for source in SOURCES))
