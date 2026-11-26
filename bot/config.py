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
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
DEFAULT_DEEPSEEK_MODEL = "manageit/deepseek-v4.1-flash"
DEFAULT_DEEPSEEK_BASE_URL = "https://ai.manageit.dev/api/v1"
DEFAULT_GEMINI_CALLS_PER_CYCLE = 20
DEFAULT_DEEPSEEK_DAILY_CALL_CAP = 10
DEFAULT_GEMINI_MAX_OUTPUT_TOKENS = 512
DEFAULT_DEEPSEEK_MAX_OUTPUT_TOKENS = 512


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    owner_chat_id: int
    gemini_api_key: str
    deepseek_api_key: str
    gemini_model: str
    deepseek_model: str
    deepseek_base_url: str
    gemini_calls_per_cycle: int
    deepseek_daily_call_cap: int
    gemini_max_output_tokens: int
    deepseek_max_output_tokens: int
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

        gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not gemini_api_key:
            raise ValueError("GEMINI_API_KEY is required")
        deepseek_api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        if not deepseek_api_key:
            raise ValueError("DEEPSEEK_API_KEY is required")
        gemini_model = os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL).strip()
        if not gemini_model:
            raise ValueError("GEMINI_MODEL must not be empty")
        deepseek_model = os.getenv("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL).strip()
        if not deepseek_model:
            raise ValueError("DEEPSEEK_MODEL must not be empty")
        deepseek_base_url = os.getenv(
            "DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL
        ).strip().rstrip("/")
        parsed_deepseek_url = urlsplit(deepseek_base_url)
        if (
            parsed_deepseek_url.scheme != "https"
            or not parsed_deepseek_url.hostname
            or parsed_deepseek_url.username is not None
            or parsed_deepseek_url.password is not None
            or parsed_deepseek_url.query
            or parsed_deepseek_url.fragment
        ):
            raise ValueError("DEEPSEEK_BASE_URL must be an HTTPS base URL")

        gemini_calls_per_cycle = _positive_int(
            "GEMINI_CALLS_PER_CYCLE", DEFAULT_GEMINI_CALLS_PER_CYCLE
        )
        deepseek_daily_call_cap = _positive_int(
            "DEEPSEEK_DAILY_CALL_CAP", DEFAULT_DEEPSEEK_DAILY_CALL_CAP
        )
        gemini_max_output_tokens = _positive_int(
            "GEMINI_MAX_OUTPUT_TOKENS", DEFAULT_GEMINI_MAX_OUTPUT_TOKENS
        )
        deepseek_max_output_tokens = _positive_int(
            "DEEPSEEK_MAX_OUTPUT_TOKENS", DEFAULT_DEEPSEEK_MAX_OUTPUT_TOKENS
        )

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
            gemini_api_key=gemini_api_key,
            deepseek_api_key=deepseek_api_key,
            gemini_model=gemini_model,
            deepseek_model=deepseek_model,
            deepseek_base_url=deepseek_base_url,
            gemini_calls_per_cycle=gemini_calls_per_cycle,
            deepseek_daily_call_cap=deepseek_daily_call_cap,
            gemini_max_output_tokens=gemini_max_output_tokens,
            deepseek_max_output_tokens=deepseek_max_output_tokens,
            fetch_interval_minutes=interval,
            digest_time=digest_time,
            timezone=timezone,
            db_path=db_path,
            calendar_feed_url=calendar_feed_url,
            calendar_reminder_minutes=calendar_reminder_minutes,
        )


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be at least 1")
    return value
