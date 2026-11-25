from __future__ import annotations

import json
import logging
from typing import Protocol

from bot.analyzer import NewsAnalysis, validate_analysis
from bot.formatter import format_analysis_message
from bot.storage import DeferredStory, StateStore

LOGGER = logging.getLogger(__name__)
DIGEST_MESSAGE_LIMIT = 3900


class MessageSink(Protocol):
    async def put(self, message: str, parse_mode: str | None = None) -> None: ...


async def send_deferred_digest(
    state_store: StateStore,
    messages: MessageSink,
) -> int:
    stories = await state_store.claim_deferred_stories()
    if not stories:
        return 0

    formatted: list[str] = []
    for story in stories:
        analysis = _parse_analysis(story.analysis_json)
        if analysis is not None:
            formatted.append(_format_story_with_analysis(story, analysis))
    for message in _chunk_messages(formatted):
        await messages.put(message, parse_mode="HTML")
    return len(formatted)


def _parse_analysis(value: str) -> NewsAnalysis | None:
    try:
        analysis = validate_analysis(json.loads(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        LOGGER.warning("Skipping a deferred story with invalid stored analysis")
        return None
    if analysis.impact != "low":
        LOGGER.warning("Skipping a deferred story that is not low impact")
        return None
    return analysis


def _format_story_with_analysis(
    story: DeferredStory, analysis: NewsAnalysis
) -> str:
    return format_analysis_message(
        title=story.title,
        market=story.market,
        source_name=story.source_name,
        published_at=story.published_at,
        analysis=analysis,
        link=story.link,
    )


def _chunk_messages(items: list[str]) -> list[str]:
    header = "<b>خلاصهٔ خبرهای کم‌تأثیر</b>"
    messages: list[str] = []
    current = header
    for item in items:
        candidate = f"{current}\n\n{item}"
        if len(candidate) > DIGEST_MESSAGE_LIMIT and current != header:
            messages.append(current)
            current = f"{header}\n\n{item}"
        else:
            current = candidate
    if current != header:
        messages.append(current)
    return messages
