from __future__ import annotations

import html
from datetime import datetime, timezone
from urllib.parse import urlsplit

from bot.analyzer import NewsAnalysis

MARKET_LABELS = {
    "forex": "فارکس",
    "global_stock": "سهام جهانی",
    "macro": "اقتصاد کلان",
}
IMPACT_LABELS = {
    "low": ("🟢", "کم"),
    "medium": ("🟠", "متوسط"),
    "high": ("🔴", "زیاد"),
}
SENTIMENT_LABELS = {
    "positive": "مثبت",
    "negative": "منفی",
    "neutral": "خنثی",
}


def format_analysis_message(
    *,
    title: str,
    market: str | None,
    source_name: str,
    published_at: str | None,
    analysis: NewsAnalysis,
    link: str,
) -> str:
    impact_emoji, impact_label = IMPACT_LABELS[analysis.impact]
    safe_title = _escape(title[:800])
    safe_market = _escape(MARKET_LABELS.get(market, "نامشخص"))
    safe_source = _escape(source_name[:120])
    safe_time = _escape(_format_time(published_at))
    safe_summary = _escape(analysis.summary_fa[:1200])
    safe_assets = _escape("، ".join(analysis.assets)[:600] or "نامشخص")
    safe_sentiment = _escape(SENTIMENT_LABELS[analysis.sentiment])
    safe_reasoning = _escape(analysis.reasoning_fa[:300])
    safe_impact = _escape(impact_label)

    safe_link = link[:2048]
    if _is_http_url(safe_link):
        link_line = (
            f'<a href="{_escape(safe_link)}">مشاهدهٔ خبر</a>'
        )
    else:
        link_line = _escape(safe_link)

    return "\n".join(
        (
            f"<b>{safe_title}</b>",
            f"<b>بازار:</b> {safe_market}",
            f"<b>منبع:</b> {safe_source} | <b>زمان انتشار:</b> {safe_time}",
            f"<b>خلاصه:</b> {safe_summary}",
            f"<b>دارایی‌ها:</b> {safe_assets}",
            f"<b>اثر:</b> {impact_emoji} {safe_impact}",
            f"<b>احساس بازار:</b> {safe_sentiment}",
            f"<b>دلیل:</b> {safe_reasoning}",
            link_line,
            "<i>این محتوا صرفاً جهت اطلاع‌رسانی است و توصیهٔ سرمایه‌گذاری نیست.</i>",
        )
    )


def _escape(value: str) -> str:
    return html.escape(value, quote=True)


def _format_time(value: str | None) -> str:
    if not value:
        return "نامشخص"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return "نامشخص"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _is_http_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
