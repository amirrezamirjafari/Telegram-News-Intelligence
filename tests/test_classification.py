from __future__ import annotations

import unittest

from bot.classification import classify_entry, classify_market
from bot.fetchers.base import FeedEntry
from bot.fetchers.rss import RssFetcher


class MarketClassificationTests(unittest.TestCase):
    def test_currency_pair_gold_and_dxy_are_forex(self) -> None:
        self.assertEqual(classify_market("EUR/USD rises as the DXY falls"), "forex")
        self.assertEqual(
            classify_market("Gold and XAU/USD rally in Asian trading"), "forex"
        )
        self.assertEqual(classify_market("USD strengthens against peers"), "forex")

    def test_central_bank_and_macro_data_are_macro(self) -> None:
        self.assertEqual(
            classify_market("Federal Reserve holds interest rates after CPI"),
            "macro",
        )
        self.assertEqual(classify_market("US non-farm payrolls miss estimates"), "macro")

    def test_major_indices_and_stock_stories_are_global_stock(self) -> None:
        self.assertEqual(
            classify_market("S&P 500 rises as Apple shares gain"),
            "global_stock",
        )
        self.assertEqual(classify_market("Nvidia earnings beat expectations"), "global_stock")

    def test_irrelevant_story_is_dropped_and_summary_can_classify(self) -> None:
        irrelevant = FeedEntry(
            "Local council approves a new park", "https://example.com/local"
        )
        self.assertIsNone(classify_entry(irrelevant))

        from_summary = FeedEntry(
            "Markets update",
            "https://example.com/markets",
            summary="The European Central Bank announced a rate decision.",
        )
        classified = classify_entry(from_summary)
        self.assertIsNotNone(classified)
        self.assertEqual(classified.market, "macro")


class RssSummaryTests(unittest.TestCase):
    def test_parser_keeps_a_short_feed_summary_for_filtering(self) -> None:
        fetcher = object.__new__(RssFetcher)
        entries, _ = fetcher.parse_entries(
            b'<?xml version="1.0"?><rss version="2.0"><channel>'
            b"<item><title>Markets update</title>"
            b"<link>https://example.com/markets</link>"
            b"<description>Federal Reserve announces an interest rate decision.</description>"
            b"</item></channel></rss>"
        )
        self.assertEqual(
            entries[0].summary,
            "Federal Reserve announces an interest rate decision.",
        )
