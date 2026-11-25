from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import httpx

from bot.storage import StateStore

SourceType = Literal["rss", "google_news"]
MarketType = Literal["forex", "global_stock", "macro"]

LOGGER = logging.getLogger(__name__)
USER_AGENT = (
    "Mozilla/5.0 (compatible; TelegramNewsIntelligence/1.0; "
    "+https://github.com/amirrezamirjafari/Telegram-News-Intelligence)"
)
MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class Source:
    name: str
    market: MarketType
    type: SourceType
    url: str
    enabled: bool = True
    verification_note: str | None = None


@dataclass(frozen=True)
class FeedEntry:
    title: str
    link: str
    summary: str | None = None
    market: MarketType | None = None
    published_at: datetime | None = None


@dataclass(frozen=True)
class FetchResult:
    source: Source
    entries: tuple[FeedEntry, ...]
    disabled: bool = False
    succeeded: bool = True


class BaseFetcher:
    source_type: SourceType

    def __init__(self, state_store: StateStore) -> None:
        self._state_store = state_store
        self._client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=httpx.Timeout(15.0, connect=8.0),
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch(self, source: Source) -> FetchResult:
        if source.type != self.source_type:
            raise ValueError("Fetcher does not support this source type")

        state = await self._state_store.get(source.name)
        if not source.enabled:
            return FetchResult(source, (), succeeded=True)
        if state.disabled:
            return FetchResult(source, (), disabled=True, succeeded=True)

        headers: dict[str, str] = {}
        if await self._state_store.seed_complete():
            if state.etag:
                headers["If-None-Match"] = state.etag
            if state.last_modified:
                headers["If-Modified-Since"] = state.last_modified

        try:
            response = await self._get_with_retries(source.url, headers)
            if response.status_code == httpx.codes.NOT_MODIFIED:
                await self._state_store.record_not_modified(source.name)
                return FetchResult(source, ())

            entries, malformed = self.parse_entries(response.content)
            if malformed and not entries:
                raise ValueError("response was not a readable feed")

            await self._state_store.record_success(
                source.name,
                response.headers.get("ETag"),
                response.headers.get("Last-Modified"),
            )
            if malformed:
                LOGGER.warning("Skipped invalid entries from %s", source.name)
            return FetchResult(source, entries)
        except (httpx.HTTPError, ValueError) as exc:
            state = await self._state_store.record_failure(
                source.name, self._failure_reason(exc)
            )
            LOGGER.warning(
                "Feed fetch failed for %s (%d/%d consecutive failures; %s)",
                source.name,
                state.consecutive_failures,
                5,
                self._failure_reason(exc),
            )
            if state.disabled:
                LOGGER.error(
                    "Disabled feed after five consecutive failures: %s", source.name
                )
            return FetchResult(
                source,
                (),
                disabled=state.disabled,
                succeeded=False,
            )

    async def _get_with_retries(
        self, url: str, headers: dict[str, str]
    ) -> httpx.Response:
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = await self._client.get(url, headers=headers)
                if response.status_code == httpx.codes.NOT_MODIFIED:
                    return response
                response.raise_for_status()
                return response
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                if status_code != 429 and status_code < 500:
                    raise
                if attempt == MAX_ATTEMPTS - 1:
                    raise
            except httpx.TransportError:
                if attempt == MAX_ATTEMPTS - 1:
                    raise

            await asyncio.sleep(0.5 * (2**attempt))
        raise RuntimeError("unreachable retry state")

    @staticmethod
    def _failure_reason(error: Exception) -> str:
        if isinstance(error, httpx.HTTPStatusError):
            return f"HTTP {error.response.status_code}"
        if isinstance(error, httpx.TimeoutException):
            return "request timed out"
        if isinstance(error, httpx.TransportError):
            return "connection error"
        return "invalid feed response"

    def parse_entries(self, content: bytes) -> tuple[tuple[FeedEntry, ...], bool]:
        raise NotImplementedError
