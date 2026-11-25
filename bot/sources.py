from __future__ import annotations

from bot.fetchers.base import Source

SOURCES: tuple[Source, ...] = (
    Source(
        name="investingLive Central Banks",
        market="macro",
        type="rss",
        url="https://investinglive.com/feed/centralbank",
    ),
    Source(
        name="investingLive Breaking News",
        market="macro",
        type="rss",
        url="https://investinglive.com/feed/",
    ),
    Source(
        name="investingLive Forex Orders",
        market="forex",
        type="rss",
        url="https://investinglive.com/feed/forexorders/",
    ),
    Source(
        name="FXStreet News",
        market="forex",
        type="rss",
        url="https://www.fxstreet.com/rss",
    ),
    Source(
        name="CNBC Markets",
        market="global_stock",
        type="rss",
        url="https://www.cnbc.com/id/10001147/device/rss/rss.html",
    ),
    Source(
        name="CNBC Economy",
        market="macro",
        type="rss",
        url="https://www.cnbc.com/id/20910258/device/rss/rss.html",
    ),
    Source(
        name="MarketWatch Top Stories",
        market="global_stock",
        type="rss",
        url="https://feeds.marketwatch.com/marketwatch/topstories",
    ),
    Source(
        name="Investing.com News",
        market="global_stock",
        type="rss",
        url="https://www.investing.com/rss/news.rss",
    ),
    Source(
        name="Reuters (Google News)",
        market="macro",
        type="google_news",
        url=(
            "https://news.google.com/rss/search?"
            "q=site%3Areuters.com+%28markets+OR+forex+OR+economy%29"
        ),
        verification_note="Unofficial Reuters-restricted Google News RSS query.",
    ),
)
