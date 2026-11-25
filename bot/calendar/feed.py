from __future__ import annotations

import logging

import httpx

from bot.calendar.models import CalendarEvent
from bot.calendar.parser import CalendarParseError, parse_events
from bot.fetchers.base import USER_AGENT
from bot.storage import StateStore

LOGGER = logging.getLogger(__name__)


class CalendarFeedClient:
    def __init__(
        self,
        feed_url: str,
        state_store: StateStore,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._feed_url = feed_url
        self._state_store = state_store
        self._client = httpx.AsyncClient(
            headers={
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
            timeout=httpx.Timeout(20.0, connect=8.0),
            follow_redirects=True,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def refresh(self) -> tuple[CalendarEvent, ...]:
        try:
            response = await self._client.get(self._feed_url)
            response.raise_for_status()
            events = parse_events(response.content)
        except (httpx.HTTPError, CalendarParseError) as exc:
            LOGGER.warning(
                "Economic calendar feed unavailable (%s); using cached events",
                _failure_reason(exc),
            )
            return await self._state_store.calendar_events()

        await self._state_store.replace_calendar_events(events)
        return events


def _failure_reason(error: Exception) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTP {error.response.status_code}"
    if isinstance(error, httpx.TimeoutException):
        return "request timed out"
    if isinstance(error, httpx.TransportError):
        return "connection error"
    return "invalid feed response"
