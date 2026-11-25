from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# TODO: Verify this candidate public weekly JSON feed's availability and schema
# against the provider before relying on it in production.
DEFAULT_CALENDAR_FEED_URL = (
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
)


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    owner_chat_id: int
    api_key: str
    model: str
    fetch_interval_minutes: int
    digest_time: str
    timezone: ZoneInfo
    db_path: Path
    calendar_feed_url: str
    calendar_reminder_minutes: int

    @classmethod
    def from_env(cls) -> Settings:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")

        raw_chat_id = os.getenv("OWNER_CHAT_ID", "").strip()
        try:
            owner_chat_id = int(raw_chat_id)
        except ValueError as exc:
            raise ValueError("OWNER_CHAT_ID must be an integer") from exc

        api_key = os.getenv("API_KEY", "").strip()
        if not api_key:
            raise ValueError("API_KEY is required")
        model = os.getenv("MODEL", "gpt-4o-mini").strip()
        if not model:
            raise ValueError("MODEL must not be empty")

        raw_interval = os.getenv("FETCH_INTERVAL_MINUTES", "10")
        try:
            interval = int(raw_interval)
        except ValueError as exc:
            raise ValueError("FETCH_INTERVAL_MINUTES must be an integer") from exc
        if interval < 1:
            raise ValueError("FETCH_INTERVAL_MINUTES must be at least 1")

        digest_time = os.getenv("DIGEST_TIME", "21:00").strip()
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", digest_time):
            raise ValueError("DIGEST_TIME must use HH:MM (24-hour) format")

        timezone_name = os.getenv("TIMEZONE", "Asia/Tehran").strip()
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("TIMEZONE must be a valid IANA timezone") from exc

        raw_db_path = os.getenv("DB_PATH", "data/newsbot.sqlite3").strip()
        if not raw_db_path:
            raise ValueError("DB_PATH must not be empty")
        db_path = Path(raw_db_path)

        calendar_feed_url = os.getenv(
            "CALENDAR_FEED_URL", DEFAULT_CALENDAR_FEED_URL
        ).strip()
        parsed_calendar_url = urlsplit(calendar_feed_url)
        if (
            parsed_calendar_url.scheme != "https"
            or not parsed_calendar_url.netloc
        ):
            raise ValueError("CALENDAR_FEED_URL must be an HTTPS URL")
        raw_reminder_minutes = os.getenv("CALENDAR_REMINDER_MINUTES", "30")
        try:
            calendar_reminder_minutes = int(raw_reminder_minutes)
        except ValueError as exc:
            raise ValueError("CALENDAR_REMINDER_MINUTES must be an integer") from exc
        if calendar_reminder_minutes < 1:
            raise ValueError("CALENDAR_REMINDER_MINUTES must be at least 1")

        return cls(
            telegram_bot_token=token,
            owner_chat_id=owner_chat_id,
            api_key=api_key,
            model=model,
            fetch_interval_minutes=interval,
            digest_time=digest_time,
            timezone=timezone,
            db_path=db_path,
            calendar_feed_url=calendar_feed_url,
            calendar_reminder_minutes=calendar_reminder_minutes,
        )
