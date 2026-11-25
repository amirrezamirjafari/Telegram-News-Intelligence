from __future__ import annotations

from bot.fetchers.base import SourceType
from bot.fetchers.rss import RssFetcher


class GoogleNewsFetcher(RssFetcher):
    source_type: SourceType = "google_news"
