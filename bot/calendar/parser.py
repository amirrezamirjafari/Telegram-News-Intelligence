from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone, tzinfo
from typing import Any

from bot.calendar.models import CalendarEvent, Impact


class CalendarParseError(ValueError):
    pass


def parse_events(
    payload: bytes | str,
    default_timezone: tzinfo = timezone.utc,
) -> tuple[CalendarEvent, ...]:
    try:
        raw_events: Any = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
        raise CalendarParseError("calendar feed is not valid JSON") from exc
    if not isinstance(raw_events, list):
        raise CalendarParseError("calendar feed must be a JSON array")

    events: dict[str, CalendarEvent] = {}
    for index, raw_event in enumerate(raw_events):
        if not isinstance(raw_event, dict):
            raise CalendarParseError(f"calendar event {index} must be an object")

        title = _required_text(raw_event.get("title"), f"event {index} title")
        event_at = _parse_datetime(
            raw_event.get("date"),
            default_timezone,
            f"event {index} date",
        )
        currency = _optional_text(
            raw_event.get("country") or raw_event.get("currency")
        ) or "—"
        impact = _parse_impact(raw_event.get("impact"))
        raw_event_id = _optional_text(raw_event.get("id"))
        event_id = raw_event_id or _stable_event_id(title, currency, event_at)

        events[event_id] = CalendarEvent(
            event_id=event_id,
            title=title,
            currency=currency,
            impact=impact,
            event_at=event_at,
            actual=_optional_text(raw_event.get("actual")),
            forecast=_optional_text(raw_event.get("forecast")),
            previous=_optional_text(raw_event.get("previous")),
        )

    return tuple(sorted(events.values(), key=lambda event: event.event_at))


def _required_text(value: object, field: str) -> str:
    text = _optional_text(value)
    if text is None:
        raise CalendarParseError(f"{field} is required")
    return text


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_datetime(
    value: object,
    default_timezone: tzinfo,
    field: str,
) -> datetime:
    try:
        if isinstance(value, bool):
            raise ValueError("boolean is not a timestamp")
        if isinstance(value, (int, float)):
            parsed = datetime.fromtimestamp(value, tz=timezone.utc)
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        else:
            raise ValueError("unsupported event time")
    except (OverflowError, OSError, TypeError, ValueError) as exc:
        raise CalendarParseError(f"{field} is invalid") from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=default_timezone)
    return parsed.astimezone(timezone.utc)


def _parse_impact(value: object) -> Impact:
    normalized = (_optional_text(value) or "").casefold()
    if normalized == "high":
        return "high"
    if normalized == "medium":
        return "medium"
    return "low"


def _stable_event_id(title: str, currency: str, event_at: datetime) -> str:
    identity = "\0".join((title.casefold(), currency.casefold(), event_at.isoformat()))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()
