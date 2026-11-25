from __future__ import annotations

import html
import re
from collections.abc import Mapping
from datetime import datetime, timezone

import feedparser

from bot.fetchers.base import BaseFetcher, FeedEntry


class RssFetcher(BaseFetcher):
    source_type = "rss"

    def parse_entries(self, content: bytes) -> tuple[tuple[FeedEntry, ...], bool]:
        parsed = feedparser.parse(content)
        entries = tuple(
            FeedEntry(
                title=title,
                link=link,
                summary=_summary(item),
                published_at=_published_at(item),
            )
            for item in parsed.entries
            if (title := _clean_text(item.get("title", "")))
            and (link := _clean_text(item.get("link", "")))
        )
        return entries, bool(parsed.bozo)


def _clean_text(value: object) -> str:
    text = html.unescape(str(value))
    text = re.sub(r"<[^>]*>", "", text)
    return " ".join(text.split()).strip()


def _summary(item: Mapping[str, object]) -> str | None:
    raw_summary = item.get("summary") or item.get("description")
    if not raw_summary:
        content = item.get("content", [])
        if (
            isinstance(content, list)
            and content
            and isinstance(content[0], Mapping)
        ):
            raw_summary = content[0].get("value", "")
    summary = _clean_text(raw_summary or "")[:500]
    return summary or None


def _published_at(item: Mapping[str, object]) -> datetime | None:
    for field in ("published_parsed", "updated_parsed"):
        value = item.get(field)
        if isinstance(value, tuple) and len(value) >= 6:
            try:
                return datetime(
                    *(int(part) for part in value[:6]), tzinfo=timezone.utc
                )
            except (TypeError, ValueError):
                continue
    return None
