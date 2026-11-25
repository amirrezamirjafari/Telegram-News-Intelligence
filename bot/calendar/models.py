from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

Impact = Literal["high", "medium", "low"]


@dataclass(frozen=True)
class CalendarEvent:
    event_id: str
    title: str
    currency: str
    impact: Impact
    event_at: datetime
    actual: str | None = None
    forecast: str | None = None
    previous: str | None = None
