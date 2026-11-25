from __future__ import annotations

import asyncio
import hashlib
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import aiosqlite

from bot.calendar.models import CalendarEvent

FAILURE_LIMIT = 5
TITLE_SIMILARITY_THRESHOLD = 0.9
MIN_TITLE_LENGTH_FOR_SIMILARITY = 20
TRACKING_PARAMETERS = {
    "at",
    "ceid",
    "fbclid",
    "gclid",
    "gl",
    "hl",
    "ocid",
    "source",
    "usg",
    "ved",
}


@dataclass(frozen=True)
class SourceState:
    etag: str | None
    last_modified: str | None
    consecutive_failures: int
    disabled: bool
    last_fetched_at: str | None = None
    last_success_at: str | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class PendingStory:
    url_hash: str
    title: str
    summary: str | None
    link: str
    source_name: str
    market: str | None
    published_at: str | None


@dataclass(frozen=True)
class DeferredStory:
    title: str
    summary: str | None
    link: str
    source_name: str
    market: str | None
    published_at: str | None
    analysis_json: str


@dataclass(frozen=True)
class StoryCounts:
    total: int
    pending_analysis: int
    processed: int
    deferred: int
    digest_delivered: int
    by_market: dict[str, int]


class StateStore:
    def __init__(self, path: str) -> None:
        self._path = path
        self._db: aiosqlite.Connection | None = None
        self._db_lock = asyncio.Lock()

    async def open(self) -> None:
        async with self._db_lock:
            self._db = await aiosqlite.connect(self._path)
            await self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS source_state (
                    source_name TEXT PRIMARY KEY,
                    etag TEXT,
                    last_modified TEXT,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    disabled INTEGER NOT NULL DEFAULT 0,
                    last_fetched_at TEXT,
                    last_success_at TEXT,
                    last_error TEXT
                )
                """
            )
            await self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS seen_stories (
                    url_hash TEXT PRIMARY KEY,
                    title_hash TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    normalized_url TEXT NOT NULL,
                    normalized_title TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    title TEXT,
                    summary TEXT,
                    link TEXT,
                    market TEXT,
                    published_at TEXT,
                    analysis_json TEXT,
                    analysis_status TEXT,
                    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor = await self._db.execute("PRAGMA table_info(seen_stories)")
            story_columns = {row[1] for row in await cursor.fetchall()}
            await cursor.close()
            for column in (
                "title",
                "summary",
                "link",
                "market",
                "published_at",
                "analysis_json",
                "analysis_status",
            ):
                if column not in story_columns:
                    await self._db.execute(
                        f"ALTER TABLE seen_stories ADD COLUMN {column} TEXT"
                    )
            await self._db.execute(
                """
                UPDATE seen_stories
                SET title = COALESCE(title, normalized_title),
                    link = COALESCE(link, normalized_url),
                    analysis_status = COALESCE(analysis_status, 'seeded')
                """
            )
            cursor = await self._db.execute("PRAGMA table_info(source_state)")
            source_columns = {row[1] for row in await cursor.fetchall()}
            await cursor.close()
            for column in ("last_fetched_at", "last_success_at", "last_error"):
                if column not in source_columns:
                    await self._db.execute(
                        f"ALTER TABLE source_state ADD COLUMN {column} TEXT"
                    )
            await self._db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_seen_stories_title_hash
                ON seen_stories(title_hash)
                """
            )
            await self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            await self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS calendar_events (
                    event_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    impact TEXT NOT NULL,
                    event_at TEXT NOT NULL,
                    actual TEXT,
                    forecast TEXT,
                    previous TEXT
                )
                """
            )
            await self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS calendar_notifications (
                    event_id TEXT NOT NULL,
                    notification_type TEXT NOT NULL,
                    notified_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (event_id, notification_type)
                )
                """
            )
            await self._db.commit()

    async def close(self) -> None:
        async with self._db_lock:
            if self._db is not None:
                await self._db.close()
                self._db = None

    async def get(self, source_name: str) -> SourceState:
        async with self._db_lock:
            return await self._get(source_name)

    async def _get(self, source_name: str) -> SourceState:
        cursor = await self._connection().execute(
            """
            SELECT etag, last_modified, consecutive_failures, disabled,
                   last_fetched_at, last_success_at, last_error
            FROM source_state WHERE source_name = ?
            """,
            (source_name,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return SourceState(None, None, 0, False)
        return SourceState(
            row[0],
            row[1],
            int(row[2]),
            bool(row[3]),
            row[4],
            row[5],
            row[6],
        )

    async def record_success(
        self, source_name: str, etag: str | None, last_modified: str | None
    ) -> None:
        async with self._db_lock:
            fetched_at = _now_utc()
            await self._connection().execute(
                """
                INSERT INTO source_state
                    (source_name, etag, last_modified,
                     consecutive_failures, disabled, last_fetched_at,
                     last_success_at, last_error)
                VALUES (?, ?, ?, 0, 0, ?, ?, NULL)
                ON CONFLICT(source_name) DO UPDATE SET
                    etag = excluded.etag,
                    last_modified = excluded.last_modified,
                    consecutive_failures = 0,
                    disabled = 0,
                    last_fetched_at = excluded.last_fetched_at,
                    last_success_at = excluded.last_success_at,
                    last_error = NULL
                """,
                (source_name, etag, last_modified, fetched_at, fetched_at),
            )
            await self._connection().commit()

    async def record_not_modified(self, source_name: str) -> None:
        async with self._db_lock:
            fetched_at = _now_utc()
            await self._connection().execute(
                """
                INSERT INTO source_state
                    (source_name, consecutive_failures, disabled,
                     last_fetched_at, last_success_at, last_error)
                VALUES (?, 0, 0, ?, ?, NULL)
                ON CONFLICT(source_name) DO UPDATE SET
                    consecutive_failures = 0,
                    disabled = 0,
                    last_fetched_at = excluded.last_fetched_at,
                    last_success_at = excluded.last_success_at,
                    last_error = NULL
                """,
                (source_name, fetched_at, fetched_at),
            )
            await self._connection().commit()

    async def record_failure(
        self, source_name: str, error: str | None = None
    ) -> SourceState:
        async with self._db_lock:
            db = self._connection()
            fetched_at = _now_utc()
            await db.execute(
                """
                INSERT INTO source_state
                    (source_name, consecutive_failures, disabled,
                     last_fetched_at, last_error)
                VALUES (?, 1, 0, ?, ?)
                ON CONFLICT(source_name) DO UPDATE SET
                    consecutive_failures = consecutive_failures + 1,
                    disabled = CASE
                        WHEN consecutive_failures + 1 >= ? THEN 1
                        ELSE disabled
                    END,
                    last_fetched_at = excluded.last_fetched_at,
                    last_error = excluded.last_error
                """,
                (source_name, fetched_at, error, FAILURE_LIMIT),
            )
            await db.commit()
            return await self._get(source_name)

    async def seed_complete(self) -> bool:
        async with self._db_lock:
            cursor = await self._connection().execute(
                "SELECT value FROM bot_metadata WHERE key = 'seed_complete'"
            )
            row = await cursor.fetchone()
            await cursor.close()
            return row is not None and row[0] == "true"

    async def mark_seed_complete(self) -> None:
        async with self._db_lock:
            await self._connection().execute(
                """
                INSERT INTO bot_metadata (key, value)
                VALUES ('seed_complete', 'true')
                ON CONFLICT(key) DO UPDATE SET value = 'true'
                """
            )
            await self._connection().commit()

    async def disable_notice_sent(self, source_name: str) -> bool:
        async with self._db_lock:
            cursor = await self._connection().execute(
                "SELECT value FROM bot_metadata WHERE key = ?",
                (f"disable_notice_sent:{source_name}",),
            )
            row = await cursor.fetchone()
            await cursor.close()
            return row is not None and row[0] == "true"

    async def mark_disable_notice_sent(self, source_name: str) -> None:
        async with self._db_lock:
            await self._connection().execute(
                """
                INSERT INTO bot_metadata (key, value) VALUES (?, 'true')
                ON CONFLICT(key) DO UPDATE SET value = 'true'
                """,
                (f"disable_notice_sent:{source_name}",),
            )
            await self._connection().commit()

    async def source_enabled(self, source_name: str, default: bool = True) -> bool:
        async with self._db_lock:
            return await self._read_bool_setting(
                f"source_enabled:{source_name}", default
            )

    async def set_source_enabled(self, source_name: str, enabled: bool) -> None:
        async with self._db_lock:
            await self._write_bool_setting(f"source_enabled:{source_name}", enabled)
            if enabled:
                await self._connection().execute(
                    """
                    UPDATE source_state
                    SET consecutive_failures = 0, disabled = 0, last_error = NULL
                    WHERE source_name = ?
                    """,
                    (source_name,),
                )
            await self._connection().commit()

    async def toggle_source_enabled(
        self, source_name: str, default: bool = True
    ) -> bool:
        async with self._db_lock:
            enabled = not await self._read_bool_setting(
                f"source_enabled:{source_name}", default
            )
            await self._write_bool_setting(f"source_enabled:{source_name}", enabled)
            if enabled:
                await self._connection().execute(
                    """
                    UPDATE source_state
                    SET consecutive_failures = 0, disabled = 0, last_error = NULL
                    WHERE source_name = ?
                    """,
                    (source_name,),
                )
            await self._connection().commit()
            return enabled

    async def market_enabled(self, market: str) -> bool:
        async with self._db_lock:
            return await self._read_bool_setting(f"market_enabled:{market}", True)

    async def toggle_market_enabled(self, market: str) -> bool:
        async with self._db_lock:
            enabled = not await self._read_bool_setting(
                f"market_enabled:{market}", True
            )
            await self._write_bool_setting(f"market_enabled:{market}", enabled)
            await self._connection().commit()
            return enabled

    async def is_paused(self) -> bool:
        async with self._db_lock:
            return await self._read_bool_setting("automation_paused", False)

    async def set_paused(self, paused: bool) -> None:
        async with self._db_lock:
            await self._write_bool_setting("automation_paused", paused)
            await self._connection().commit()

    async def story_counts(self) -> StoryCounts:
        async with self._db_lock:
            cursor = await self._connection().execute(
                """
                SELECT COUNT(*),
                       SUM(CASE WHEN analysis_status = 'pending' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN analysis_status = 'processed' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN analysis_status = 'deferred' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN analysis_status = 'digest_delivered' THEN 1 ELSE 0 END)
                FROM seen_stories
                """
            )
            counts = await cursor.fetchone()
            await cursor.close()
            cursor = await self._connection().execute(
                """
                SELECT market, COUNT(*)
                FROM seen_stories
                WHERE market IS NOT NULL
                GROUP BY market
                """
            )
            market_rows = await cursor.fetchall()
            await cursor.close()
            return StoryCounts(
                total=int(counts[0] or 0),
                pending_analysis=int(counts[1] or 0),
                processed=int(counts[2] or 0),
                deferred=int(counts[3] or 0),
                digest_delivered=int(counts[4] or 0),
                by_market={row[0]: int(row[1]) for row in market_rows},
            )

    async def claim_deferred_stories(self) -> tuple[DeferredStory, ...]:
        async with self._db_lock:
            db = self._connection()
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    """
                    SELECT title, summary, link, source_name, market,
                           published_at, analysis_json
                    FROM seen_stories
                    WHERE analysis_status = 'deferred'
                    ORDER BY first_seen_at, url_hash
                    """
                )
                rows = await cursor.fetchall()
                await cursor.close()
                if rows:
                    await db.execute(
                        """
                        UPDATE seen_stories
                        SET analysis_status = 'digest_delivered'
                        WHERE analysis_status = 'deferred'
                        """
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            return tuple(
                DeferredStory(
                    title=row[0],
                    summary=row[1],
                    link=row[2],
                    source_name=row[3],
                    market=row[4],
                    published_at=row[5],
                    analysis_json=row[6],
                )
                for row in rows
            )

    async def record_story(
        self,
        source_name: str,
        title: str,
        url: str,
        summary: str | None = None,
        market: str | None = None,
        published_at: str | None = None,
        analysis_status: str = "pending",
    ) -> bool:
        async with self._db_lock:
            return await self._record_story_unlocked(
                source_name,
                title,
                url,
                summary,
                market,
                published_at,
                analysis_status,
            )

    async def _record_story_unlocked(
        self,
        source_name: str,
        title: str,
        url: str,
        summary: str | None,
        market: str | None,
        published_at: str | None,
        analysis_status: str,
    ) -> bool:
        normalized_url = normalize_url(url)
        normalized_title = normalize_title(title)
        if not normalized_url or not normalized_title:
            return True

        url_hash = _sha256(normalized_url)
        title_hash = _sha256(normalized_title)
        content_hash = _sha256(f"{normalized_url}\0{normalized_title}")
        short_summary = summary[:500] if summary else None
        db = self._connection()

        cursor = await db.execute(
            """
            SELECT 1 FROM seen_stories
            WHERE url_hash = ? OR title_hash = ?
            LIMIT 1
            """,
            (url_hash, title_hash),
        )
        is_duplicate = await cursor.fetchone() is not None
        await cursor.close()

        if (
            not is_duplicate
            and len(normalized_title) >= MIN_TITLE_LENGTH_FOR_SIMILARITY
        ):
            cursor = await db.execute(
                """
                SELECT normalized_title FROM seen_stories
                WHERE source_name != ? AND length(normalized_title) >= ?
                """,
                (source_name, MIN_TITLE_LENGTH_FOR_SIMILARITY),
            )
            existing_titles = await cursor.fetchall()
            await cursor.close()
            is_duplicate = any(
                SequenceMatcher(None, normalized_title, existing_title).ratio()
                >= TITLE_SIMILARITY_THRESHOLD
                for (existing_title,) in existing_titles
            )

        if is_duplicate:
            return True

        await db.execute(
            """
            INSERT OR IGNORE INTO seen_stories
                (url_hash, title_hash, content_hash, normalized_url,
                 normalized_title, source_name, title, summary, link,
                 market, published_at, analysis_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                url_hash,
                title_hash,
                content_hash,
                normalized_url,
                normalized_title,
                source_name,
                title,
                short_summary,
                url,
                market,
                published_at,
                analysis_status,
            ),
        )
        await db.commit()
        return False

    async def pending_stories(self) -> list[PendingStory]:
        async with self._db_lock:
            cursor = await self._connection().execute(
                """
                SELECT url_hash, title, summary, link, source_name, market,
                       published_at
                FROM seen_stories
                WHERE analysis_status = 'pending'
                ORDER BY first_seen_at, url_hash
                """
            )
            rows = await cursor.fetchall()
            await cursor.close()
            return [
                PendingStory(
                    url_hash=row[0],
                    title=row[1],
                    summary=row[2],
                    link=row[3],
                    source_name=row[4],
                    market=row[5],
                    published_at=row[6],
                )
                for row in rows
            ]

    async def save_analysis(
        self, url_hash: str, analysis_json: str, status: str
    ) -> None:
        async with self._db_lock:
            await self._connection().execute(
                """
                UPDATE seen_stories
                SET analysis_json = ?, analysis_status = ?
                WHERE url_hash = ?
                """,
                (analysis_json, status, url_hash),
            )
            await self._connection().commit()

    async def replace_calendar_events(
        self, events: Sequence[CalendarEvent]
    ) -> None:
        async with self._db_lock:
            db = self._connection()
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute("DELETE FROM calendar_events")
                if events:
                    await db.executemany(
                        """
                        INSERT INTO calendar_events
                            (event_id, title, currency, impact, event_at,
                             actual, forecast, previous)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            (
                                event.event_id,
                                event.title,
                                event.currency,
                                event.impact,
                                event.event_at.isoformat(),
                                event.actual,
                                event.forecast,
                                event.previous,
                            )
                            for event in events
                        ),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def calendar_events(self) -> tuple[CalendarEvent, ...]:
        async with self._db_lock:
            cursor = await self._connection().execute(
                """
                SELECT event_id, title, currency, impact, event_at,
                       actual, forecast, previous
                FROM calendar_events
                ORDER BY event_at, event_id
                """
            )
            rows = await cursor.fetchall()
            await cursor.close()
            return tuple(
                CalendarEvent(
                    event_id=row[0],
                    title=row[1],
                    currency=row[2],
                    impact=row[3],
                    event_at=datetime.fromisoformat(row[4]),
                    actual=row[5],
                    forecast=row[6],
                    previous=row[7],
                )
                for row in rows
            )

    async def claim_calendar_notification(
        self, event_id: str, notification_type: str
    ) -> bool:
        async with self._db_lock:
            cursor = await self._connection().execute(
                """
                INSERT OR IGNORE INTO calendar_notifications
                    (event_id, notification_type)
                VALUES (?, ?)
                """,
                (event_id, notification_type),
            )
            claimed = cursor.rowcount == 1
            await cursor.close()
            await self._connection().commit()
            return claimed

    async def _read_bool_setting(self, key: str, default: bool) -> bool:
        cursor = await self._connection().execute(
            "SELECT value FROM bot_metadata WHERE key = ?",
            (key,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return default
        return row[0] == "true"

    async def _write_bool_setting(self, key: str, value: bool) -> None:
        await self._connection().execute(
            """
            INSERT INTO bot_metadata (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, "true" if value else "false"),
        )

    def _connection(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("StateStore is not open")
        return self._db


def normalize_url(url: str) -> str:
    try:
        parts = urlsplit(url.strip())
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        return ""
    if not parts.scheme or not hostname:
        return ""

    scheme = parts.scheme.lower()
    hostname = hostname.lower().removeprefix("www.")
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None and not (
        (scheme == "http" and port == 80)
        or (scheme == "https" and port == 443)
    ):
        netloc = f"{netloc}:{port}"

    path = unquote(parts.path).rstrip("/") or "/"
    query = sorted(
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
        and key.lower() not in TRACKING_PARAMETERS
    )
    return urlunsplit((scheme, netloc, path, urlencode(query), ""))


def normalize_title(title: str) -> str:
    normalized = unicodedata.normalize("NFKC", title).casefold()
    normalized = re.sub(r"[^\w\s]", " ", normalized)
    return " ".join(normalized.split())


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()
