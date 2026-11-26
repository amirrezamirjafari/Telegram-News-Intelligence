from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.config import (
    DEFAULT_DEEPSEEK_DAILY_CALL_CAP,
    DEFAULT_DEEPSEEK_BASE_URL,
    DEFAULT_DEEPSEEK_MAX_OUTPUT_TOKENS,
    DEFAULT_GEMINI_CALLS_PER_CYCLE,
    DEFAULT_GEMINI_MAX_OUTPUT_TOKENS,
    Settings,
)
from bot.sources import SOURCES


class ConfigAndSourceTests(unittest.TestCase):
    def test_legacy_api_key_cannot_replace_both_provider_keys(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "test-token",
                "OWNER_CHAT_ID": "123",
                "API_KEY": "legacy-manageit-key",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "GEMINI_API_KEY is required"):
                Settings.from_env()

    def test_db_path_can_be_configured_for_persistent_volume(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "test-token",
                "OWNER_CHAT_ID": "123",
                "GEMINI_API_KEY": "test-gemini-key",
                "DEEPSEEK_API_KEY": "test-deepseek-key",
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

    def test_provider_models_and_budgets_can_be_overridden(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "test-token",
                "OWNER_CHAT_ID": "123",
                "GEMINI_API_KEY": "test-gemini-key",
                "DEEPSEEK_API_KEY": "test-deepseek-key",
                "GEMINI_MODEL": "custom-gemini",
                "DEEPSEEK_MODEL": "custom-deepseek",
                "DEEPSEEK_BASE_URL": "https://custom.example/v1/",
                "GEMINI_CALLS_PER_CYCLE": "7",
                "DEEPSEEK_DAILY_CALL_CAP": "3",
                "GEMINI_MAX_OUTPUT_TOKENS": "300",
                "DEEPSEEK_MAX_OUTPUT_TOKENS": "400",
            },
            clear=True,
        ):
            settings = Settings.from_env()

        self.assertEqual(settings.gemini_model, "custom-gemini")
        self.assertEqual(settings.deepseek_model, "custom-deepseek")
        self.assertEqual(settings.deepseek_base_url, "https://custom.example/v1")
        self.assertEqual(settings.gemini_calls_per_cycle, 7)
        self.assertEqual(settings.deepseek_daily_call_cap, 3)
        self.assertEqual(settings.gemini_max_output_tokens, 300)
        self.assertEqual(settings.deepseek_max_output_tokens, 400)

    def test_provider_models_and_budgets_have_safe_defaults(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "test-token",
                "OWNER_CHAT_ID": "123",
                "GEMINI_API_KEY": "test-gemini-key",
                "DEEPSEEK_API_KEY": "test-deepseek-key",
            },
            clear=True,
        ):
            settings = Settings.from_env()

        self.assertEqual(settings.gemini_model, "gemini-3.5-flash-lite")
        self.assertEqual(settings.deepseek_model, "manageit/deepseek-v4.1-flash")
        self.assertEqual(settings.deepseek_base_url, DEFAULT_DEEPSEEK_BASE_URL)
        self.assertEqual(settings.gemini_calls_per_cycle, DEFAULT_GEMINI_CALLS_PER_CYCLE)
        self.assertEqual(
            settings.deepseek_daily_call_cap, DEFAULT_DEEPSEEK_DAILY_CALL_CAP
        )
        self.assertEqual(
            settings.gemini_max_output_tokens, DEFAULT_GEMINI_MAX_OUTPUT_TOKENS
        )
        self.assertEqual(
            settings.deepseek_max_output_tokens, DEFAULT_DEEPSEEK_MAX_OUTPUT_TOKENS
        )

    def test_deepseek_base_url_requires_https_without_embedded_credentials(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "test-token",
                "OWNER_CHAT_ID": "123",
                "GEMINI_API_KEY": "test-gemini-key",
                "DEEPSEEK_API_KEY": "test-deepseek-key",
                "DEEPSEEK_BASE_URL": "http://example.test/v1",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "DEEPSEEK_BASE_URL"):
                Settings.from_env()

    def test_all_sources_use_supported_market_categories(self) -> None:
        allowed_markets = {"forex", "global_stock", "macro"}
        self.assertTrue(SOURCES)
        self.assertTrue(all(source.market in allowed_markets for source in SOURCES))
